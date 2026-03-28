# -*- coding: utf-8 -*-
"""
business_logic.py — Working-day decomposition, alignment validation,
                    and SubmissionId generation
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable, List

from models import LeaveDetail, LeavePeriod, LeaveDayRecord


# ── SubmissionId generation ───────────────────────────────────────────────────

def generate_submission_id(
    get_next_seq_fn: Callable[[], int],
    exists_fn: Callable[[str], bool],
    now: datetime | None = None,
) -> str:
    """
    Generate a unique, incremental SubmissionId.

    Format:  LS-YYYY-NNNNNN
      - LS    : fixed prefix
      - YYYY  : year of submission (reflects when submitted)
      - NNNNNN: global zero-padded sequence — never resets, continues
                across years (e.g. LS-2026-000999 → LS-2027-001000)

    Args:
        get_next_seq_fn: returns MAX(global sequence) + 1 from the DB.
        exists_fn:       returns True if a candidate ID already exists.
        now:             override current datetime (for testing).

    Raises:
        RuntimeError: if sequence exceeds 999,999.
    """
    year = (now or datetime.now()).strftime("%Y")
    seq  = get_next_seq_fn()       # start from DB MAX + 1, not from 1

    while seq <= 999_999:
        candidate = f"LS-{year}-{seq:06d}"
        if not exists_fn(candidate):
            return candidate
        seq += 1                   # collision safety (concurrent inserts)

    raise RuntimeError(
        "SubmissionId sequence exceeded 999,999. "
        "Migrate to a wider sequence or UUID."
    )


# ── Working-day utilities ─────────────────────────────────────────────────────

def _is_working_day(d: date) -> bool:
    """Monday–Friday only. Public holidays out of scope."""
    return d.weekday() < 5


def working_days_in_range(start: date, end: date) -> List[date]:
    days, current = [], start
    while current <= end:
        if _is_working_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days


def validate_working_day_alignment(
    period: LeavePeriod,
    leave_details: List[LeaveDetail],
) -> None:
    actual = len(working_days_in_range(period.startDate.date(), period.endDate.date()))

    if actual != period.totalWorkingDays:
        raise ValueError(
            f"totalWorkingDays ({period.totalWorkingDays}) does not match "
            f"actual working-day count in date range ({actual})."
        )

    day_details = [d for d in leave_details if d.unitOfMeasure.lower() == "days"]
    if day_details:
        total_qty = sum(d.quantity for d in day_details)
        if total_qty != Decimal(actual):
            raise ValueError(
                f"Sum of leaveDetail quantities ({total_qty}) does not match "
                f"working-day count ({actual})."
            )


def decompose_to_leave_days(
    period: LeavePeriod,
    leave_details: List[LeaveDetail],
) -> List[LeaveDayRecord]:
    working_days = working_days_in_range(period.startDate.date(), period.endDate.date())
    return [
        LeaveDayRecord(
            leaveDate=day,
            leaveTypeCode=detail.leaveTypeCode,
            leaveCategory=detail.leaveCategory,
            unitOfMeasure=detail.unitOfMeasure,
            quantity=Decimal("1.00"),
        )
        for detail in leave_details
        for day in working_days
    ]