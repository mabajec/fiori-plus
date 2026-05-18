from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import totp as totp_helper
from app.auth import hash_password, verify_password
from app.config import settings
from app.db import SessionLocal
from app.importer import (
    ALL_FIELDS,
    COLUMN_MAP,
    NeedsHeaderMapping,
    HeaderResolution,
    import_file,
    parse_amount,
    parse_header,
    peek_pps_element,
)
from app.models import (
    HeaderMapping,
    ImportRun,
    Project,
    ProjectAnnualData,
    ProjectBudgetLine,
    ProjectShare,
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
templates.env.globals["date"] = date


@dataclass
class ImportRunView:
    id: int
    imported_at: datetime
    filename: str
    project_name: str
    pps_element: str
    mode: str
    rows_imported: int
    rows_skipped: int
    rows_deleted: int


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
    # Set when the header row needs the user to map columns before we can
    # parse it. The card switches to a "Map columns" call-to-action.
    needs_mapping: bool = False
    mapping_signature: Optional[str] = None
    # Set when an existing saved mapping is being used to parse this file —
    # shows a small badge with Edit / Forget controls.
    saved_mapping_id: Optional[int] = None
    saved_mapping_created: Optional[datetime] = None


app = FastAPI(title="Fiori Plus")
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)


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


def _user_inputs_dir(user_id: int) -> Path:
    """Per-user subdirectory under the inputs root. Each user only sees their
    own uploads, so payroll files from one project coordinator don't leak to
    another. Created on demand so a brand-new user's imports page doesn't
    trip over a missing path."""
    d = _inputs_dir() / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_user_file(user_id: int, filename: str) -> Path:
    """Look up `filename` in the user's inputs directory, with the same
    safety check every endpoint needs: the resolved path must live directly
    under the user's folder (no path-traversal escapes), and must exist.

    Raises HTTPException(404) on miss."""
    user_dir = _user_inputs_dir(user_id)
    path = user_dir / filename
    if not path.exists() or not path.is_file() or path.parent != user_dir:
        raise HTTPException(status_code=404, detail="File not found")
    return path


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
        id=run.id,
        imported_at=run.imported_at,
        filename=run.filename,
        project_name=project.name if project else "<deleted>",
        pps_element=project.pps_element if project else "",
        mode=run.mode,
        rows_imported=run.rows_imported,
        rows_skipped=run.rows_skipped,
        rows_deleted=run.rows_deleted,
    )


def _lookup_saved_mapping(
    session: Session, user_id: int, signature: str
) -> Optional[HeaderMapping]:
    return session.scalar(
        select(HeaderMapping).where(
            HeaderMapping.user_id == user_id,
            HeaderMapping.signature == signature,
        )
    )


def _resolve_for_file(
    session: Session, user_id: int, path: Path
) -> tuple[dict[str, str] | None, Optional[HeaderMapping], Optional[HeaderResolution]]:
    """Decide what overrides (if any) to apply when parsing `path` for this
    user. Returns (overrides, saved_mapping, blocking_resolution).

      - overrides is non-None when a saved mapping covers the file's headers
      - saved_mapping is the ORM row that backed those overrides (for the
        Edit / Forget UI)
      - blocking_resolution is non-None when we still can't resolve the
        headers (caller should switch to the mapping panel); its
        `signature` is what a future saved mapping would key on
    """
    try:
        parse_header(path)
        return None, None, None
    except NeedsHeaderMapping as exc:
        saved = _lookup_saved_mapping(session, user_id, exc.resolution.signature)
        if saved is None:
            return None, None, exc.resolution
        try:
            parse_header(path, overrides=saved.mapping)
        except NeedsHeaderMapping as exc2:
            # Saved mapping is incomplete (e.g. file gained another unmapped
            # column). Treat as still-blocking — the user will see the
            # mapping panel pre-filled with the current saved mapping.
            return None, saved, exc2.resolution
        return dict(saved.mapping), saved, None


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

    # Not yet imported — work out whether the headers parse natively, with a
    # saved mapping, or not at all.
    try:
        overrides, saved, blocking = _resolve_for_file(session, user_id, path)
    except Exception as exc:
        entry.error = f"Cannot read file: {exc}"
        return entry
    if blocking is not None:
        entry.needs_mapping = True
        entry.mapping_signature = blocking.signature
        if saved is not None:
            entry.saved_mapping_id = saved.id
            entry.saved_mapping_created = saved.created_at
        return entry
    if saved is not None:
        entry.saved_mapping_id = saved.id
        entry.saved_mapping_created = saved.created_at

    try:
        pps = peek_pps_element(path, overrides=overrides)
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
    with SessionLocal() as session:
        user = _current_user(session, request)
        user_dir = _user_inputs_dir(user.id)

        entries: list[FileEntry] = []
        for path in sorted(user_dir.glob("*.txt")):
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
            "inputs_dir": str(user_dir),
            "entries": entries,
            "runs": run_views,
            "oob": False,
        },
    )


# ---------------------------------------------------------------------------
# File upload — multipart POST into the user's inputs subfolder.
# Declared BEFORE the dynamic /imports/{filename} routes so the static
# /imports/upload path matches first.
# ---------------------------------------------------------------------------


import re as _re
import secrets as _secrets

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB cap — payroll exports are tiny
ALLOWED_UPLOAD_SUFFIX = ".txt"
# Conservative allowlist: letters, digits, dot, hyphen, underscore, space,
# parentheses. Anything else is rejected up-front so the OS-level filename
# stays safe and predictable.
_UPLOAD_NAME_RE = _re.compile(r"^[A-Za-z0-9._\- ()]+$")


def _sanitize_upload_filename(name: str) -> str:
    """Strip any directory part and validate against the allowlist. Raises
    HTTPException(400) on disallowed input."""
    base = Path(name or "").name
    if not base or base in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if Path(base).suffix.lower() != ALLOWED_UPLOAD_SUFFIX:
        raise HTTPException(
            status_code=400,
            detail=f"Only {ALLOWED_UPLOAD_SUFFIX} files are accepted.",
        )
    if not _UPLOAD_NAME_RE.match(base):
        raise HTTPException(
            status_code=400,
            detail=(
                "Filename can contain letters, digits, spaces, dots, "
                "hyphens, underscores, and parentheses only."
            ),
        )
    return base


def _upload_stash_dir(user_id: int) -> Path:
    """Hidden subdirectory under the user's inputs folder where pending
    uploads wait for an overwrite confirmation. Files in here are not
    listed on /imports."""
    d = _user_inputs_dir(user_id) / ".uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _validate_upload_content(path: Path) -> None:
    """Confirm a freshly-uploaded file at least has a recognisable header
    row. NeedsHeaderMapping is fine here — the user can map columns later;
    that's the whole point of the mapping flow. Anything else (no header
    row, decode failure, etc.) is hard junk; raise ValueError."""
    try:
        peek_pps_element(path)
    except NeedsHeaderMapping:
        return
    # peek_pps_element raises on no header row / no data rows; let those
    # propagate as-is so the caller can show the actual message.


def _render_upload_success(
    request: Request, user_id: int, message: str
) -> HTMLResponse:
    """Success response: result alert into #upload-result (innerHTML swap) +
    OOB refresh of the full #files-list so the new card appears at once."""
    with SessionLocal() as session:
        user_dir = _user_inputs_dir(user_id)
        entries: list[FileEntry] = []
        for p in sorted(user_dir.glob("*.txt")):
            entries.append(_build_entry(session, user_id, p))
    result_html = templates.get_template("_upload_result.html").render(
        {"request": request, "message": message, "error": False}
    )
    files_html = templates.get_template("_files_list.html").render(
        {"request": request, "entries": entries, "oob": True}
    )
    return HTMLResponse(result_html + files_html)


@app.post("/imports/upload", response_class=HTMLResponse)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
) -> HTMLResponse:
    safe_name = _sanitize_upload_filename(file.filename or "")
    # Cap at the limit + 1 byte so we can detect oversize without slurping
    # the whole thing.
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return templates.TemplateResponse(
            request, "_upload_result.html",
            {
                "message": (
                    f"File too large — max {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
                ),
                "error": True,
            },
        )
    if not data:
        return templates.TemplateResponse(
            request, "_upload_result.html",
            {"message": "Empty file.", "error": True},
        )

    with SessionLocal() as session:
        user = _current_user(session, request)
        user_dir = _user_inputs_dir(user.id)
        dest = user_dir / safe_name

        # Stash to a temp file first so we can validate without clobbering.
        stash_dir = _upload_stash_dir(user.id)
        token = _secrets.token_hex(8)
        stash_path = stash_dir / f"{token}.upload"
        stash_path.write_bytes(data)
        try:
            _validate_upload_content(stash_path)
        except Exception as exc:
            stash_path.unlink(missing_ok=True)
            return templates.TemplateResponse(
                request, "_upload_result.html",
                {"message": f"File rejected: {exc}", "error": True},
            )

        if dest.exists():
            # Conflict — keep the stash and ask the user to confirm overwrite.
            return templates.TemplateResponse(
                request, "_upload_confirm.html",
                {"filename": safe_name, "token": token},
            )

        stash_path.replace(dest)
        uid = user.id
    return _render_upload_success(
        request, uid, message=f"Uploaded {safe_name}."
    )


@app.post("/imports/upload/{token}/confirm", response_class=HTMLResponse)
def upload_confirm(
    request: Request, token: str, filename: str = Form(...)
) -> HTMLResponse:
    """Finalize a stashed upload (overwriting the existing file)."""
    safe_name = _sanitize_upload_filename(filename)
    if not _re.fullmatch(r"[a-f0-9]{16}", token):
        raise HTTPException(status_code=400, detail="Invalid token.")
    with SessionLocal() as session:
        user = _current_user(session, request)
        stash_path = _upload_stash_dir(user.id) / f"{token}.upload"
        if not stash_path.exists():
            return templates.TemplateResponse(
                request, "_upload_result.html",
                {
                    "message": "Upload expired — please pick the file again.",
                    "error": True,
                },
            )
        dest = _user_inputs_dir(user.id) / safe_name
        stash_path.replace(dest)
        uid = user.id
    return _render_upload_success(
        request, uid, message=f"Replaced {safe_name}."
    )


@app.delete("/imports/upload/{token}", response_class=HTMLResponse)
def upload_cancel(request: Request, token: str) -> HTMLResponse:
    """Discard a stashed upload (user clicked Cancel on the confirm
    dialog)."""
    if not _re.fullmatch(r"[a-f0-9]{16}", token):
        raise HTTPException(status_code=400, detail="Invalid token.")
    with SessionLocal() as session:
        user = _current_user(session, request)
        stash_path = _upload_stash_dir(user.id) / f"{token}.upload"
        stash_path.unlink(missing_ok=True)
    return HTMLResponse("")


@app.post("/imports/{filename}", response_class=HTMLResponse)
async def do_import(
    request: Request,
    filename: str,
    name: Optional[str] = Form(default=None),
    mode: str = Form(default="add"),
    selective: Optional[str] = Form(default=None),
) -> HTMLResponse:
    if mode not in ("add", "replace"):
        raise HTTPException(status_code=400, detail="Invalid mode.")

    new_fingerprints: Optional[set[str]] = None
    missing_ids: Optional[set[int]] = None
    if selective:
        # Pull repeated checkbox fields from the raw form. FastAPI's Form()
        # binds only single values; getlist() is what we need here.
        form = await request.form()
        new_fingerprints = {v for v in form.getlist("new_fp") if v}
        missing_ids = set()
        for v in form.getlist("missing_id"):
            try:
                missing_ids.add(int(v))
            except (TypeError, ValueError):
                continue

    with SessionLocal() as session:
        user = _current_user(session, request)
        path = _resolve_user_file(user.id, filename)

        resolved_name: Optional[str] = name.strip() if name else None

        def name_resolver(pps: str) -> str:
            # If the page sent a name, use it. Otherwise we have nothing
            # to fall back on — surface the need-name state in the row.
            if resolved_name:
                return resolved_name
            raise _NeedsName(pps)

        overrides, saved, blocking = _resolve_for_file(session, user.id, path)
        if blocking is not None:
            # Headers don't resolve; surface the mapping prompt instead of
            # erroring out — the user couldn't have known until they clicked.
            entry = _build_entry(session, user.id, path)
            return templates.TemplateResponse(
                request, "_file_card.html", {"entry": entry}
            )
        if saved is not None:
            _bump_mapping_use(session, saved)

        try:
            result = import_file(
                session=session,
                path=path,
                user_id=user.id,
                name_resolver=name_resolver,
                mode=mode,
                new_fingerprints=new_fingerprints,
                missing_ids=missing_ids,
                overrides=overrides,
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


@app.get("/imports/{filename}/analyze", response_class=HTMLResponse)
def analyze_import(request: Request, filename: str) -> HTMLResponse:
    """Return an analysis panel showing the diff vs the DB for the file's
    date range. No DB writes."""
    from app.importer import analyze_file

    with SessionLocal() as session:
        user = _current_user(session, request)
        path = _resolve_user_file(user.id, filename)
        overrides, saved, blocking = _resolve_for_file(session, user.id, path)
        if blocking is not None:
            # Can't analyze without a working header map. Surface the prompt
            # in the analyze slot so the user can map and try again.
            return templates.TemplateResponse(
                request,
                "_import_analysis.html",
                {
                    "filename": filename,
                    "error": (
                        "This file's headers can't be mapped yet — open the "
                        "Map columns dialog above to assign the unrecognised "
                        "columns."
                    ),
                    "analysis": None,
                },
            )
        try:
            analysis = analyze_file(session, path, user.id, overrides=overrides)
        except ValueError as exc:
            return templates.TemplateResponse(
                request,
                "_import_analysis.html",
                {"filename": filename, "error": str(exc), "analysis": None},
            )

    return templates.TemplateResponse(
        request,
        "_import_analysis.html",
        {
            "filename": filename,
            "analysis": analysis,
            "error": None,
        },
    )


def _bump_mapping_use(session: Session, mapping: HeaderMapping) -> None:
    """Track when a saved mapping is applied. Cheap — incremented inside the
    same session that's about to commit for the import."""
    mapping.use_count = (mapping.use_count or 0) + 1
    mapping.last_used_at = datetime.utcnow()


# ---------------------------------------------------------------------------
# Header mapping endpoints
# ---------------------------------------------------------------------------


# Stable list shown in the mapping dropdown — canonical label paired with
# the internal field name; required fields surface first.
_FIELD_OPTIONS: list[tuple[str, str, bool]] = [
    (field, canonical, field in {
        "pps_element", "document_number", "account_code", "amount",
        "posting_date",
    })
    for canonical, field in COLUMN_MAP.items()
]


def _mapping_context(
    session: Session,
    user_id: int,
    filename: str,
    path: Path,
) -> dict:
    """Build the template context for the mapping panel. Detects the file's
    current unrecognized headers, any existing saved mapping, and the
    suggestions to pre-fill the dropdowns."""
    try:
        resolution = parse_header(path)
        # File parses without help — there's nothing to map. Caller should
        # generally not have reached this state; we return a "nothing to do"
        # marker so the template can show it.
        return {
            "filename": filename,
            "entry_id": path.name.replace(".", "_").replace(" ", "_"),
            "resolution": resolution,
            "saved": None,
            "field_options": _FIELD_OPTIONS,
            "preselected": {},
            "nothing_to_map": True,
        }
    except NeedsHeaderMapping as exc:
        resolution = exc.resolution
    saved = _lookup_saved_mapping(session, user_id, resolution.signature)
    # Pre-fill: saved mapping wins; otherwise use suggestions.
    preselected: dict[str, str] = {}
    if saved is not None:
        preselected.update(saved.mapping or {})
    for k, v in resolution.suggested.items():
        preselected.setdefault(k, v)
    return {
        "filename": filename,
        "entry_id": path.name.replace(".", "_").replace(" ", "_"),
        "resolution": resolution,
        "saved": saved,
        "field_options": _FIELD_OPTIONS,
        "preselected": preselected,
        "nothing_to_map": False,
    }


@app.get("/imports/{filename}/card", response_class=HTMLResponse)
def render_file_card(request: Request, filename: str) -> HTMLResponse:
    """Re-render a single file card. Used by Cancel buttons in the mapping
    panel to swap the panel back to whatever state the card was in."""
    with SessionLocal() as session:
        user = _current_user(session, request)
        path = _resolve_user_file(user.id, filename)
        entry = _build_entry(session, user.id, path)
    return templates.TemplateResponse(
        request, "_file_card.html", {"entry": entry}
    )


@app.get("/imports/{filename}/mapping", response_class=HTMLResponse)
def show_mapping_panel(request: Request, filename: str) -> HTMLResponse:
    """Render the header mapping panel for a file. Used both when the file's
    headers don't auto-resolve (initial mapping) and when the user clicks
    Edit on a saved mapping."""
    with SessionLocal() as session:
        user = _current_user(session, request)
        path = _resolve_user_file(user.id, filename)
        ctx = _mapping_context(session, user.id, filename, path)
    return templates.TemplateResponse(request, "_header_mapping.html", ctx)


@app.post("/imports/{filename}/mapping", response_class=HTMLResponse)
async def save_mapping(request: Request, filename: str) -> HTMLResponse:
    """Save a header mapping for the file's signature. Form fields:
    `signature` (the resolution's signature) and one `map:<file_header>`
    field per unrecognized header. Returns the re-rendered file card."""
    form = await request.form()
    signature = (form.get("signature") or "").strip()
    if not signature:
        raise HTTPException(status_code=400, detail="Missing signature.")
    mapping: dict[str, str] = {}
    for k, v in form.multi_items():
        if not k.startswith("map:"):
            continue
        file_header = k[4:]
        field = (v or "").strip()
        if field and field in ALL_FIELDS:
            mapping[file_header] = field
    if not mapping:
        raise HTTPException(
            status_code=400, detail="At least one column must be mapped."
        )

    with SessionLocal() as session:
        user = _current_user(session, request)
        path = _resolve_user_file(user.id, filename)
        existing = _lookup_saved_mapping(session, user.id, signature)
        if existing is not None:
            existing.mapping = mapping
        else:
            session.add(
                HeaderMapping(
                    user_id=user.id, signature=signature, mapping=mapping
                )
            )
        session.commit()
        entry = _build_entry(session, user.id, path)
    return templates.TemplateResponse(
        request, "_file_card.html", {"entry": entry}
    )


@app.delete("/imports/mappings/{mapping_id}", response_class=HTMLResponse)
def forget_mapping(
    request: Request,
    mapping_id: int,
    filename: Optional[str] = None,
) -> HTMLResponse:
    """Delete a saved mapping. If `filename` is given, return the
    re-rendered file card so the user sees the new state inline; otherwise
    return empty (the caller HTMX-swaps out the row, e.g. on /profile)."""
    with SessionLocal() as session:
        user = _current_user(session, request)
        mapping = session.get(HeaderMapping, mapping_id)
        if mapping is not None and mapping.user_id == user.id:
            session.delete(mapping)
            session.commit()
        if filename:
            user_dir = _user_inputs_dir(user.id)
            path = user_dir / filename
            if path.exists() and path.is_file() and path.parent == user_dir:
                entry = _build_entry(session, user.id, path)
                return templates.TemplateResponse(
                    request, "_file_card.html", {"entry": entry}
                )
    return HTMLResponse("")


@app.delete("/imports/{filename}", response_class=HTMLResponse)
def delete_file(request: Request, filename: str) -> HTMLResponse:
    """Remove an unimported file from the user's inputs folder. Used by the
    trash button on a file card to discard an upload before it's been
    imported. Refuses to delete a file that's already been imported — the
    audit row would point at a missing file and the user should instead
    Forget the audit entry."""
    with SessionLocal() as session:
        user = _current_user(session, request)
        path = _resolve_user_file(user.id, filename)
        from app.importer import sha256_file
        file_hash = sha256_file(path)
        already_imported = session.scalar(
            select(ImportRun)
            .where(
                ImportRun.user_id == user.id,
                ImportRun.file_sha256 == file_hash,
            )
            .limit(1)
        )
        if already_imported is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This file is already imported — remove the audit entry "
                    "from Recent imports first, then delete the file."
                ),
            )
        path.unlink()
    # Empty body + outerHTML on the file card removes it from the page.
    return HTMLResponse("")


@app.delete("/imports/runs/{run_id}", response_class=HTMLResponse)
def delete_import_run(request: Request, run_id: int) -> HTMLResponse:
    """Remove a single ImportRun audit entry. Transactions inserted by that
    run are NOT removed — the run is bookkeeping only. Users use this to tidy
    up rows whose source file is no longer in inputs."""
    with SessionLocal() as session:
        user = _current_user(session, request)
        run = session.get(ImportRun, run_id)
        if run is None or run.user_id != user.id:
            # Treat missing/foreign rows as already-gone for the UI's purposes;
            # HTMX will just remove the row.
            return HTMLResponse("")
        session.delete(run)
        session.commit()
    # Empty body + outerHTML swap on the <tr> removes the row from the table.
    return HTMLResponse("")


class _NeedsName(Exception):
    def __init__(self, pps_element: str):
        super().__init__(f"Need name for new project {pps_element!r}")
        self.pps_element = pps_element


# ---------------------------------------------------------------------------
# Transactions page
# ---------------------------------------------------------------------------


def _accessible_project_ids(session: Session, user_id: int) -> list[int]:
    """Projects this user can see — owned plus any shared with them."""
    return list(
        session.scalars(
            select(Project.id)
            .outerjoin(
                ProjectShare, ProjectShare.project_id == Project.id
            )
            .where(
                or_(
                    Project.owner_user_id == user_id,
                    ProjectShare.user_id == user_id,
                )
            )
            .distinct()
        )
    )


def _accessible_projects(session: Session, user_id: int) -> list[Project]:
    """List of accessible Project objects, ordered by name."""
    ids = _accessible_project_ids(session, user_id)
    if not ids:
        return []
    return list(
        session.scalars(
            select(Project).where(Project.id.in_(ids)).order_by(Project.name)
        )
    )


def _load_accessible_project(
    session: Session, user_id: int, project_id: int
) -> Project:
    """Load one project the user can access. 404 otherwise."""
    project = session.get(Project, project_id)
    if project is None or project.id not in _accessible_project_ids(
        session, user_id
    ):
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _is_owner(project: Project, user: User) -> bool:
    return project.owner_user_id == user.id


def _require_owner(project: Project, user: User) -> None:
    if not _is_owner(project, user):
        raise HTTPException(
            status_code=403,
            detail="Only the project owner can do this.",
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

        projects = _accessible_projects(session, user.id)
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
        projects = _accessible_projects(session, user.id)
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

    project = _load_accessible_project(session, user_id, project_id)

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
        owner = session.get(User, project.owner_user_id)
        share_rows = session.execute(
            select(ProjectShare, User)
            .join(User, User.id == ProjectShare.user_id)
            .where(ProjectShare.project_id == project.id)
            .order_by(User.email)
        ).all()
        shares = [
            {
                "user_id": u.id,
                "email": u.email,
                "name": u.name,
                "granted_at": share.granted_at,
                "is_me": u.id == user.id,
            }
            for share, u in share_rows
        ]
        # Users we could still share with: anyone who isn't the owner and
        # isn't already shared with this project.
        excluded_ids = {project.owner_user_id} | {s["user_id"] for s in shares}
        shareable = list(
            session.scalars(
                select(User)
                .where(User.id.not_in(excluded_ids))
                .order_by(User.name, User.email)
            )
        )
        return templates.TemplateResponse(
            request,
            "project_settings.html",
            {
                "project": _ProjectSettingsView(
                    id=project.id,
                    name=project.name,
                    pps_element=project.pps_element,
                    description=project.description,
                    start_date=project.start_date,
                    end_date=project.end_date,
                    annual_data=annual_views,
                ),
                "is_owner": _is_owner(project, user),
                "owner_email": owner.email if owner else None,
                "owner_name": owner.name if owner else None,
                "shares": shares,
                "shareable_users": shareable,
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
    start_date: date | None
    end_date: date | None
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
        project = _load_accessible_project(session, user.id, project_id)

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

            start_raw = (form.get("start_date") or "").strip()
            end_raw = (form.get("end_date") or "").strip()
            project.start_date = _parse_iso_date(start_raw) if start_raw else None
            project.end_date = _parse_iso_date(end_raw) if end_raw else None
            if (
                project.start_date
                and project.end_date
                and project.start_date > project.end_date
            ):
                raise ValueError("Project start date is after end date.")

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
        project = _load_accessible_project(session, user.id, project_id)

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

        grid = per_person_month(session, project.id, selected_year)

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
    student: str = "",
) -> HTMLResponse:
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="month must be 1..12")

    from app.analytics import MONTHS_SHORT, build_employee_name_map, _extract_student_name

    with SessionLocal() as session:
        user = _current_user(session, request)
        project = _load_accessible_project(session, user.id, project_id)

        where_clauses = [
            Transaction.project_id == project.id,
            func.extract("year", Transaction.posting_date) == year,
            func.extract("month", Transaction.posting_date) == month,
        ]
        if student:
            # Student rows: employee is null, text is "ŠS <student>".
            where_clauses.append(Transaction.employee.is_(None))
            where_clauses.append(Transaction.text.like("ŠS %"))
        elif employee:
            where_clauses.append(Transaction.employee == employee)
        else:
            where_clauses.append(Transaction.employee.is_(None))

        candidates = list(
            session.scalars(
                select(Transaction)
                .where(*where_clauses)
                .order_by(Transaction.posting_date, Transaction.id)
            )
        )
        # For student requests the SQL filter matches any ŠS row; pin it
        # down to the exact student name in Python (regex-via-strip).
        if student:
            txns = [t for t in candidates if _extract_student_name(t.text) == student]
        else:
            txns = candidates

        names = build_employee_name_map(session, project_id) if employee else {}

    total = sum((t.amount for t in txns), start=Decimal("0"))
    if student:
        display = f"{student} (student)"
    elif employee:
        display = (
            f"{names[employee]} ({employee})"
            if employee in names
            else f"employee {employee}"
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
        project = _load_accessible_project(session, user.id, project_id)

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
        mappings = list(
            session.scalars(
                select(HeaderMapping)
                .where(HeaderMapping.user_id == user.id)
                .order_by(HeaderMapping.created_at.desc())
            )
        )
        return templates.TemplateResponse(
            request,
            "profile.html",
            {
                "user": user,
                "saved": bool(saved),
                "error": error or None,
                "header_mappings": mappings,
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


# ---------------------------------------------------------------------------
# Project sharing — grant / revoke read+edit access for another user
# ---------------------------------------------------------------------------


@app.post("/projects/{project_id}/share")
def share_project(
    request: Request,
    project_id: int,
    email: str = Form(...),
) -> RedirectResponse:
    from urllib.parse import quote

    email = email.strip().lower()
    with SessionLocal() as session:
        actor = _current_user(session, request)
        project = _load_accessible_project(session, actor.id, project_id)
        _require_owner(project, actor)  # only the owner can grant access

        target = session.scalar(select(User).where(User.email == email))
        if target is None:
            return RedirectResponse(
                f"/projects/{project_id}/settings"
                f"?error={quote('No user with that email. Create them with fiori user create first.')}",
                status_code=303,
            )
        if target.id == actor.id:
            return RedirectResponse(
                f"/projects/{project_id}/settings"
                f"?error={quote('You already own this project; no need to share with yourself.')}",
                status_code=303,
            )
        existing = session.scalar(
            select(ProjectShare).where(
                ProjectShare.project_id == project.id,
                ProjectShare.user_id == target.id,
            )
        )
        if existing is None:
            session.add(
                ProjectShare(
                    project_id=project.id,
                    user_id=target.id,
                    granted_by=actor.id,
                )
            )
            session.commit()
    return RedirectResponse(
        f"/projects/{project_id}/settings?saved=1", status_code=303
    )


@app.post("/projects/{project_id}/unshare")
def unshare_project(
    request: Request,
    project_id: int,
    user_id: int = Form(...),
) -> RedirectResponse:
    with SessionLocal() as session:
        actor = _current_user(session, request)
        project = _load_accessible_project(session, actor.id, project_id)
        _require_owner(project, actor)
        session.execute(
            ProjectShare.__table__.delete().where(
                ProjectShare.project_id == project.id,
                ProjectShare.user_id == user_id,
            )
        )
        session.commit()
    return RedirectResponse(
        f"/projects/{project_id}/settings?saved=1", status_code=303
    )


# ---------------------------------------------------------------------------
# /people — cross-project per-person × month grid
# ---------------------------------------------------------------------------


from fastapi import Query  # local-style import to keep changes scoped


@app.get("/people", response_class=HTMLResponse)
def people_page(
    request: Request,
    project_id: list[int] = Query(default=None),
    year: Optional[int] = None,
) -> HTMLResponse:
    from app.analytics import MONTHS_SHORT, per_person_month

    with SessionLocal() as session:
        user = _current_user(session, request)
        all_projects = _accessible_projects(session, user.id)
        accessible_ids = [p.id for p in all_projects]

        # Default to all accessible when nothing is selected.
        if project_id is None:
            selected_ids = list(accessible_ids)
        else:
            # Drop any IDs the user doesn't actually have access to.
            selected_ids = [i for i in project_id if i in accessible_ids]

        # Years that have data anywhere in the user's accessible projects.
        if accessible_ids:
            available_years = sorted(
                {
                    int(y)
                    for y in session.scalars(
                        select(
                            func.distinct(
                                func.extract("year", Transaction.posting_date)
                            )
                        )
                        .where(Transaction.project_id.in_(accessible_ids))
                    )
                    if y is not None
                },
                reverse=True,
            )
        else:
            available_years = []

        if year not in available_years:
            year = available_years[0] if available_years else None

        grid = (
            per_person_month(session, selected_ids, year)
            if year is not None and selected_ids
            else None
        )

    context = {
        "all_projects": all_projects,
        "selected_project_ids": selected_ids,
        "available_years": available_years,
        "year": year,
        "grid": grid,
        "month_names": MONTHS_SHORT if grid is not None else [],
    }
    template = (
        "_people_results.html"
        if request.headers.get("HX-Request")
        else "people.html"
    )
    return templates.TemplateResponse(request, template, context)


@app.get("/people/cell", response_class=HTMLResponse)
def people_cell(
    request: Request,
    year: int,
    month: int,
    employee: str = "",
    student: str = "",
    project_id: list[int] = Query(default=None),
) -> HTMLResponse:
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="month must be 1..12")
    if not employee and not student:
        raise HTTPException(status_code=400, detail="employee or student required")

    from app.analytics import MONTHS_SHORT, build_employee_name_map, _extract_student_name

    with SessionLocal() as session:
        user = _current_user(session, request)
        accessible_ids = _accessible_project_ids(session, user.id)
        selected_ids = [i for i in (project_id or []) if i in accessible_ids]
        if not selected_ids:
            raise HTTPException(status_code=400, detail="no projects selected")

        base_where = [
            Transaction.project_id.in_(selected_ids),
            func.extract("year", Transaction.posting_date) == year,
            func.extract("month", Transaction.posting_date) == month,
        ]
        if student:
            base_where.append(Transaction.employee.is_(None))
            base_where.append(Transaction.text.like("ŠS %"))
        else:
            base_where.append(Transaction.employee == employee)

        rows = list(
            session.execute(
                select(Transaction, Project.name, Project.pps_element)
                .join(Project, Project.id == Transaction.project_id)
                .where(*base_where)
                .order_by(Transaction.posting_date, Transaction.id)
            ).all()
        )
        if student:
            rows = [
                r for r in rows if _extract_student_name(r[0].text) == student
            ]
        names = build_employee_name_map(session, selected_ids) if employee else {}

    total = sum((t.amount for t, _, _ in rows), start=Decimal("0"))
    if student:
        name_part = f"{student} (student)"
    else:
        name_part = (
            f"{names[employee]} ({employee})"
            if employee in names
            else f"employee {employee}"
        )
    heading = f"{MONTHS_SHORT[month - 1]} {year} — {name_part}"

    return templates.TemplateResponse(
        request,
        "_people_cell_expansion.html",
        {
            "rows": rows,
            "txns": [t for t, _, _ in rows],
            "total": total,
            "heading": heading,
        },
    )
