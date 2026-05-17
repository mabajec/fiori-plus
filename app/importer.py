from __future__ import annotations

import difflib
import hashlib
import unicodedata
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterator

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import ImportRun, Project, Transaction


HEADER_FIRST_CELL = "PPS element"
# Native export from the source system is Mac Central European. Files
# round-tripped through Excel, LibreOffice or most text editors usually come
# back as UTF-8 (often with BOM) or, on Windows, CP1250. We try the native
# encoding first and fall back to the others so a manually-edited file still
# imports cleanly.
DEFAULT_ENCODING = "mac_latin2"
CANDIDATE_ENCODINGS: tuple[str, ...] = (
    "mac_latin2",
    "utf-8-sig",
    "utf-8",
    "cp1250",
)

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

# Reverse lookup field → canonical header label.
_CANONICAL_FOR_FIELD: dict[str, str] = {v: k for k, v in COLUMN_MAP.items()}


def _normalize_header(s: str) -> str:
    """Strip accents/combining marks, lowercase, collapse whitespace. Used
    as the forgiving comparison key — handles Unicode variants and stray
    spaces but won't silently match a different word."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


# Precomputed normalized lookup; built once at import time.
_NORMALIZED_COLUMN_MAP: dict[str, str] = {
    _normalize_header(k): v for k, v in COLUMN_MAP.items()
}


def signature_for(unrecognized_headers: list[str]) -> str:
    """Stable short hash of the sorted, normalized unrecognized headers.
    Two files whose unmappable columns share the same names (after
    normalization) share a signature — and therefore a saved mapping."""
    normalized = sorted({_normalize_header(h) for h in unrecognized_headers})
    return hashlib.sha256("\n".join(normalized).encode()).hexdigest()[:32]


def suggest_mapping(
    unrecognized: list[str], already_mapped_fields: set[str]
) -> dict[str, str]:
    """Best-effort suggestion of {file_header: field_name} for the mapping
    panel. Conservative: only suggests when similarity is above a threshold,
    and never proposes the same field twice. Compares against the canonical
    header, the field name, and a few label variants."""
    unmapped_fields = set(COLUMN_MAP.values()) - already_mapped_fields
    if not unmapped_fields:
        return {}
    field_labels: dict[str, list[str]] = {}
    for canonical, field in COLUMN_MAP.items():
        if field not in unmapped_fields:
            continue
        field_labels.setdefault(field, []).extend(
            [
                _normalize_header(canonical),
                _normalize_header(field),
                _normalize_header(field.replace("_", " ")),
            ]
        )
    suggestions: dict[str, str] = {}
    used_fields: set[str] = set()
    for h in unrecognized:
        nh = _normalize_header(h)
        best_field: str | None = None
        best_score = 0.0
        for field, labels in field_labels.items():
            if field in used_fields:
                continue
            for label in labels:
                score = difflib.SequenceMatcher(None, nh, label).ratio()
                if score > best_score:
                    best_score, best_field = score, field
        if best_field and best_score >= 0.55:
            suggestions[h] = best_field
            used_fields.add(best_field)
    return suggestions


@dataclass
class HeaderResolution:
    """Outcome of mapping a file's header row to internal field names.

    Layered match: exact COLUMN_MAP entry → user/saved override →
    normalized form (accents stripped, lowercased). `missing_required`
    being non-empty means the caller should either supply overrides or
    raise NeedsHeaderMapping for the UI to prompt."""
    field_map: dict[int, str]
    normalization_log: list[tuple[str, str]] = dc_field(default_factory=list)
    override_log: list[tuple[str, str]] = dc_field(default_factory=list)
    unrecognized: list[str] = dc_field(default_factory=list)
    missing_required: set[str] = dc_field(default_factory=set)
    suggested: dict[str, str] = dc_field(default_factory=dict)
    signature: str = ""


def resolve_header(
    header_cells: list[str], overrides: dict[str, str] | None = None
) -> HeaderResolution:
    """Map a header row to field names. Three tiers per cell:
      1. Exact match against COLUMN_MAP keys
      2. Explicit override (file_header → field_name) from a saved or
         user-supplied mapping
      3. Normalized fall-back (accents stripped, lowercase, whitespace
         collapsed)
    Anything left over goes to `unrecognized` and contributes to the
    signature."""
    overrides = overrides or {}
    field_map: dict[int, str] = {}
    normalization_log: list[tuple[str, str]] = []
    override_log: list[tuple[str, str]] = []
    unrecognized: list[str] = []
    for idx, cell in enumerate(header_cells):
        raw = cell.strip()
        if not raw:
            continue
        if raw in COLUMN_MAP:
            field_map[idx] = COLUMN_MAP[raw]
            continue
        # Explicit override beats normalization — the user has spoken.
        if raw in overrides and overrides[raw] in ALL_FIELDS:
            field = overrides[raw]
            field_map[idx] = field
            override_log.append((raw, _CANONICAL_FOR_FIELD.get(field, field)))
            continue
        norm = _normalize_header(raw)
        if norm in _NORMALIZED_COLUMN_MAP:
            field = _NORMALIZED_COLUMN_MAP[norm]
            field_map[idx] = field
            canonical = _CANONICAL_FOR_FIELD.get(field, field)
            if canonical != raw:
                normalization_log.append((raw, canonical))
            continue
        unrecognized.append(raw)
    mapped_fields = set(field_map.values())
    missing_required = REQUIRED_FIELDS - mapped_fields
    suggested = (
        suggest_mapping(unrecognized, mapped_fields) if missing_required else {}
    )
    return HeaderResolution(
        field_map=field_map,
        normalization_log=normalization_log,
        override_log=override_log,
        unrecognized=unrecognized,
        missing_required=missing_required,
        suggested=suggested,
        signature=signature_for(unrecognized),
    )


class NeedsHeaderMapping(Exception):
    """Raised when a file's header row leaves required fields unmapped and
    the caller didn't supply enough overrides. The web layer catches this
    and renders the mapping panel."""
    def __init__(self, resolution: HeaderResolution):
        super().__init__(
            f"Cannot map required column(s): "
            f"{sorted(resolution.missing_required)}"
        )
        self.resolution = resolution


def detect_encoding(path: Path) -> str:
    """Pick the first candidate encoding under which the header row decodes
    AND yields all required column names (after normalization). Returns
    DEFAULT_ENCODING if nothing fits, so downstream parsing still raises a
    meaningful error.

    mac_latin2 / cp1250 are single-byte encodings, so they never raise
    UnicodeDecodeError — we have to validate semantically (the required-field
    columns map cleanly under that encoding) rather than just structurally."""
    for enc in CANDIDATE_ENCODINGS:
        try:
            with open(path, encoding=enc) as f:
                text = f.read()
        except UnicodeDecodeError:
            continue
        for raw in text.splitlines():
            cells = raw.split("\t")
            while cells and cells[0] == "":
                cells.pop(0)
            if not cells or cells[0].strip() != HEADER_FIRST_CELL:
                continue
            resolution = resolve_header(cells)
            if not resolution.missing_required:
                return enc
            break  # header located but unresolvable under this encoding
    return DEFAULT_ENCODING


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
    normalization_log: list[tuple[str, str]] = dc_field(default_factory=list)
    override_log: list[tuple[str, str]] = dc_field(default_factory=list)


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
    normalization_log: list[tuple[str, str]] = dc_field(default_factory=list)
    override_log: list[tuple[str, str]] = dc_field(default_factory=list)


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


def parse_header(
    path: Path,
    encoding: str | None = None,
    overrides: dict[str, str] | None = None,
) -> HeaderResolution:
    """Locate and resolve the header row. Raises NeedsHeaderMapping if any
    required fields are still unmapped after normalization and overrides.
    Useful when the caller wants the resolution metadata up front (e.g.
    to look up a saved mapping by signature)."""
    if encoding is None:
        encoding = detect_encoding(path)
    with open(path, encoding=encoding) as f:
        text = f.read()
    for raw in text.splitlines():
        cells = raw.split("\t")
        while cells and cells[0] == "":
            cells.pop(0)
        if not cells or cells[0].strip() != HEADER_FIRST_CELL:
            continue
        resolution = resolve_header(cells, overrides=overrides)
        if resolution.missing_required:
            raise NeedsHeaderMapping(resolution)
        return resolution
    raise ValueError(
        f"No column header row (starting with {HEADER_FIRST_CELL!r}) found "
        f"in {path}."
    )


def peek_pps_element(
    path: Path,
    encoding: str | None = None,
    overrides: dict[str, str] | None = None,
) -> str:
    """Return the PPS element from the first data row, without parsing the rest.

    Used by the UI to decide whether the project is already known (no name
    prompt needed) before kicking off a full import.
    """
    if encoding is None:
        encoding = detect_encoding(path)
    for rec in read_records(path, encoding=encoding, overrides=overrides):
        pps = rec.get("pps_element")
        if pps:
            return pps
    raise ValueError(f"No data rows found in {path}.")


def read_footer_total(path: Path, encoding: str | None = None) -> Decimal:
    """Return the total amount from the `*` footer row.

    Every export from the source system ends with a `* | ... | total` row.
    Comparing that figure against the sum of parsed rows is our safety net
    against silent parsing or encoding errors.
    """
    if encoding is None:
        encoding = detect_encoding(path)
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
    path: Path,
    encoding: str | None = None,
    overrides: dict[str, str] | None = None,
) -> Iterator[dict]:
    """Yield one dict per data row, with string values mapped by column name.

    Handles the two known source-system layouts (10-column full and 7-column
    compact) and any future column reorderings, because field positions are
    resolved from the actual header row in each file. Skips preamble lines
    before the header and stops at a `*` totals footer. Raises
    NeedsHeaderMapping if a required field is unmappable.
    """
    if encoding is None:
        encoding = detect_encoding(path)
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
                resolution = resolve_header(cells, overrides=overrides)
                if resolution.missing_required:
                    raise NeedsHeaderMapping(resolution)
                field_map = resolution.field_map
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


def fingerprint(rec) -> str:
    """Short, stable fingerprint of a row's natural key. Used as the
    checkbox value in the analyze panel so the apply step can re-identify
    rows even after the file or DB rows are re-fetched."""
    return hashlib.sha256(repr(_natural_key(rec)).encode()).hexdigest()[:16]


def _record_to_dict(t: Transaction) -> dict:
    return {
        "id": t.id,
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
    path: Path,
    encoding: str | None,
    overrides: dict[str, str] | None = None,
) -> tuple[str, list[dict], str, date, date, HeaderResolution]:
    """Parse + validate a file. Returns (file_hash, records, pps_element,
    period_start, period_end, header_resolution). Raises ValueError on any
    structural problem or footer-total mismatch; NeedsHeaderMapping if the
    header row is unresolvable."""
    if encoding is None:
        encoding = detect_encoding(path)
    file_hash = sha256_file(path)
    resolution = parse_header(path, encoding=encoding, overrides=overrides)
    records = [
        parse_record(r)
        for r in read_records(path, encoding=encoding, overrides=overrides)
    ]
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
    return file_hash, records, pps_element, period_start, period_end, resolution


def import_file(
    session: Session,
    path: Path,
    user_id: int,
    name_resolver: Callable[[str], str],
    encoding: str | None = None,
    mode: str = "add",
    new_fingerprints: set[str] | None = None,
    missing_ids: set[int] | None = None,
    overrides: dict[str, str] | None = None,
) -> ImportResult:
    """Apply a file to the DB.

    Default (non-selective) semantics:
      - mode='add'     → insert every new row from the file; skip duplicates.
      - mode='replace' → delete every row in the file's date range, then insert
                         every row from the file.

    Selective semantics (caller passes new_fingerprints and/or missing_ids,
    typically from the analyze view's checkboxes):
      - mode='add'     → insert only rows whose fingerprint is in
                         new_fingerprints. missing_ids is ignored.
      - mode='replace' → insert only rows whose fingerprint is in
                         new_fingerprints, AND delete only DB rows whose id is
                         in missing_ids. Existing (in both) rows are untouched.
                         This is "surgical replace" — much narrower than the
                         period-wide delete of the non-selective form.

    The file-hash dedup short-circuit only fires in non-selective mode; with
    a selection the user is explicitly picking rows and may legitimately
    re-apply parts of the same file.
    """
    if mode not in ("add", "replace"):
        raise ValueError(f"Unknown mode {mode!r}; expected 'add' or 'replace'.")

    selective = new_fingerprints is not None or missing_ids is not None

    (
        file_hash,
        records,
        pps_element,
        period_start,
        period_end,
        resolution,
    ) = _parse_and_validate(path, encoding, overrides=overrides)

    # For "add" with no selection, skip if we've already ingested this exact
    # file. For "replace" the user is asking to overwrite, so re-running on the
    # same file is fine. With a selection the user is explicit; skip the check.
    if mode == "add" and not selective:
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
    if mode == "replace" and not selective:
        # Period-wide replace: wipe the project's rows in the file's date
        # range, then re-insert everything in the file. Rows outside the range
        # are left alone so partial-year exports don't nuke other months.
        from sqlalchemy import delete as sql_delete

        result = session.execute(
            sql_delete(Transaction).where(
                Transaction.project_id == project.id,
                Transaction.posting_date.between(period_start, period_end),
            )
        )
        rows_deleted = result.rowcount or 0
        session.flush()
    elif mode == "replace" and selective and missing_ids:
        # Surgical replace: only delete the DB rows the user ticked in the
        # Missing list. Scope by project to avoid stray ids from another
        # project sneaking through.
        from sqlalchemy import delete as sql_delete

        result = session.execute(
            sql_delete(Transaction).where(
                Transaction.project_id == project.id,
                Transaction.id.in_(missing_ids),
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
        if selective:
            # Only insert rows whose fingerprint was ticked. An empty set means
            # "ticked nothing" (e.g. unticked every New row); we still honor it
            # and insert nothing.
            if not new_fingerprints or fingerprint(rec) not in new_fingerprints:
                continue
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
        normalization_log=resolution.normalization_log,
        override_log=resolution.override_log,
    )


def analyze_file(
    session: Session,
    path: Path,
    user_id: int,
    encoding: str | None = None,
    overrides: dict[str, str] | None = None,
) -> ImportAnalysis:
    """Compute a diff between the file and the DB for the file's date range.
    No DB writes; doesn't create a project if it's new."""
    (
        file_hash,
        records,
        pps_element,
        period_start,
        period_end,
        resolution,
    ) = _parse_and_validate(path, encoding, overrides=overrides)

    project = session.scalar(
        select(Project).where(
            Project.owner_user_id == user_id,
            Project.pps_element == pps_element,
        )
    )

    # Attach a stable fingerprint to each parsed file row so the analyze
    # template can use it as the checkbox value and apply can re-identify
    # rows after a fresh re-parse.
    def with_fp(r: dict) -> dict:
        return {**r, "fp": fingerprint(r)}

    file_by_key: dict[tuple, dict] = {_natural_key(r): with_fp(r) for r in records}

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
            new_records=sorted(file_by_key.values(), key=sort_key),
            existing_records=[],
            missing_records=[],
            normalization_log=resolution.normalization_log,
            override_log=resolution.override_log,
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
        normalization_log=resolution.normalization_log,
        override_log=resolution.override_log,
    )
