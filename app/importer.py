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
EXPECTED_COLUMNS = 10
DEFAULT_ENCODING = "mac_latin2"


@dataclass
class ImportResult:
    project_id: int
    project_name: str
    pps_element: str
    rows_imported: int
    rows_skipped: int
    file_sha256: str
    duplicate_file: bool  # True if this exact file was already imported


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_rows(path: Path, encoding: str = DEFAULT_ENCODING) -> Iterator[list[str]]:
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
        if cells[0].strip() == HEADER_FIRST_CELL:
            continue
        yield cells


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


def parse_int_or_none(s: str) -> int | None:
    s = s.strip()
    return int(s) if s else None


def parse_row(cells: list[str]) -> dict:
    if len(cells) < EXPECTED_COLUMNS:
        raise ValueError(
            f"Expected {EXPECTED_COLUMNS} columns, got {len(cells)}: {cells!r}"
        )
    return {
        "pps_element": cells[0].strip(),
        "document_number": cells[1].strip(),
        "account_code": cells[2].strip(),
        "account_text": cells[3].strip() or None,
        "amount": parse_amount(cells[4]),
        "employee": cells[5].strip() or None,
        "text": cells[6].strip() or None,
        "posting_date": parse_date(cells[7]),
        "source": cells[8].strip() or None,
        "year": parse_int_or_none(cells[9]),
    }


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

    rows_iter = read_rows(path, encoding=encoding)
    try:
        first = next(rows_iter)
    except StopIteration:
        raise ValueError("File contains no data rows.")

    first_parsed = parse_row(first)
    pps_element = first_parsed["pps_element"]
    project = ensure_project(session, user_id, pps_element, name_resolver)

    def to_record(parsed: dict) -> dict:
        rec = {k: v for k, v in parsed.items() if k != "pps_element"}
        rec["project_id"] = project.id
        return rec

    parsed_rows = [first_parsed]
    for cells in rows_iter:
        p = parse_row(cells)
        if p["pps_element"] != pps_element:
            raise ValueError(
                f"File contains multiple PPS elements: {pps_element!r} and "
                f"{p['pps_element']!r}. One file must cover one project."
            )
        parsed_rows.append(p)

    rows_imported = 0
    for parsed in parsed_rows:
        stmt = (
            pg_insert(Transaction)
            .values(**to_record(parsed))
            .on_conflict_do_nothing(
                constraint="uq_transactions_natural_key"
            )
            .returning(Transaction.id)
        )
        result = session.execute(stmt).first()
        if result is not None:
            rows_imported += 1
    rows_skipped = len(parsed_rows) - rows_imported

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
