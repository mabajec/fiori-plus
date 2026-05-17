from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterator

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import ImportRun, Project, Transaction


HEADER_FIRST_CELL = "PPS element"
DEFAULT_ENCODING = "mac_latin2"

# Source-system column header → field name we use internally.
COLUMN_MAP: dict[str, str] = {
    "PPS element": "pps_element",
    "Štev. dok.": "document_number",
    "Kto GK": "account_code",
    "Dolgi tekst konta GK": "account_text",
    "Znes.v DV": "amount",
    "KadrŠt": "employee",
    "Tekst": "text",
    "Dat.knj.": "posting_date",
    "Vir fin.": "source",
    "Leto": "year",
}

REQUIRED_FIELDS = {
    "pps_element",
    "document_number",
    "account_code",
    "amount",
    "posting_date",
}

ALL_FIELDS = set(COLUMN_MAP.values())


@dataclass
class ImportResult:
    project_id: int
    project_name: str
    pps_element: str
    rows_imported: int
    rows_skipped: int
    rows_deleted: int
    mode: str               # "add" or "replace"
    file_sha256: str
    duplicate_file: bool


@dataclass
class ImportAnalysis:
    """Diff between a file and what the DB already has, for the file's
    own date range. No side effects — purely informational."""
    project_id: int
    project_name: str
    pps_element: str
    file_sha256: str
    period_start: date
    period_end: date
    new_records: list[dict]              # in file, not in DB
    existing_records: list[dict]         # in both (natural key matches)
    missing_records: list[dict]          # in DB for period, not in file


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_amount(s: str) -> Decimal:
    # Slovenian format: "1.234,56" — dots = thousands, comma = decimal.
    return Decimal(s.strip().replace(".", "").replace(",", "."))


def parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unparseable date: {s!r}")


def _build_field_map(header_cells: list[str]) -> dict[int, str]:
    """Map column position → field name based on the header row."""
    field_map: dict[int, str] = {}
    for idx, cell in enumerate(header_cells):
        name = cell.strip()
        if name in COLUMN_MAP:
            field_map[idx] = COLUMN_MAP[name]
    missing = REQUIRED_FIELDS - set(field_map.values())
    if missing:
        raise ValueError(
            f"Header row missing required columns: {sorted(missing)}"
        )
    return field_map


def peek_pps_element(path: Path, encoding: str = DEFAULT_ENCODING) -> str:
    """Return the PPS element from the first data row, without parsing the rest.

    Used by the UI to decide whether the project is already known (no name
    prompt needed) before kicking off a full import.
    """
    for rec in read_records(path, encoding=encoding):
        pps = rec.get("pps_element")
        if pps:
            return pps
    raise ValueError(f"No data rows found in {path}.")


def read_footer_total(path: Path, encoding: str = DEFAULT_ENCODING) -> Decimal:
    """Return the total amount from the `*` footer row.

    Every export from the source system ends with a `* | ... | total` row.
    Comparing that figure against the sum of parsed rows is our safety net
    against silent parsing or encoding errors.
    """
    with open(path, encoding=encoding) as f:
        text = f.read()
    for raw in text.splitlines():
        if not raw.strip():
            continue
        cells = raw.split("\t")
        while cells and cells[0] == "":
            cells.pop(0)
        if not cells:
            continue
        if cells[0].strip() == "*":
            for cell in reversed(cells):
                stripped = cell.strip()
                if stripped and stripped != "*":
                    return parse_amount(stripped)
            raise ValueError(
                f"`*` footer in {path} has no total amount."
            )
    raise ValueError(
        f"No `*` totals footer in {path}. Every export from the source "
        f"system should include one — refusing to import."
    )


def read_records(
    path: Path, encoding: str = DEFAULT_ENCODING
) -> Iterator[dict]:
    """Yield one dict per data row, with string values mapped by column name.

    Handles the two known source-system layouts (10-column full and 7-column
    compact) and any future column reorderings, because field positions are
    resolved from the actual header row in each file. Skips preamble lines
    before the header and stops at a `*` totals footer.
    """
    with open(path, encoding=encoding) as f:
        text = f.read()

    field_map: dict[int, str] | None = None

    for raw in text.splitlines():
        if not raw.strip():
            continue
        cells = raw.split("\t")
        while cells and cells[0] == "":
            cells.pop(0)
        if not cells:
            continue
        first = cells[0].strip()

        if field_map is None:
            if first == HEADER_FIRST_CELL:
                field_map = _build_field_map(cells)
            continue

        if first == "*":
            break

        rec: dict = {f: None for f in ALL_FIELDS}
        for idx, name in field_map.items():
            if idx < len(cells):
                rec[name] = cells[idx].strip() or None
        yield rec

    if field_map is None:
        raise ValueError(
            f"No column header row (starting with {HEADER_FIRST_CELL!r}) found "
            f"in {path}."
        )


def parse_record(rec: dict) -> dict:
    out = dict(rec)
    out["amount"] = parse_amount(rec["amount"])
    out["posting_date"] = parse_date(rec["posting_date"])
    if rec.get("year") is not None:
        try:
            out["year"] = int(rec["year"])
        except (TypeError, ValueError):
            out["year"] = None
    return out


def ensure_project(
    session: Session,
    owner_user_id: int,
    pps_element: str,
    name_resolver: Callable[[str], str],
) -> Project:
    proj = session.scalar(
        select(Project).where(
            Project.owner_user_id == owner_user_id,
            Project.pps_element == pps_element,
        )
    )
    if proj is not None:
        return proj
    name = name_resolver(pps_element).strip()
    if not name:
        raise ValueError("Project name cannot be empty.")
    proj = Project(
        owner_user_id=owner_user_id, pps_element=pps_element, name=name
    )
    session.add(proj)
    session.flush()
    return proj


def _natural_key(rec) -> tuple:
    """The tuple matching the UNIQUE constraint on transactions. Works on
    either a parsed dict or a Transaction ORM row."""
    if isinstance(rec, dict):
        return (
            rec["document_number"],
            rec["account_code"],
            rec["amount"],
            rec["posting_date"],
            rec.get("employee"),
            rec.get("text"),
        )
    return (
        rec.document_number,
        rec.account_code,
        rec.amount,
        rec.posting_date,
        rec.employee,
        rec.text,
    )


def _record_to_dict(t: Transaction) -> dict:
    return {
        "document_number": t.document_number,
        "account_code": t.account_code,
        "account_text": t.account_text,
        "amount": t.amount,
        "posting_date": t.posting_date,
        "employee": t.employee,
        "text": t.text,
        "source": t.source,
        "year": t.year,
    }


def _parse_and_validate(
    path: Path, encoding: str
) -> tuple[str, list[dict], str, date, date]:
    """Parse + validate a file. Returns (file_hash, records, pps_element,
    period_start, period_end). Raises ValueError on any structural problem
    or footer-total mismatch."""
    file_hash = sha256_file(path)
    records = [parse_record(r) for r in read_records(path, encoding=encoding)]
    if not records:
        raise ValueError("File contains no data rows.")

    footer_total = read_footer_total(path, encoding=encoding)
    parsed_total = sum((r["amount"] for r in records), start=Decimal("0"))
    if parsed_total != footer_total:
        raise ValueError(
            f"This file is internally inconsistent: rows sum to "
            f"{parsed_total} but the footer reports {footer_total} "
            f"(delta {parsed_total - footer_total}). Re-export from the "
            f"source system or check whether the file was modified."
        )

    pps_element = records[0]["pps_element"]
    for r in records:
        if r["pps_element"] != pps_element:
            raise ValueError(
                f"File contains multiple PPS elements: {pps_element!r} and "
                f"{r['pps_element']!r}. One file must cover one project."
            )

    period_start = min(r["posting_date"] for r in records)
    period_end = max(r["posting_date"] for r in records)
    return file_hash, records, pps_element, period_start, period_end


def import_file(
    session: Session,
    path: Path,
    user_id: int,
    name_resolver: Callable[[str], str],
    encoding: str = DEFAULT_ENCODING,
    mode: str = "add",
) -> ImportResult:
    if mode not in ("add", "replace"):
        raise ValueError(f"Unknown mode {mode!r}; expected 'add' or 'replace'.")

    file_hash, records, pps_element, period_start, period_end = _parse_and_validate(
        path, encoding
    )

    # For "add", skip if we've already ingested this exact file. For "replace"
    # the user is asking to overwrite, so re-running on the same file is fine.
    if mode == "add":
        prior = session.scalar(
            select(ImportRun)
            .where(
                ImportRun.file_sha256 == file_hash,
                ImportRun.user_id == user_id,
                ImportRun.mode == "add",
            )
            .order_by(ImportRun.imported_at.desc())
        )
        if prior is not None:
            proj = (
                session.get(Project, prior.project_id) if prior.project_id else None
            )
            return ImportResult(
                project_id=prior.project_id or 0,
                project_name=proj.name if proj else "<deleted>",
                pps_element=proj.pps_element if proj else "",
                rows_imported=0,
                rows_skipped=0,
                rows_deleted=0,
                mode="add",
                file_sha256=file_hash,
                duplicate_file=True,
            )

    project = ensure_project(session, user_id, pps_element, name_resolver)

    rows_deleted = 0
    if mode == "replace":
        # Delete the project's existing rows in the file's date range; outside
        # that range is left alone so partial-year exports don't nuke other
        # months.
        from sqlalchemy import delete as sql_delete

        result = session.execute(
            sql_delete(Transaction).where(
                Transaction.project_id == project.id,
                Transaction.posting_date.between(period_start, period_end),
            )
        )
        rows_deleted = result.rowcount or 0
        session.flush()

    # Python-side dedup against rows already in the period. Postgres's UNIQUE
    # treats NULL as distinct, so the natural-key constraint wouldn't catch a
    # NULL-employee row that's logically identical to an existing one —
    # ON CONFLICT alone would let it duplicate. We use the same _natural_key
    # tuple as analyze does, which keeps the two consistent.
    existing_db = list(
        session.scalars(
            select(Transaction).where(
                Transaction.project_id == project.id,
                Transaction.posting_date.between(period_start, period_end),
            )
        )
    )
    seen_keys: set[tuple] = {_natural_key(t) for t in existing_db}

    rows_imported = 0
    for rec in records:
        key = _natural_key(rec)
        if key in seen_keys:
            continue
        values = {k: v for k, v in rec.items() if k != "pps_element"}
        values["project_id"] = project.id
        session.add(Transaction(**values))
        seen_keys.add(key)
        rows_imported += 1
    rows_skipped = len(records) - rows_imported

    run = ImportRun(
        user_id=user_id,
        project_id=project.id,
        filename=str(path.name),
        file_sha256=file_hash,
        mode=mode,
        rows_imported=rows_imported,
        rows_skipped=rows_skipped,
        rows_deleted=rows_deleted,
    )
    session.add(run)
    session.commit()

    return ImportResult(
        project_id=project.id,
        project_name=project.name,
        pps_element=project.pps_element,
        rows_imported=rows_imported,
        rows_skipped=rows_skipped,
        rows_deleted=rows_deleted,
        mode=mode,
        file_sha256=file_hash,
        duplicate_file=False,
    )


def analyze_file(
    session: Session,
    path: Path,
    user_id: int,
    encoding: str = DEFAULT_ENCODING,
) -> ImportAnalysis:
    """Compute a diff between the file and the DB for the file's date range.
    No DB writes; doesn't create a project if it's new."""
    file_hash, records, pps_element, period_start, period_end = _parse_and_validate(
        path, encoding
    )

    project = session.scalar(
        select(Project).where(
            Project.owner_user_id == user_id,
            Project.pps_element == pps_element,
        )
    )

    file_by_key: dict[tuple, dict] = {_natural_key(r): r for r in records}

    if project is None:
        # Brand-new project — everything in the file is "new".
        sort_key = lambda r: (r["posting_date"], r["document_number"])
        return ImportAnalysis(
            project_id=0,
            project_name="(new project)",
            pps_element=pps_element,
            file_sha256=file_hash,
            period_start=period_start,
            period_end=period_end,
            new_records=sorted(records, key=sort_key),
            existing_records=[],
            missing_records=[],
        )

    db_rows = list(
        session.scalars(
            select(Transaction).where(
                Transaction.project_id == project.id,
                Transaction.posting_date.between(period_start, period_end),
            )
        )
    )
    db_by_key: dict[tuple, Transaction] = {_natural_key(t): t for t in db_rows}

    file_keys = set(file_by_key.keys())
    db_keys = set(db_by_key.keys())
    sort_key = lambda r: (r["posting_date"], r["document_number"])
    new_records = sorted([file_by_key[k] for k in file_keys - db_keys], key=sort_key)
    existing_records = sorted(
        [file_by_key[k] for k in file_keys & db_keys], key=sort_key
    )
    missing_records = sorted(
        [_record_to_dict(db_by_key[k]) for k in db_keys - file_keys],
        key=sort_key,
    )

    return ImportAnalysis(
        project_id=project.id,
        project_name=project.name,
        pps_element=project.pps_element,
        file_sha256=file_hash,
        period_start=period_start,
        period_end=period_end,
        new_records=new_records,
        existing_records=existing_records,
        missing_records=missing_records,
    )
