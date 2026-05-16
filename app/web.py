from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import totp as totp_helper
from app.auth import hash_password, verify_password
from app.config import settings
from app.db import SessionLocal
from app.importer import import_file, parse_amount, peek_pps_element
from app.models import (
    ImportRun,
    Project,
    ProjectAnnualData,
    ProjectBudgetLine,
    Transaction,
    User,
)


PAGE_SIZE = 50
SORT_COLUMNS = {
    "posting_date": Transaction.posting_date,
    "amount": Transaction.amount,
    "account_code": Transaction.account_code,
}


def _format_slo_money(value: Decimal | None) -> str:
    if value is None:
        return ""
    s = f"{value:,.2f}"
    # English: "1,234.56"  →  Slovenian: "1.234,56"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["slo_money"] = _format_slo_money


@dataclass
class ImportRunView:
    imported_at: datetime
    filename: str
    project_name: str
    pps_element: str
    rows_imported: int
    rows_skipped: int


@dataclass
class FileEntry:
    id: str
    filename: str
    size_kb: int
    modified: datetime
    pps_element: Optional[str] = None
    suggested_name: Optional[str] = None
    needs_name: bool = False
    error: Optional[str] = None
    import_run: Optional[ImportRunView] = None


app = FastAPI(title="Fiori")


# Paths that don't require a logged-in session. Everything else is gated.
PUBLIC_PATHS = {
    "/login",
    "/login/change-password",
    "/login/enroll-2fa",
    "/login/2fa",
    "/logout",
}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)
    if not request.session.get("auth_user_id"):
        return RedirectResponse("/login", status_code=303)
    # Make the user available to every template via request.state for the
    # header / profile link.
    uid = request.session["auth_user_id"]
    with SessionLocal() as s:
        u = s.get(User, uid)
        if u is None:
            request.session.clear()
            return RedirectResponse("/login", status_code=303)
        # Detach a lightweight snapshot we can read from templates without a
        # live session — actual mutations always go through a fresh session.
        request.state.current_user_email = u.email
        request.state.current_user_name = u.name
        request.state.current_user_role = u.role
    return await call_next(request)


# SessionMiddleware must be added AFTER the auth middleware. Starlette wraps
# in reverse order of addition, so the last-added middleware is the outermost;
# we need SessionMiddleware to be outer so it populates request.session
# BEFORE require_login reads it.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=settings.session_https_only,
    max_age=settings.session_max_age_days * 86400,
)


def _inputs_dir() -> Path:
    return Path(settings.inputs_dir).resolve()


def _current_user(session: Session, request: Request) -> User:
    user_id = request.session.get("auth_user_id")
    if not user_id:
        # require_login middleware should have prevented this; defensive fall-back.
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = session.get(User, user_id)
    if user is None:
        # Stale session pointing to a deleted user — clear it.
        request.session.clear()
        raise HTTPException(status_code=401, detail="Session invalid")
    return user


def _import_run_view(session: Session, run: ImportRun) -> ImportRunView:
    project = session.get(Project, run.project_id) if run.project_id else None
    return ImportRunView(
        imported_at=run.imported_at,
        filename=run.filename,
        project_name=project.name if project else "<deleted>",
        pps_element=project.pps_element if project else "",
        rows_imported=run.rows_imported,
        rows_skipped=run.rows_skipped,
    )


def _build_entry(session: Session, user_id: int, path: Path) -> FileEntry:
    stat = path.stat()
    entry = FileEntry(
        id=path.name.replace(".", "_").replace(" ", "_"),
        filename=path.name,
        size_kb=max(1, round(stat.st_size / 1024)),
        modified=datetime.fromtimestamp(stat.st_mtime),
    )

    # Latest import_run for this user that matches this file's current hash?
    # We don't precompute the hash here for every file on every page load —
    # importer.sha256_file is fast (<1ms per file at our sizes) but we keep
    # the page render simple by joining on filename+latest. If the user has
    # imported a file with the same NAME but different content, we still show
    # the most recent run; clicking Import will re-hash and detect the change.
    from app.importer import sha256_file  # local import to avoid cycle at import time

    file_hash = sha256_file(path)
    run = session.scalar(
        select(ImportRun)
        .where(
            ImportRun.user_id == user_id,
            ImportRun.file_sha256 == file_hash,
        )
        .order_by(ImportRun.imported_at.desc())
    )
    if run is not None:
        entry.import_run = _import_run_view(session, run)
        return entry

    # Not yet imported — peek the PPS element so we know whether a name prompt
    # is required.
    try:
        pps = peek_pps_element(path)
    except Exception as exc:
        entry.error = f"Cannot read file: {exc}"
        return entry

    entry.pps_element = pps
    project = session.scalar(
        select(Project).where(
            Project.owner_user_id == user_id, Project.pps_element == pps
        )
    )
    entry.needs_name = project is None
    if entry.needs_name:
        # Default to filename stem so the user can accept with one keystroke.
        entry.suggested_name = path.stem
    return entry


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/projects")


@app.get("/imports", response_class=HTMLResponse)
def imports_page(request: Request) -> HTMLResponse:
    inputs_dir = _inputs_dir()
    with SessionLocal() as session:
        user = _current_user(session, request)

        entries: list[FileEntry] = []
        if inputs_dir.exists():
            for path in sorted(inputs_dir.glob("*.txt")):
                entries.append(_build_entry(session, user.id, path))

        runs = (
            session.scalars(
                select(ImportRun)
                .where(ImportRun.user_id == user.id)
                .order_by(ImportRun.imported_at.desc())
                .limit(20)
            )
        ).all()
        run_views = [_import_run_view(session, r) for r in runs]

    return templates.TemplateResponse(
        request,
        "imports.html",
        {
            "inputs_dir": str(inputs_dir),
            "entries": entries,
            "runs": run_views,
            "oob": False,
        },
    )


@app.post("/imports/{filename}", response_class=HTMLResponse)
def do_import(
    request: Request,
    filename: str,
    name: Optional[str] = Form(default=None),
) -> HTMLResponse:
    inputs_dir = _inputs_dir()
    path = inputs_dir / filename
    if not path.exists() or not path.is_file() or path.parent != inputs_dir:
        raise HTTPException(status_code=404, detail="File not found")

    with SessionLocal() as session:
        user = _current_user(session, request)

        resolved_name: Optional[str] = name.strip() if name else None

        def name_resolver(pps: str) -> str:
            # If the page sent a name, use it. Otherwise we have nothing
            # to fall back on — surface the need-name state in the row.
            if resolved_name:
                return resolved_name
            raise _NeedsName(pps)

        try:
            result = import_file(
                session=session,
                path=path,
                user_id=user.id,
                name_resolver=name_resolver,
            )
        except _NeedsName as exc:
            entry = FileEntry(
                id=path.name.replace(".", "_").replace(" ", "_"),
                filename=path.name,
                size_kb=max(1, round(path.stat().st_size / 1024)),
                modified=datetime.fromtimestamp(path.stat().st_mtime),
                pps_element=exc.pps_element,
                needs_name=True,
                suggested_name=path.stem,
            )
            return templates.TemplateResponse(
                request, "_file_card.html", {"entry": entry}
            )
        except ValueError as exc:
            entry = FileEntry(
                id=path.name.replace(".", "_").replace(" ", "_"),
                filename=path.name,
                size_kb=max(1, round(path.stat().st_size / 1024)),
                modified=datetime.fromtimestamp(path.stat().st_mtime),
                error=str(exc),
            )
            return templates.TemplateResponse(
                request, "_file_card.html", {"entry": entry}
            )

        # Success — rebuild the entry showing the new import_run, and also
        # send an out-of-band update for the recent-imports section.
        entry = _build_entry(session, user.id, path)
        runs = (
            session.scalars(
                select(ImportRun)
                .where(ImportRun.user_id == user.id)
                .order_by(ImportRun.imported_at.desc())
                .limit(20)
            )
        ).all()
        run_views = [_import_run_view(session, r) for r in runs]

    row_html = templates.get_template("_file_card.html").render(
        {"request": request, "entry": entry}
    )
    oob_html = templates.get_template("_runs_table.html").render(
        {"request": request, "runs": run_views, "oob": True}
    )
    return HTMLResponse(row_html + oob_html)


class _NeedsName(Exception):
    def __init__(self, pps_element: str):
        super().__init__(f"Need name for new project {pps_element!r}")
        self.pps_element = pps_element


# ---------------------------------------------------------------------------
# Transactions page
# ---------------------------------------------------------------------------


def _accessible_project_ids(session: Session, user_id: int) -> list[int]:
    # Phase 2b: only own projects. ProjectShare will widen this later.
    return list(
        session.scalars(
            select(Project.id).where(Project.owner_user_id == user_id)
        )
    )


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


@dataclass
class TransactionView:
    id: int
    project_id: int
    document_number: str
    account_code: str
    account_text: str | None
    amount: Decimal
    amount_formatted: str
    posting_date: date
    employee: str | None
    text: str | None
    source: str | None
    year: int | None


def _txn_view(tr: Transaction) -> TransactionView:
    return TransactionView(
        id=tr.id,
        project_id=tr.project_id,
        document_number=tr.document_number,
        account_code=tr.account_code,
        account_text=tr.account_text,
        amount=tr.amount,
        amount_formatted=_format_slo_money(tr.amount),
        posting_date=tr.posting_date,
        employee=tr.employee,
        text=tr.text,
        source=tr.source,
        year=tr.year,
    )


@app.get("/transactions", response_class=HTMLResponse)
def transactions_page(
    request: Request,
    project_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    account_code: Optional[str] = None,
    q: Optional[str] = None,
    source: Optional[str] = None,
    sort: str = "posting_date",
    dir: str = "desc",
    page: int = 1,
) -> HTMLResponse:
    if sort not in SORT_COLUMNS:
        sort = "posting_date"
    if dir not in ("asc", "desc"):
        dir = "desc"
    page = max(1, page)

    df = _parse_iso_date(date_from)
    dt = _parse_iso_date(date_to)
    project_id = project_id or None
    account_code = (account_code or "").strip() or None
    q = (q or "").strip() or None
    source = (source or "").strip() or None

    with SessionLocal() as session:
        user = _current_user(session, request)
        accessible = _accessible_project_ids(session, user.id)

        base_where = [Transaction.project_id.in_(accessible)] if accessible else [Transaction.id == -1]
        if project_id is not None:
            base_where.append(Transaction.project_id == project_id)
        if df is not None:
            base_where.append(Transaction.posting_date >= df)
        if dt is not None:
            base_where.append(Transaction.posting_date <= dt)
        if account_code:
            base_where.append(Transaction.account_code.ilike(f"%{account_code}%"))
        if source:
            base_where.append(Transaction.source == source)
        if q:
            like = f"%{q}%"
            base_where.append(
                or_(Transaction.text.ilike(like), Transaction.employee.ilike(like))
            )

        totals = session.execute(
            select(func.count(), func.coalesce(func.sum(Transaction.amount), 0))
            .select_from(Transaction)
            .where(*base_where)
        ).one()
        total_count, total_sum = totals

        sort_col = SORT_COLUMNS[sort]
        order = sort_col.desc() if dir == "desc" else sort_col.asc()
        tiebreak = Transaction.id.desc() if dir == "desc" else Transaction.id.asc()

        offset = (page - 1) * PAGE_SIZE
        rows_q = (
            select(Transaction, Project)
            .join(Project, Transaction.project_id == Project.id)
            .where(*base_where)
            .order_by(order, tiebreak)
            .limit(PAGE_SIZE)
            .offset(offset)
        )
        rows = [(_txn_view(tr), proj) for tr, proj in session.execute(rows_q).all()]

        projects = list(
            session.scalars(
                select(Project)
                .where(Project.owner_user_id == user.id)
                .order_by(Project.name)
            )
        )
        sources = list(
            session.scalars(
                select(Transaction.source)
                .where(Transaction.project_id.in_(accessible) if accessible else False)
                .where(Transaction.source.isnot(None))
                .distinct()
                .order_by(Transaction.source)
            )
        )

    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)

    context = {
        "rows": rows,
        "projects": projects,
        "sources": sources,
        "filters": {
            "project_id": project_id,
            "date_from": date_from,
            "date_to": date_to,
            "account_code": account_code,
            "q": q,
            "source": source,
        },
        "sort": sort,
        "dir": dir,
        "page": page,
        "total_pages": total_pages,
        "total_count": total_count,
        "total_sum_formatted": _format_slo_money(total_sum),
    }

    template = (
        "_transactions_results.html"
        if request.headers.get("HX-Request")
        else "transactions.html"
    )
    return templates.TemplateResponse(request, template, context)


# ---------------------------------------------------------------------------
# Edit a single transaction
# ---------------------------------------------------------------------------


def _form_from_txn(tr: Transaction) -> dict:
    return {
        "posting_date": tr.posting_date.isoformat(),
        "amount": _format_slo_money(tr.amount),
        "document_number": tr.document_number,
        "account_code": tr.account_code,
        "account_text": tr.account_text,
        "employee": tr.employee,
        "text": tr.text,
        "source": tr.source,
        "year": tr.year,
    }


def _load_owned_txn(session: Session, user_id: int, txn_id: int) -> tuple[Transaction, Project]:
    accessible = _accessible_project_ids(session, user_id)
    row = session.execute(
        select(Transaction, Project)
        .join(Project, Transaction.project_id == Project.id)
        .where(Transaction.id == txn_id, Transaction.project_id.in_(accessible))
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return row


@app.get("/transactions/{txn_id}/edit", response_class=HTMLResponse)
def edit_modal(request: Request, txn_id: int) -> HTMLResponse:
    with SessionLocal() as session:
        user = _current_user(session, request)
        tr, proj = _load_owned_txn(session, user.id, txn_id)
        return templates.TemplateResponse(
            request,
            "_edit_modal.html",
            {"tr": tr, "proj": proj, "form": _form_from_txn(tr), "error": None},
        )


@app.post("/transactions/{txn_id}", response_class=HTMLResponse)
def update_transaction(
    request: Request,
    txn_id: int,
    posting_date: str = Form(...),
    amount: str = Form(...),
    document_number: str = Form(...),
    account_code: str = Form(...),
    account_text: Optional[str] = Form(default=None),
    employee: Optional[str] = Form(default=None),
    text: Optional[str] = Form(default=None),
    source: Optional[str] = Form(default=None),
    year: Optional[str] = Form(default=None),
) -> HTMLResponse:
    raw_form = {
        "posting_date": posting_date,
        "amount": amount,
        "document_number": document_number,
        "account_code": account_code,
        "account_text": account_text,
        "employee": employee,
        "text": text,
        "source": source,
        "year": year,
    }

    with SessionLocal() as session:
        user = _current_user(session, request)
        tr, proj = _load_owned_txn(session, user.id, txn_id)

        def fail(msg: str) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "_edit_modal.html",
                {"tr": tr, "proj": proj, "form": raw_form, "error": msg},
            )

        try:
            parsed_date = date.fromisoformat(posting_date.strip())
        except ValueError:
            return fail(f"Posting date {posting_date!r} is not a valid date.")
        try:
            parsed_amount = parse_amount(amount)
        except Exception:
            return fail(
                f"Amount {amount!r} is not a valid number. "
                "Use the Slovenian format, e.g. 1.234,56"
            )
        parsed_year: Optional[int] = None
        if year and year.strip():
            try:
                parsed_year = int(year.strip())
            except ValueError:
                return fail(f"Year {year!r} is not a valid integer.")

        def n(v: Optional[str]) -> Optional[str]:
            return v.strip() if v and v.strip() else None

        tr.posting_date = parsed_date
        tr.amount = parsed_amount
        tr.document_number = document_number.strip()
        tr.account_code = account_code.strip()
        tr.account_text = n(account_text)
        tr.employee = n(employee)
        tr.text = n(text)
        tr.source = n(source)
        tr.year = parsed_year

        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            if "uq_transactions_natural_key" in str(exc.orig):
                return fail(
                    "This change would create a duplicate of another transaction "
                    "in the same project (same document, account, date, amount, "
                    "employee and text). Pick different values."
                )
            return fail(f"Database rejected the change: {exc.orig}")

        session.commit()
        session.refresh(tr)

        updated_view = _txn_view(tr)
        return templates.TemplateResponse(
            request,
            "_transaction_row.html",
            {"tr": updated_view, "proj": proj, "oob": True},
        )


# ---------------------------------------------------------------------------
# Projects list + per-project settings
# ---------------------------------------------------------------------------


@dataclass
class _CategoryWarning:
    label: str
    pct: float                # current spent / budget, ≥ 80
    pct_fmt: str              # "120%"
    severity: str             # "over" (>100) or "near" (80..100)


@dataclass
class _ProjectCardView:
    id: int
    name: str
    pps_element: str
    description: str | None
    latest_year: int | None
    ytd_spent_fmt: str
    projection_fmt: str
    budget_fmt: str | None
    budget: Decimal | None        # raw, for conditional logic
    projection: Decimal | None
    last_txn_date_fmt: str
    spent_pct: float              # raw % (may exceed 100)
    spent_pct_fmt: str            # formatted "N.N"
    projection_pct_fmt: str
    bar_pct: float                # clamped 0..100
    tick_pct: float               # clamped 0..100
    bar_color: str                # Tailwind classes
    warnings: list[_CategoryWarning] = field(default_factory=list)


def _card_view(summary) -> _ProjectCardView:
    from app.analytics import ProjectSummary  # noqa: F401 (type hint reference)

    p = summary.project
    budget = summary.annual.total_budget if summary.annual else None

    spent_pct = (
        float(summary.ytd_spent) / float(budget) * 100
        if budget and budget > 0
        else 0.0
    )
    projection_pct = (
        float(summary.projection) / float(budget) * 100
        if budget and budget > 0 and summary.projection is not None
        else 0.0
    )
    if spent_pct < 80:
        bar_color = "bg-emerald-500 dark:bg-emerald-600"
    elif spent_pct < 100:
        bar_color = "bg-amber-500 dark:bg-amber-600"
    else:
        bar_color = "bg-red-500 dark:bg-red-600"

    return _ProjectCardView(
        id=p.id,
        name=p.name,
        pps_element=p.pps_element,
        description=p.description,
        latest_year=summary.latest_year,
        ytd_spent_fmt=_format_slo_money(summary.ytd_spent),
        projection_fmt=(
            _format_slo_money(summary.projection)
            if summary.projection is not None else "—"
        ),
        budget_fmt=_format_slo_money(budget) if budget is not None else None,
        budget=budget,
        projection=summary.projection,
        last_txn_date_fmt=(
            summary.last_txn_date.strftime("%Y-%m-%d")
            if summary.last_txn_date else "—"
        ),
        spent_pct=spent_pct,
        spent_pct_fmt=f"{spent_pct:.1f}",
        projection_pct_fmt=f"{projection_pct:.0f}",
        bar_pct=min(100.0, spent_pct),
        tick_pct=min(100.0, projection_pct),
        bar_color=bar_color,
    )


def _card_warnings(structure) -> list[_CategoryWarning]:
    """Pick rows where spent/budget ≥ 80%. Worst first (highest pct)."""
    items: list[_CategoryWarning] = []
    for row in structure:
        if row.budgeted is None or row.budgeted <= 0:
            continue
        pct = float(row.spent) / float(row.budgeted) * 100
        if pct > 100:
            severity = "over"
        elif pct >= 80:
            severity = "near"
        else:
            continue
        items.append(
            _CategoryWarning(
                label=row.label,
                pct=pct,
                pct_fmt=f"{pct:.0f}%",
                severity=severity,
            )
        )
    items.sort(key=lambda w: (0 if w.severity == "over" else 1, -w.pct))
    return items


@app.get("/projects", response_class=HTMLResponse)
def projects_list(request: Request) -> HTMLResponse:
    from app.analytics import project_summary, structure_breakdown

    with SessionLocal() as session:
        user = _current_user(session, request)
        projects = list(
            session.scalars(
                select(Project)
                .where(Project.owner_user_id == user.id)
                .order_by(Project.name)
            )
        )
        cards = []
        for p in projects:
            summary = project_summary(session, p)
            card = _card_view(summary)
            if summary.latest_year is not None:
                structure = structure_breakdown(
                    session, p, summary.latest_year
                )
                card.warnings = _card_warnings(structure)
            cards.append(card)
    return templates.TemplateResponse(
        request, "projects.html", {"cards": cards}
    )


def _format_slo_amount_or_blank(value: Decimal | None) -> str:
    return _format_slo_money(value) if value is not None else ""


def _parse_slo_amount(s: str | None, total_budget: Decimal | None = None) -> Decimal | None:
    """Parse a user-typed amount.
    Returns None for empty input. Raises ValueError for invalid input.
    Supports `1.234,56`, `1234,56`, plain `1234.56`, and `20%` (of total_budget).
    """
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    if s.endswith("%"):
        if total_budget is None:
            raise ValueError(
                f"Cannot use percent {s!r} without a total budget set."
            )
        pct = parse_amount(s[:-1])  # Decimal
        return (total_budget * pct / Decimal(100)).quantize(Decimal("0.01"))
    return parse_amount(s)


@dataclass
class BudgetLineView:
    label: str
    account_prefix: str | None
    amount: Decimal
    amount_formatted: str


@dataclass
class AnnualDataView:
    year: int
    total_budget: Decimal | None
    total_budget_formatted: str
    starting_balance: Decimal | None
    starting_balance_formatted: str
    budget_lines: list[BudgetLineView]
    lines_sum_formatted: str


def _annual_view(ad) -> AnnualDataView:
    lines = [
        BudgetLineView(
            label=line.label,
            account_prefix=line.account_prefix,
            amount=line.amount,
            amount_formatted=_format_slo_money(line.amount),
        )
        for line in ad.budget_lines
    ]
    lines_sum = sum((l.amount for l in lines), start=Decimal("0"))
    return AnnualDataView(
        year=ad.year,
        total_budget=ad.total_budget,
        total_budget_formatted=_format_slo_amount_or_blank(ad.total_budget),
        starting_balance=ad.starting_balance,
        starting_balance_formatted=_format_slo_amount_or_blank(ad.starting_balance),
        budget_lines=lines,
        lines_sum_formatted=_format_slo_money(lines_sum),
    )


def _load_project_for_settings(session: Session, user_id: int, project_id: int):
    """Load a project owned by user_id, ensure an annual_data row exists for
    each year present in its transactions, and return (project, annual_views).
    """
    from app.models import ProjectAnnualData  # local import to avoid cycle

    project = session.scalar(
        select(Project).where(
            Project.id == project_id, Project.owner_user_id == user_id
        )
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    existing_years = {ad.year for ad in project.annual_data}
    txn_years = set(
        session.scalars(
            select(func.distinct(func.extract("year", Transaction.posting_date)))
            .where(Transaction.project_id == project.id)
        )
    )
    txn_years = {int(y) for y in txn_years if y is not None}
    for year in sorted(txn_years - existing_years):
        session.add(ProjectAnnualData(project_id=project.id, year=year))
    if txn_years - existing_years:
        session.commit()
        session.refresh(project)

    annual_views = [_annual_view(ad) for ad in sorted(project.annual_data, key=lambda a: a.year, reverse=True)]
    return project, annual_views


@app.get("/projects/{project_id}/settings", response_class=HTMLResponse)
def project_settings(request: Request, project_id: int, saved: int = 0, error: str = "") -> HTMLResponse:
    with SessionLocal() as session:
        user = _current_user(session, request)
        project, annual_views = _load_project_for_settings(session, user.id, project_id)
        # We need the annual_views attached for the template loop. Re-render
        # project's annual_data as views, since template iterates project.annual_data.
        # Simplest path: just pass annual_views as the iterable.
        return templates.TemplateResponse(
            request,
            "project_settings.html",
            {
                "project": _ProjectSettingsView(
                    id=project.id,
                    name=project.name,
                    pps_element=project.pps_element,
                    description=project.description,
                    annual_data=annual_views,
                ),
                "saved": bool(saved),
                "error": error or None,
            },
        )


@dataclass
class _ProjectSettingsView:
    id: int
    name: str
    pps_element: str
    description: str | None
    annual_data: list[AnnualDataView]


@app.post("/projects/{project_id}/settings", response_class=HTMLResponse)
async def save_project_settings(request: Request, project_id: int) -> HTMLResponse:
    form = await request.form()
    action = form.get("action", "save")
    delete_year = form.get("delete_year")

    from app.models import ProjectAnnualData, ProjectBudgetLine
    import re

    with SessionLocal() as session:
        user = _current_user(session, request)
        project = session.scalar(
            select(Project).where(
                Project.id == project_id, Project.owner_user_id == user.id
            )
        )
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        # ── action: delete a year ──────────────────────────────────────────
        if delete_year:
            try:
                yr = int(delete_year)
            except ValueError:
                return RedirectResponse(
                    f"/projects/{project_id}/settings?error=Invalid+year",
                    status_code=303,
                )
            session.execute(
                ProjectAnnualData.__table__.delete().where(
                    ProjectAnnualData.project_id == project.id,
                    ProjectAnnualData.year == yr,
                )
            )
            session.commit()
            return RedirectResponse(
                f"/projects/{project_id}/settings?saved=1", status_code=303
            )

        # ── action: add a new year ─────────────────────────────────────────
        if action == "add_year":
            raw = (form.get("add_year") or "").strip()
            if not raw:
                return RedirectResponse(
                    f"/projects/{project_id}/settings?error=Enter+a+year+first",
                    status_code=303,
                )
            try:
                yr = int(raw)
            except ValueError:
                return RedirectResponse(
                    f"/projects/{project_id}/settings?error=Invalid+year",
                    status_code=303,
                )
            existing = session.scalar(
                select(ProjectAnnualData).where(
                    ProjectAnnualData.project_id == project.id,
                    ProjectAnnualData.year == yr,
                )
            )
            if existing is None:
                session.add(ProjectAnnualData(project_id=project.id, year=yr))
                session.commit()
            return RedirectResponse(
                f"/projects/{project_id}/settings?saved=1", status_code=303
            )

        # ── action: save all ───────────────────────────────────────────────
        try:
            new_name = (form.get("name") or "").strip()
            if not new_name:
                raise ValueError("Project name cannot be empty.")
            project.name = new_name
            project.description = (form.get("description") or "").strip() or None

            # Parse form into { year: { total, starting, lines: [(idx, label, prefix, amount_str)] } }
            year_data: dict[int, dict] = {}
            for key, value in form.multi_items():
                m = re.match(r"^year_(\d+)_(total|starting)$", key)
                if m:
                    yr = int(m.group(1))
                    field = m.group(2)
                    year_data.setdefault(yr, {"lines": {}})[field] = value
                    continue
                m = re.match(r"^year_(\d+)_line_(\d+)_(label|prefix|amount)$", key)
                if m:
                    yr = int(m.group(1))
                    idx = int(m.group(2))
                    field = m.group(3)
                    yd = year_data.setdefault(yr, {"lines": {}})
                    yd["lines"].setdefault(idx, {})[field] = value

            for yr, data in year_data.items():
                ad = session.scalar(
                    select(ProjectAnnualData).where(
                        ProjectAnnualData.project_id == project.id,
                        ProjectAnnualData.year == yr,
                    )
                )
                if ad is None:
                    continue  # year was deleted concurrently; skip

                ad.total_budget = _parse_slo_amount(data.get("total"))
                ad.starting_balance = _parse_slo_amount(data.get("starting"))

                # Replace lines wholesale
                session.execute(
                    ProjectBudgetLine.__table__.delete().where(
                        ProjectBudgetLine.project_annual_data_id == ad.id
                    )
                )
                session.flush()
                position = 0
                for idx in sorted(data["lines"].keys()):
                    line_in = data["lines"][idx]
                    label = (line_in.get("label") or "").strip()
                    amt_raw = (line_in.get("amount") or "").strip()
                    if not label and not amt_raw:
                        continue  # blank row → drop
                    if not label:
                        raise ValueError(f"Year {yr}: a budget line is missing a label.")
                    if not amt_raw:
                        raise ValueError(f"Year {yr}: line {label!r} is missing an amount.")
                    amt = _parse_slo_amount(amt_raw, total_budget=ad.total_budget)
                    if amt is None:
                        raise ValueError(f"Year {yr}: line {label!r} amount is invalid.")
                    session.add(
                        ProjectBudgetLine(
                            project_annual_data_id=ad.id,
                            label=label,
                            account_prefix=(line_in.get("prefix") or "").strip() or None,
                            amount=amt,
                            position=position,
                        )
                    )
                    position += 1

            session.commit()
        except ValueError as exc:
            session.rollback()
            from urllib.parse import quote
            return RedirectResponse(
                f"/projects/{project_id}/settings?error={quote(str(exc))}",
                status_code=303,
            )

    return RedirectResponse(
        f"/projects/{project_id}/settings?saved=1", status_code=303
    )


# ---------------------------------------------------------------------------
# Project detail page (View A drill-down + View B grid)
# ---------------------------------------------------------------------------


def _all_years_for_project(session: Session, project_id: int) -> list[int]:
    years = session.scalars(
        select(func.distinct(func.extract("year", Transaction.posting_date)))
        .where(Transaction.project_id == project_id)
    )
    return sorted({int(y) for y in years if y is not None}, reverse=True)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(
    request: Request,
    project_id: int,
    year: Optional[int] = None,
) -> HTMLResponse:
    from app.analytics import (
        MONTHS_SHORT,
        per_person_month,
        project_summary,
        structure_breakdown,
    )

    with SessionLocal() as session:
        user = _current_user(session, request)
        project = session.scalar(
            select(Project).where(
                Project.id == project_id, Project.owner_user_id == user.id
            )
        )
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        summary = project_summary(session, project)
        available_years = _all_years_for_project(session, project.id)
        if not available_years:
            return templates.TemplateResponse(
                request,
                "project_detail.html",
                {
                    "project": project,
                    "summary": summary,
                    "available_years": [],
                    "year": None,
                },
            )

        selected_year = year if year in available_years else available_years[0]

        structure = structure_breakdown(session, project, selected_year)
        # If the selected year is the same as summary.latest_year, reuse its
        # annual data; otherwise fetch the year-specific row.
        if summary.annual and summary.annual.year == selected_year:
            annual = summary.annual
        else:
            annual = session.scalar(
                select(ProjectAnnualData).where(
                    ProjectAnnualData.project_id == project.id,
                    ProjectAnnualData.year == selected_year,
                )
            )
        budget = annual.total_budget if annual else None
        starting_balance = annual.starting_balance if annual else None

        # Spending in the selected year (recompute if different from latest_year).
        if selected_year == summary.latest_year:
            ytd_for_year = summary.ytd_spent
        else:
            ytd_for_year = session.scalar(
                select(func.coalesce(func.sum(Transaction.amount), 0))
                .where(
                    Transaction.project_id == project.id,
                    func.extract("year", Transaction.posting_date) == selected_year,
                )
            ) or Decimal(0)

        # Bar / projection visual numbers.
        spent_pct = (
            float(ytd_for_year) / float(budget) * 100
            if budget and budget > 0 else 0.0
        )
        if summary.projection is not None and budget and budget > 0 and selected_year == summary.latest_year:
            projection_pct = float(summary.projection) / float(budget) * 100
        else:
            projection_pct = 0.0
        if spent_pct < 80:
            bar_color = "bg-emerald-500 dark:bg-emerald-600"
        elif spent_pct < 100:
            bar_color = "bg-amber-500 dark:bg-amber-600"
        else:
            bar_color = "bg-red-500 dark:bg-red-600"

        # Replace summary.ytd_spent with the selected year's value so the
        # template renders consistently when toggling years.
        summary.ytd_spent = ytd_for_year

        grid = per_person_month(session, project, selected_year)

        from app.analytics import monthly_totals
        monthly = monthly_totals(session, project, selected_year)

    structure_has_budget = any(r.budgeted is not None for r in structure)

    # Chart data: serialised as JSON in the template.
    # Donut excludes zero-spend categories so empty slices don't clutter.
    donut_data = [
        {"label": r.label, "spent": float(r.spent)}
        for r in structure
        if float(r.spent) != 0
    ]
    monthly_chart = {
        "labels": MONTHS_SHORT,
        "totals": monthly,
        "monthly_budget": (
            float(budget) / 12.0 if budget and budget > 0 else None
        ),
    }

    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            "project": project,
            "summary": summary,
            "available_years": available_years,
            "year": selected_year,
            "budget": budget,
            "starting_balance": starting_balance,
            "spent_pct": spent_pct,
            "projection_pct": projection_pct,
            "bar_pct": min(100.0, spent_pct),
            "tick_pct": min(100.0, projection_pct),
            "bar_color": bar_color,
            "structure": structure,
            "structure_has_budget": structure_has_budget,
            "grid": grid,
            "month_names": MONTHS_SHORT,
            "monthly_chart": monthly_chart,
            "donut_data": donut_data,
        },
    )


@app.get("/projects/{project_id}/cell", response_class=HTMLResponse)
def cell_expansion(
    request: Request,
    project_id: int,
    year: int,
    month: int,
    employee: str = "",
) -> HTMLResponse:
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="month must be 1..12")

    with SessionLocal() as session:
        user = _current_user(session, request)
        project = session.scalar(
            select(Project).where(
                Project.id == project_id, Project.owner_user_id == user.id
            )
        )
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        where_clauses = [
            Transaction.project_id == project.id,
            func.extract("year", Transaction.posting_date) == year,
            func.extract("month", Transaction.posting_date) == month,
        ]
        if employee:
            where_clauses.append(Transaction.employee == employee)
        else:
            where_clauses.append(Transaction.employee.is_(None))

        txns = list(
            session.scalars(
                select(Transaction)
                .where(*where_clauses)
                .order_by(Transaction.posting_date, Transaction.id)
            )
        )

    from app.analytics import MONTHS_SHORT, build_employee_name_map

    total = sum((t.amount for t in txns), start=Decimal("0"))
    if employee:
        with SessionLocal() as session:
            names = build_employee_name_map(session, project_id)
        display = (
            f"{names[employee]} ({employee})" if employee in names else f"employee {employee}"
        )
    else:
        display = "(no employee)"
    heading = f"{MONTHS_SHORT[month - 1]} {year} — {display}"

    return templates.TemplateResponse(
        request,
        "_cell_expansion.html",
        {"txns": txns, "total": total, "heading": heading},
    )


# ---------------------------------------------------------------------------
# Category breakdown: click a Spent value → expand transactions for that line
# ---------------------------------------------------------------------------


@app.get("/projects/{project_id}/category-transactions", response_class=HTMLResponse)
def category_transactions(
    request: Request,
    project_id: int,
    year: int,
    line_id: str,  # numeric ProjectBudgetLine.id, or "other"
) -> HTMLResponse:
    from sqlalchemy import not_

    from app.analytics import OTHER_LINE_ID

    with SessionLocal() as session:
        user = _current_user(session, request)
        project = session.scalar(
            select(Project).where(
                Project.id == project_id, Project.owner_user_id == user.id
            )
        )
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        # Pull all budget-line prefixes for this project+year (for OTHER, and
        # for the "exclude longer prefixes" trick on a specific line).
        annual = session.scalar(
            select(ProjectAnnualData).where(
                ProjectAnnualData.project_id == project.id,
                ProjectAnnualData.year == year,
            )
        )
        all_lines = list(annual.budget_lines) if annual else []
        all_prefixes = [l.account_prefix for l in all_lines if l.account_prefix]

        where_clauses = [
            Transaction.project_id == project.id,
            func.extract("year", Transaction.posting_date) == year,
        ]

        if line_id == OTHER_LINE_ID:
            # OTHER = matches none of the defined prefixes.
            for prefix in all_prefixes:
                where_clauses.append(
                    not_(Transaction.account_code.like(f"{prefix}%"))
                )
            heading = f"OTHER ({year})"
        else:
            try:
                lid = int(line_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="bad line_id")
            target = next((l for l in all_lines if l.id == lid), None)
            if target is None or not target.account_prefix:
                raise HTTPException(
                    status_code=404, detail="Budget line not found or has no prefix"
                )
            # Longest-prefix-match semantics: include rows starting with this
            # line's prefix but not with a longer prefix that also exists.
            where_clauses.append(
                Transaction.account_code.like(f"{target.account_prefix}%")
            )
            for other_prefix in all_prefixes:
                if (
                    len(other_prefix) > len(target.account_prefix)
                    and other_prefix.startswith(target.account_prefix)
                ):
                    where_clauses.append(
                        not_(Transaction.account_code.like(f"{other_prefix}%"))
                    )
            heading = f"{target.label} (prefix {target.account_prefix}, {year})"

        txns = list(
            session.scalars(
                select(Transaction)
                .where(*where_clauses)
                .order_by(Transaction.posting_date, Transaction.id)
            )
        )

    total = sum((t.amount for t in txns), start=Decimal("0"))
    return templates.TemplateResponse(
        request,
        "_category_expansion.html",
        {"txns": txns, "total": total, "heading": heading},
    )


# ---------------------------------------------------------------------------
# Authentication flow: /login → password → maybe change-password → maybe
# enroll-2fa → 2fa verification → logged in.
# Session keys:
#   pending_user_id : authenticated by password, not yet by 2FA
#   pending_force_password_change : True while user must reset their password
#   pending_totp_secret : new TOTP secret being enrolled (not yet saved)
#   auth_user_id  : fully authenticated; gated routes accept this
# ---------------------------------------------------------------------------


def _go_after_login(request: Request, user: User) -> RedirectResponse:
    """Pick the next step after a valid password submission."""
    if user.force_password_change:
        request.session["pending_force_password_change"] = True
        return RedirectResponse("/login/change-password", status_code=303)
    if not user.totp_secret:
        request.session["pending_totp_secret"] = totp_helper.generate_secret()
        return RedirectResponse("/login/enroll-2fa", status_code=303)
    return RedirectResponse("/login/2fa", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    if request.session.get("auth_user_id"):
        return RedirectResponse("/projects", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "email": ""}
    )


@app.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
) -> Response:
    email = email.strip().lower()
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == email))
        if user is None or not verify_password(password, user.password_hash):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Invalid email or password.", "email": email},
                status_code=400,
            )
        # Clear any leftover partial-login state.
        request.session.pop("pending_user_id", None)
        request.session.pop("pending_force_password_change", None)
        request.session.pop("pending_totp_secret", None)
        request.session["pending_user_id"] = user.id
        return _go_after_login(request, user)


def _require_pending_user(request: Request, session: Session) -> User | RedirectResponse:
    uid = request.session.get("pending_user_id")
    if not uid:
        return RedirectResponse("/login", status_code=303)
    user = session.get(User, uid)
    if user is None:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
    return user


@app.get("/login/change-password", response_class=HTMLResponse)
def change_password_forced_page(request: Request) -> Response:
    with SessionLocal() as session:
        user = _require_pending_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
    return templates.TemplateResponse(
        request,
        "change_password.html",
        {
            "error": None,
            "forced": True,
            "action_url": "/login/change-password",
        },
    )


@app.post("/login/change-password")
def change_password_forced_submit(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> Response:
    if new_password != confirm_password:
        return templates.TemplateResponse(
            request,
            "change_password.html",
            {
                "error": "The two passwords don't match.",
                "forced": True,
                "action_url": "/login/change-password",
            },
            status_code=400,
        )
    if len(new_password) < 10:
        return templates.TemplateResponse(
            request,
            "change_password.html",
            {
                "error": "Password must be at least 10 characters.",
                "forced": True,
                "action_url": "/login/change-password",
            },
            status_code=400,
        )
    with SessionLocal() as session:
        user = _require_pending_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        user.password_hash = hash_password(new_password)
        user.force_password_change = False
        session.commit()
        request.session.pop("pending_force_password_change", None)
        # Continue down the login funnel.
        return _go_after_login(request, user)


@app.get("/login/enroll-2fa", response_class=HTMLResponse)
def enroll_2fa_page(request: Request) -> Response:
    with SessionLocal() as session:
        user = _require_pending_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
    secret = request.session.get("pending_totp_secret")
    if not secret:
        # Direct GET — generate and remember a fresh secret for this attempt.
        secret = totp_helper.generate_secret()
        request.session["pending_totp_secret"] = secret
    uri = totp_helper.provisioning_uri(
        email=user.email, secret=secret, issuer=settings.totp_issuer
    )
    return templates.TemplateResponse(
        request,
        "enroll_2fa.html",
        {"error": None, "qr_svg": totp_helper.qr_svg(uri), "secret": secret},
    )


@app.post("/login/enroll-2fa")
def enroll_2fa_submit(
    request: Request, code: str = Form(...)
) -> Response:
    secret = request.session.get("pending_totp_secret")
    if not secret:
        return RedirectResponse("/login", status_code=303)
    with SessionLocal() as session:
        user = _require_pending_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        if not totp_helper.verify_code(secret, code):
            uri = totp_helper.provisioning_uri(
                email=user.email, secret=secret, issuer=settings.totp_issuer
            )
            return templates.TemplateResponse(
                request,
                "enroll_2fa.html",
                {
                    "error": "That code didn't match. Try again with a fresh one.",
                    "qr_svg": totp_helper.qr_svg(uri),
                    "secret": secret,
                },
                status_code=400,
            )
        user.totp_secret = secret
        session.commit()
        request.session.pop("pending_totp_secret", None)
        request.session.pop("pending_user_id", None)
        request.session["auth_user_id"] = user.id
        return RedirectResponse("/projects", status_code=303)


@app.get("/login/2fa", response_class=HTMLResponse)
def verify_2fa_page(request: Request) -> Response:
    with SessionLocal() as session:
        user = _require_pending_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        if not user.totp_secret:
            return RedirectResponse("/login/enroll-2fa", status_code=303)
    return templates.TemplateResponse(
        request, "login_2fa.html", {"error": None}
    )


@app.post("/login/2fa")
def verify_2fa_submit(
    request: Request, code: str = Form(...)
) -> Response:
    with SessionLocal() as session:
        user = _require_pending_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        if not user.totp_secret or not totp_helper.verify_code(user.totp_secret, code):
            return templates.TemplateResponse(
                request,
                "login_2fa.html",
                {"error": "Invalid code. Try the next one your app shows."},
                status_code=400,
            )
        request.session.pop("pending_user_id", None)
        request.session["auth_user_id"] = user.id
        return RedirectResponse("/projects", status_code=303)


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------------------
# Profile page: view account info, voluntary change-password
# ---------------------------------------------------------------------------


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, saved: int = 0, error: str = "") -> HTMLResponse:
    with SessionLocal() as session:
        user = _current_user(session, request)
        return templates.TemplateResponse(
            request,
            "profile.html",
            {
                "user": user,
                "saved": bool(saved),
                "error": error or None,
            },
        )


@app.post("/profile/password")
def profile_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> RedirectResponse:
    from urllib.parse import quote

    def fail(msg: str) -> RedirectResponse:
        return RedirectResponse(f"/profile?error={quote(msg)}", status_code=303)

    if new_password != confirm_password:
        return fail("The two new passwords don't match.")
    if len(new_password) < 10:
        return fail("Password must be at least 10 characters.")
    with SessionLocal() as session:
        user = _current_user(session, request)
        if not verify_password(current_password, user.password_hash):
            return fail("Current password is incorrect.")
        user.password_hash = hash_password(new_password)
        user.force_password_change = False
        session.commit()
    return RedirectResponse("/profile?saved=1", status_code=303)
