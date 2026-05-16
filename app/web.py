from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.importer import import_file, peek_pps_element
from app.models import ImportRun, Project, User


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
