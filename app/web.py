from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.importer import import_file, parse_amount, peek_pps_element
from app.models import ImportRun, Project, Transaction, User


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


def _inputs_dir() -> Path:
    return Path(settings.inputs_dir).resolve()


def _current_user(session: Session) -> User:
    user = session.scalar(
        select(User).where(User.email == settings.default_admin_email)
    )
    if user is None:
        raise HTTPException(
            status_code=500,
            detail="No admin user found. Run `fiori init` first.",
        )
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
    return RedirectResponse(url="/imports")


@app.get("/imports", response_class=HTMLResponse)
def imports_page(request: Request) -> HTMLResponse:
    inputs_dir = _inputs_dir()
    with SessionLocal() as session:
        user = _current_user(session)

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
        user = _current_user(session)

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
        user = _current_user(session)
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
        user = _current_user(session)
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
        user = _current_user(session)
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


@app.get("/projects", response_class=HTMLResponse)
def projects_list(request: Request) -> HTMLResponse:
    with SessionLocal() as session:
        user = _current_user(session)
        projects = list(
            session.scalars(
                select(Project)
                .where(Project.owner_user_id == user.id)
                .order_by(Project.name)
            )
        )
    return templates.TemplateResponse(
        request, "projects.html", {"projects": projects}
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
        user = _current_user(session)
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
        user = _current_user(session)
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
