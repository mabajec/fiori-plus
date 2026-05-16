from __future__ import annotations

import calendar
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Project, ProjectAnnualData, Transaction


MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# Patterns observed in the `text` column. Ordered by reliability — the first
# match wins. The captured group is the candidate name.
_NAME_PATTERNS = [
    re.compile(r"^\d{6}/([^/]+?)(?:/.*)?$"),       # "202601/Cvek Jernej[/00230135]"
    re.compile(r"^ŠS\s+(.+)$"),                    # "ŠS Tomi Trošt"  (student services)
    re.compile(r"^PERNR:.+?\s-\s(.+)$"),           # "PERNR: 2300184 PN: 2026/0163 Letalo - Tjaša Šoltes"
]


def _extract_name(text: str | None) -> str | None:
    if not text:
        return None
    for pat in _NAME_PATTERNS:
        m = pat.match(text)
        if m:
            return m.group(1).strip()
    return None


def build_employee_name_map(
    session: Session, project_ids: int | list[int]
) -> dict[str, str]:
    """Map employee_number → most-common extracted name across the given
    project(s). Names are derived by regex from the `text` column; the
    winner per employee is the variant that appears the most times.
    Accepts a single project id or a list — same employee in multiple
    projects naturally gets more name evidence to vote on.
    """
    ids = [project_ids] if isinstance(project_ids, int) else list(project_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(Transaction.employee, Transaction.text)
        .where(
            Transaction.project_id.in_(ids),
            Transaction.employee.isnot(None),
            Transaction.text.isnot(None),
        )
    ).all()
    counts: dict[str, Counter] = defaultdict(Counter)
    for emp, text in rows:
        name = _extract_name(text)
        if name:
            counts[emp][name] += 1
    return {emp: ctr.most_common(1)[0][0] for emp, ctr in counts.items() if ctr}


@dataclass
class ProjectSummary:
    """Numbers shown on the dashboard card and detail-page summary."""
    project: Project
    latest_year: Optional[int]            # None when project has zero transactions
    ytd_spent: Decimal                    # sum in latest_year (0 if no data)
    all_time_spent: Decimal
    last_txn_date: Optional[date]
    days_in_year: int                     # 365 or 366
    days_elapsed: int                     # 0 when no data
    projection: Optional[Decimal]         # None when no data; ytd × days_in_year / days_elapsed
    annual: Optional[ProjectAnnualData]   # the row for latest_year, or None


def project_summary(session: Session, project: Project) -> ProjectSummary:
    latest_year_raw = session.scalar(
        select(func.max(func.extract("year", Transaction.posting_date)))
        .where(Transaction.project_id == project.id)
    )
    all_time = session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0))
        .where(Transaction.project_id == project.id)
    ) or Decimal(0)

    if latest_year_raw is None:
        return ProjectSummary(
            project=project,
            latest_year=None,
            ytd_spent=Decimal(0),
            all_time_spent=all_time,
            last_txn_date=None,
            days_in_year=365,
            days_elapsed=0,
            projection=None,
            annual=None,
        )

    latest_year = int(latest_year_raw)
    ytd = session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0))
        .where(
            Transaction.project_id == project.id,
            func.extract("year", Transaction.posting_date) == latest_year,
        )
    ) or Decimal(0)
    last_date = session.scalar(
        select(func.max(Transaction.posting_date))
        .where(Transaction.project_id == project.id)
    )

    days_in_year = 366 if calendar.isleap(latest_year) else 365
    if last_date and last_date.year == latest_year:
        days_elapsed = (last_date - date(latest_year, 1, 1)).days + 1
    else:
        days_elapsed = days_in_year

    projection: Optional[Decimal] = None
    if days_elapsed > 0:
        projection = (
            ytd * Decimal(days_in_year) / Decimal(days_elapsed)
        ).quantize(Decimal("0.01"))

    annual = session.scalar(
        select(ProjectAnnualData).where(
            ProjectAnnualData.project_id == project.id,
            ProjectAnnualData.year == latest_year,
        )
    )

    return ProjectSummary(
        project=project,
        latest_year=latest_year,
        ytd_spent=ytd,
        all_time_spent=all_time,
        last_txn_date=last_date,
        days_in_year=days_in_year,
        days_elapsed=days_elapsed,
        projection=projection,
        annual=annual,
    )


OTHER_LINE_ID = "other"


@dataclass
class StructureRow:
    line_id: int | str         # numeric ProjectBudgetLine.id, or OTHER_LINE_ID
    label: str                 # display label; "OTHER" for the catch-all bucket
    account_prefix: Optional[str]
    budgeted: Optional[Decimal]
    spent: Decimal
    projected: Optional[Decimal]  # year-end projection; None when no activity yet


def _year_progress(session: Session, project_id: int, year: int) -> tuple[int, int]:
    """Return (days_elapsed, days_in_year) for this project's transactions in
    `year`. days_elapsed is anchored to the latest transaction posting date
    so the projection is consistent with project_summary."""
    last_date = session.scalar(
        select(func.max(Transaction.posting_date))
        .where(
            Transaction.project_id == project_id,
            func.extract("year", Transaction.posting_date) == year,
        )
    )
    days_in_year = 366 if calendar.isleap(year) else 365
    if last_date is None:
        return 0, days_in_year
    days_elapsed = (last_date - date(year, 1, 1)).days + 1
    return days_elapsed, days_in_year


def _project(value: Decimal, days_elapsed: int, days_in_year: int) -> Optional[Decimal]:
    if days_elapsed <= 0:
        return None
    return (value * Decimal(days_in_year) / Decimal(days_elapsed)).quantize(
        Decimal("0.01")
    )


def structure_breakdown(
    session: Session, project: Project, year: int
) -> list[StructureRow]:
    """Group transactions into the project's budget lines (longest-prefix
    match) plus a single "OTHER" bucket for anything that didn't match.
    The OTHER row is always emitted last, even when zero — that way the
    table has a stable shape regardless of whether categories are defined.
    """
    annual = session.scalar(
        select(ProjectAnnualData).where(
            ProjectAnnualData.project_id == project.id,
            ProjectAnnualData.year == year,
        )
    )
    lines = list(annual.budget_lines) if annual else []
    txns = session.execute(
        select(Transaction.account_code, Transaction.amount)
        .where(
            Transaction.project_id == project.id,
            func.extract("year", Transaction.posting_date) == year,
        )
    ).all()

    prefixes = [(l.account_prefix, l.id) for l in lines if l.account_prefix]
    prefixes.sort(key=lambda p: len(p[0]), reverse=True)

    spent_by_id: dict[int, Decimal] = {l.id: Decimal(0) for l in lines}
    other_spent = Decimal(0)
    for code, amount in txns:
        match_id: int | None = None
        for prefix, line_id in prefixes:
            if code.startswith(prefix):
                match_id = line_id
                break
        if match_id is None:
            other_spent += amount
        else:
            spent_by_id[match_id] += amount

    days_elapsed, days_in_year = _year_progress(session, project.id, year)

    rows: list[StructureRow] = [
        StructureRow(
            line_id=line.id,
            label=line.label,
            account_prefix=line.account_prefix,
            budgeted=line.amount,
            spent=spent_by_id[line.id],
            projected=_project(spent_by_id[line.id], days_elapsed, days_in_year),
        )
        for line in lines
    ]
    rows.append(
        StructureRow(
            line_id=OTHER_LINE_ID,
            label="OTHER",
            account_prefix=None,
            budgeted=None,
            spent=other_spent,
            projected=_project(other_spent, days_elapsed, days_in_year),
        )
    )
    return rows


def monthly_totals(
    session: Session, project: Project, year: int
) -> list[float]:
    """Return spend per month for the given project+year as a 12-element list
    indexed 0..11 (Jan..Dec). Months with no transactions contribute 0.0."""
    rows = session.execute(
        select(
            func.extract("month", Transaction.posting_date).label("mo"),
            func.sum(Transaction.amount).label("total"),
        )
        .where(
            Transaction.project_id == project.id,
            func.extract("year", Transaction.posting_date) == year,
        )
        .group_by("mo")
    ).all()
    result = [0.0] * 12
    for mo, total in rows:
        result[int(mo) - 1] = float(total)
    return result


@dataclass
class PersonMonthGrid:
    employees: list[str]                # display order; "" used for null employee
    totals_by_emp: dict[str, Decimal]   # full year sum per employee
    grid: dict[str, dict[int, Decimal]] # emp → {month_int 1-12: sum}
    names: dict[str, str]               # employee_number → display name (when known)


def per_person_month(
    session: Session,
    project_ids: int | list[int],
    year: int,
) -> PersonMonthGrid:
    # Only include transactions with an employee number set. Transactions
    # without one (general overhead like software subscriptions, licences,
    # materials etc.) are categorically different and belong in the spending-
    # by-category view, not in a per-person grid where they would silently
    # land in a misleading "(no employee)" row.
    ids = [project_ids] if isinstance(project_ids, int) else list(project_ids)
    if not ids:
        return PersonMonthGrid(
            employees=[], totals_by_emp={}, grid={}, names={}
        )
    rows = session.execute(
        select(
            Transaction.employee.label("emp"),
            func.extract("month", Transaction.posting_date).label("mo"),
            func.sum(Transaction.amount).label("amt"),
        )
        .where(
            Transaction.project_id.in_(ids),
            Transaction.employee.isnot(None),
            func.extract("year", Transaction.posting_date) == year,
        )
        .group_by("emp", "mo")
    ).all()

    grid: dict[str, dict[int, Decimal]] = {}
    totals: dict[str, Decimal] = {}
    for emp, mo, amt in rows:
        grid.setdefault(emp, {})[int(mo)] = amt
        totals[emp] = totals.get(emp, Decimal(0)) + amt

    # Sort by absolute total desc so largest spenders top the list.
    employees = sorted(totals.keys(), key=lambda e: abs(totals[e]), reverse=True)
    names = build_employee_name_map(session, ids)
    return PersonMonthGrid(
        employees=employees,
        totals_by_emp=totals,
        grid=grid,
        names=names,
    )
