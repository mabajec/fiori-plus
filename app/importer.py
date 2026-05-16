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
    file_sha256: str
    duplicate_file: bool


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


def import_file(
    session: Session,
    path: Path,
    user_id: int,
    name_resolver: Callable[[str], str],
    encoding: str = DEFAULT_ENCODING,
) -> ImportResult:
    file_hash = sha256_file(path)

    prior = session.scalar(
        select(ImportRun)
        .where(ImportRun.file_sha256 == file_hash, ImportRun.user_id == user_id)
        .order_by(ImportRun.imported_at.desc())
    )
    if prior is not None:
        proj = session.get(Project, prior.project_id) if prior.project_id else None
        return ImportResult(
            project_id=prior.project_id or 0,
            project_name=proj.name if proj else "<deleted>",
            pps_element=proj.pps_element if proj else "",
            rows_imported=0,
            rows_skipped=0,
            file_sha256=file_hash,
            duplicate_file=True,
        )

    records = [parse_record(r) for r in read_records(path, encoding=encoding)]
    if not records:
        raise ValueError("File contains no data rows.")

    pps_element = records[0]["pps_element"]
    for r in records:
        if r["pps_element"] != pps_element:
            raise ValueError(
                f"File contains multiple PPS elements: {pps_element!r} and "
                f"{r['pps_element']!r}. One file must cover one project."
            )

    project = ensure_project(session, user_id, pps_element, name_resolver)

    rows_imported = 0
    for rec in records:
        values = {k: v for k, v in rec.items() if k != "pps_element"}
        values["project_id"] = project.id
        stmt = (
            pg_insert(Transaction)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_transactions_natural_key")
            .returning(Transaction.id)
        )
        result = session.execute(stmt).first()
        if result is not None:
            rows_imported += 1
    rows_skipped = len(records) - rows_imported

    run = ImportRun(
        user_id=user_id,
        project_id=project.id,
        filename=str(path.name),
        file_sha256=file_hash,
        rows_imported=rows_imported,
        rows_skipped=rows_skipped,
    )
    session.add(run)
    session.commit()

    return ImportResult(
        project_id=project.id,
        project_name=project.name,
        pps_element=project.pps_element,
        rows_imported=rows_imported,
        rows_skipped=rows_skipped,
        file_sha256=file_hash,
        duplicate_file=False,
    )
