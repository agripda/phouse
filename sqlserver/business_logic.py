"""
business_logic.py — Leave period decomposition & validation helpers
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import List

from models import LeaveDetail, LeavePeriod, LeaveDayRecord


# ------------------------------------------------------------------ #
#  Working-day utilities                                              #
# ------------------------------------------------------------------ #

def _is_working_day(d: date) -> bool:
    """Return True for Monday–Friday (weekday 0–4)."""
    return d.weekday() < 5   # 5=Saturday, 6=Sunday


def working_days_in_range(start: date, end: date) -> List[date]:
    """
    Return an ordered list of all Mon–Fri dates in [start, end] inclusive.
    Public holidays are out of scope and therefore not excluded.
    """
    days: List[date] = []
    current = start
    while current <= end:
        if _is_working_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days


# ------------------------------------------------------------------ #
#  Validation helpers                                                 #
# ------------------------------------------------------------------ #

def validate_working_day_alignment(
    period: LeavePeriod,
    leave_details: List[LeaveDetail],
) -> None:
    """
    Raise ValueError if:
      - The actual working-day count doesn't match totalWorkingDays, OR
      - The sum of leaveDetail quantities doesn't match totalWorkingDays.

    Only "Days" unit-of-measure is compared to day counts.
    Other units (e.g. Hours) are accepted as-is without day-level comparison.
    """
    actual_days = len(
        working_days_in_range(
            period.startDate.date(), period.endDate.date()
        )
    )

    if actual_days != period.totalWorkingDays:
        raise ValueError(
            f"totalWorkingDays ({period.totalWorkingDays}) does not match "
            f"the actual working-day count in the date range ({actual_days})."
        )

    day_based_details = [
        d for d in leave_details
        if d.unitOfMeasure.lower() == "days"
    ]
    if day_based_details:
        total_qty = sum(d.quantity for d in day_based_details)
        if total_qty != Decimal(actual_days):
            raise ValueError(
                f"Sum of leaveDetail quantities ({total_qty}) does not match "
                f"the working-day count ({actual_days})."
            )


# ------------------------------------------------------------------ #
#  Day decomposition                                                  #
# ------------------------------------------------------------------ #

def decompose_to_leave_days(
    period: LeavePeriod,
    leave_details: List[LeaveDetail],
) -> List[LeaveDayRecord]:
    """
    Expand a leave period into one LeaveDayRecord per working day.

    If multiple leaveDetails are provided (e.g. split leave types),
    each day record is produced once per detail item — the caller
    is responsible for providing non-overlapping quantities that sum
    to totalWorkingDays.  For the common single-detail case the
    per-day quantity is always 1.0 (one full day).
    """
    working_days = working_days_in_range(
        period.startDate.date(), period.endDate.date()
    )

    records: List[LeaveDayRecord] = []
    for detail in leave_details:
        for day in working_days:
            records.append(
                LeaveDayRecord(
                    leaveDate=day,
                    leaveTypeCode=detail.leaveTypeCode,
                    leaveCategory=detail.leaveCategory,
                    unitOfMeasure=detail.unitOfMeasure,
                    # Each day = 1 unit regardless of total quantity
                    quantity=Decimal("1.00"),
                )
            )
    return records
