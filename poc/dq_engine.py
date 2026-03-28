# -*- coding: utf-8 -*-
"""
dq_engine.py — Data Quality engine for Leave Submission PoC

5 DQ domains (Accuracy, Completeness, Consistency, Timeliness, Uniqueness)
All issues are soft warnings — submission is never rejected by DQ.
Issues are recorded in the DQResult table and returned in the 201 response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List

from models import LeaveSubmissionPayload


# ── DQ domain and severity enums ─────────────────────────────────────────────

class DQDomain:
    ACCURACY     = "Accuracy"
    COMPLETENESS = "Completeness"
    CONSISTENCY  = "Consistency"
    TIMELINESS   = "Timeliness"
    UNIQUENESS   = "Uniqueness"


class DQSeverity:
    CRITICAL = "Critical"   # recorded, submission still proceeds (soft warning)
    WARNING  = "Warning"    # recorded, submission still proceeds


# ── DQ issue record ───────────────────────────────────────────────────────────

@dataclass
class DQIssue:
    domain:   str
    severity: str
    code:     str
    message:  str
    field:    str = ""

    def is_critical(self) -> bool:
        return self.severity == DQSeverity.CRITICAL


# ── Reference data ────────────────────────────────────────────────────────────

VALID_LEAVE_TYPE_CODES = {"AL", "SL", "CL", "UL", "PL", "LWP"}
VALID_LEAVE_CATEGORIES = {"Paid", "Unpaid"}
VALID_UOM              = {"Days", "Hours"}
VALID_STATUSES         = {"Submitted", "Draft", "Pending"}
VALID_APPROVAL_STATUS  = {"Pending", "Approved", "Rejected"}
WORKER_ID_PATTERN      = re.compile(r"^W\d{6}$")
SUBMISSION_ID_PATTERN  = re.compile(r"^LS-\d{4}-\d{6}$")

MAX_BACKDATED_DAYS     = 30   # warning if submittedDate > startDate by this many days
MAX_ADVANCE_DAYS       = 365  # warning if startDate > today + this many days


# ── DQ rule implementations ───────────────────────────────────────────────────

def _check_accuracy(p: LeaveSubmissionPayload) -> List[DQIssue]:
    issues: List[DQIssue] = []
    D, S = DQDomain.ACCURACY, DQSeverity

    # WorkerId pattern W + 6 digits
    if not WORKER_ID_PATTERN.match(p.worker.workerId):
        issues.append(DQIssue(D, S.WARNING, "ACC-001",
            f"workerId '{p.worker.workerId}' does not match expected pattern W######.",
            field="worker.workerId"))

    # SubmissionId pattern LS-YYYY-NNNNNN
    if p.submissionId and not SUBMISSION_ID_PATTERN.match(p.submissionId):
        issues.append(DQIssue(D, S.WARNING, "ACC-002",
            f"submissionId '{p.submissionId}' does not match expected pattern LS-YYYY-NNNNNN.",
            field="submissionId"))

    # leaveTypeCode against reference
    for i, d in enumerate(p.leaveDetails):
        if d.leaveTypeCode not in VALID_LEAVE_TYPE_CODES:
            issues.append(DQIssue(D, S.WARNING, "ACC-003",
                f"leaveTypeCode '{d.leaveTypeCode}' is not a recognised code. "
                f"Valid codes: {sorted(VALID_LEAVE_TYPE_CODES)}.",
                field=f"leaveDetails[{i}].leaveTypeCode"))

        if d.leaveCategory not in VALID_LEAVE_CATEGORIES:
            issues.append(DQIssue(D, S.WARNING, "ACC-004",
                f"leaveCategory '{d.leaveCategory}' is not a recognised value. "
                f"Expected: {sorted(VALID_LEAVE_CATEGORIES)}.",
                field=f"leaveDetails[{i}].leaveCategory"))

        if d.unitOfMeasure not in VALID_UOM:
            issues.append(DQIssue(D, S.WARNING, "ACC-005",
                f"unitOfMeasure '{d.unitOfMeasure}' is not a recognised value. "
                f"Expected: {sorted(VALID_UOM)}.",
                field=f"leaveDetails[{i}].unitOfMeasure"))

    # status value
    if p.status not in VALID_STATUSES:
        issues.append(DQIssue(D, S.WARNING, "ACC-006",
            f"status '{p.status}' is not a recognised value. "
            f"Expected: {sorted(VALID_STATUSES)}.",
            field="status"))

    return issues


def _check_completeness(p: LeaveSubmissionPayload) -> List[DQIssue]:
    issues: List[DQIssue] = []
    D, S = DQDomain.COMPLETENESS, DQSeverity

    # approverId required for non-Draft submissions
    if p.status != "Draft":
        if not p.approver or not p.approver.approverId.strip():
            issues.append(DQIssue(D, S.WARNING, "CMP-001",
                "approver.approverId is missing for a non-Draft submission.",
                field="approver.approverId"))

    # comments required for Pending/Rejected
    if p.status in {"Pending"} and not (p.comments and p.comments.strip()):
        issues.append(DQIssue(D, S.WARNING, "CMP-002",
            "comments should be provided for Pending submissions.",
            field="comments"))

    # employeeNumber
    if not p.worker.employeeNumber.strip():
        issues.append(DQIssue(D, S.WARNING, "CMP-003",
            "worker.employeeNumber is empty.",
            field="worker.employeeNumber"))

    return issues


def _check_consistency(p: LeaveSubmissionPayload) -> List[DQIssue]:
    issues: List[DQIssue] = []
    D, S = DQDomain.CONSISTENCY, DQSeverity

    start = p.leavePeriod.startDate.date()
    end   = p.leavePeriod.endDate.date()

    # totalWeeks consistency
    actual_weeks = max(1, ((end - start).days + 1 + 6) // 7)
    if abs(actual_weeks - p.leavePeriod.totalWeeks) > 1:
        issues.append(DQIssue(D, S.WARNING, "CON-001",
            f"totalWeeks ({p.leavePeriod.totalWeeks}) is inconsistent with "
            f"the date range ({actual_weeks} weeks).",
            field="leavePeriod.totalWeeks"))

    # quantity sum matches totalWorkingDays for Days-based details
    day_details = [d for d in p.leaveDetails if d.unitOfMeasure.lower() == "days"]
    if day_details:
        qty_sum = sum(float(d.quantity) for d in day_details)
        if abs(qty_sum - p.leavePeriod.totalWorkingDays) > 0.01:
            issues.append(DQIssue(D, S.WARNING, "CON-002",
                f"Sum of leaveDetail quantities ({qty_sum}) does not match "
                f"totalWorkingDays ({p.leavePeriod.totalWorkingDays}).",
                field="leaveDetails[*].quantity"))

    # submittedDate ≤ startDate (can't submit after leave starts)
    if p.submittedDate > start:
        issues.append(DQIssue(D, S.WARNING, "CON-003",
            f"submittedDate ({p.submittedDate}) is after the leave startDate ({start}). "
            "Submission may be backdated.",
            field="submittedDate"))

    return issues


def _check_timeliness(p: LeaveSubmissionPayload) -> List[DQIssue]:
    issues: List[DQIssue] = []
    D, S = DQDomain.TIMELINESS, DQSeverity

    today = date.today()
    start = p.leavePeriod.startDate.date()
    end   = p.leavePeriod.endDate.date()

    # Backdated submission warning
    if start < today - timedelta(days=MAX_BACKDATED_DAYS):
        issues.append(DQIssue(D, S.WARNING, "TML-001",
            f"Leave startDate ({start}) is more than {MAX_BACKDATED_DAYS} days in the past. "
            "Backdated submissions may require additional approval.",
            field="leavePeriod.startDate"))

    # Far-future submission warning
    if start > today + timedelta(days=MAX_ADVANCE_DAYS):
        issues.append(DQIssue(D, S.WARNING, "TML-002",
            f"Leave startDate ({start}) is more than {MAX_ADVANCE_DAYS} days in the future. "
            "Verify this is intentional.",
            field="leavePeriod.startDate"))

    # submittedDate in future
    if p.submittedDate > today:
        issues.append(DQIssue(D, S.WARNING, "TML-003",
            f"submittedDate ({p.submittedDate}) is in the future.",
            field="submittedDate"))

    return issues


def _check_uniqueness(
    p: LeaveSubmissionPayload,
    existing_dates_fn=None,
) -> List[DQIssue]:
    """
    Check for overlapping leave periods for the same worker.
    existing_dates_fn(workerId) → set of DATE strings already in LeaveDay.
    """
    issues: List[DQIssue] = []
    D, S = DQDomain.UNIQUENESS, DQSeverity

    if existing_dates_fn is None:
        return issues

    existing = existing_dates_fn(p.worker.workerId)
    if not existing:
        return issues

    start = p.leavePeriod.startDate.date()
    end   = p.leavePeriod.endDate.date()

    overlapping = []
    cur = start
    while cur <= end:
        if cur.strftime("%Y-%m-%d") in existing:
            overlapping.append(str(cur))
        cur += timedelta(days=1)

    if overlapping:
        issues.append(DQIssue(D, S.CRITICAL, "UNQ-001",
            f"Worker '{p.worker.workerId}' already has leave recorded on "
            f"{len(overlapping)} date(s) in this range: {overlapping[:5]}"
            f"{'...' if len(overlapping) > 5 else ''}. "
            "Submission rejected — overlapping leave dates are not allowed.",
            field="leavePeriod"))

    return issues


# ── Public interface ──────────────────────────────────────────────────────────

@dataclass
class DQResult:
    issues:   List[DQIssue] = field(default_factory=list)
    passed:   bool = True

    @property
    def critical_issues(self) -> List[DQIssue]:
        return [i for i in self.issues if i.is_critical()]

    @property
    def warning_issues(self) -> List[DQIssue]:
        return [i for i in self.issues if not i.is_critical()]

    def to_dict_list(self) -> list:
        return [
            {
                "domain":   i.domain,
                "severity": i.severity,
                "code":     i.code,
                "message":  i.message,
                "field":    i.field,
            }
            for i in self.issues
        ]


def run_dq_checks(
    payload: LeaveSubmissionPayload,
    existing_dates_fn=None,
) -> DQResult:
    """
    Run all 5 DQ domain checks against the payload.
    Returns a DQResult with all issues and a passed flag.
    passed=False means at least one CRITICAL issue — submission should be rejected.
    """
    all_issues: List[DQIssue] = []
    all_issues += _check_accuracy(payload)
    all_issues += _check_completeness(payload)
    all_issues += _check_consistency(payload)
    all_issues += _check_timeliness(payload)
    all_issues += _check_uniqueness(payload, existing_dates_fn)

    passed = not any(i.is_critical() for i in all_issues)
    return DQResult(issues=all_issues, passed=passed)