# -*- coding: utf-8 -*-
"""
models.py — Pydantic v2 request / response schemas (PoC copy)
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


# ── Request ───────────────────────────────────────────────────────────────────

class Worker(BaseModel):
    workerId: str = Field(..., min_length=1, max_length=50)
    employeeNumber: str
    sourceSystem: str


class LeavePeriod(BaseModel):
    startDate: datetime
    endDate: datetime
    totalWeeks: int = Field(..., ge=0)
    totalWorkingDays: int = Field(..., ge=1)

    @model_validator(mode="after")
    def start_before_end(self) -> "LeavePeriod":
        if self.startDate > self.endDate:
            raise ValueError("startDate must be ≤ endDate")
        return self


class LeaveDetail(BaseModel):
    leaveTypeCode: str = Field(..., min_length=1, max_length=10)
    leaveTypeDescription: str
    leaveCategory: str = Field(..., max_length=20)
    unitOfMeasure: str = Field(..., max_length=10)
    quantity: Decimal = Field(..., ge=Decimal("0.01"))


class Approver(BaseModel):
    approverId: str
    approvalStatus: str


class LeaveSubmissionPayload(BaseModel):
    submissionId: Optional[str] = Field(
        default=None,
        max_length=50,
        description="Caller-supplied (e.g. LS-2026-000123). If omitted, server auto-generates LS-YYYY-NNNNNN.",
    )
    submittedDate: date
    status: str = Field(..., max_length=20)
    worker: Worker
    leavePeriod: LeavePeriod
    leaveDetails: List[LeaveDetail] = Field(..., min_length=1)
    approver: Optional[Approver] = None
    comments: Optional[str] = None


class SubmitLeaveRequest(BaseModel):
    leaveSubmission: LeaveSubmissionPayload


# ── Response ──────────────────────────────────────────────────────────────────

class LeaveDayRecord(BaseModel):
    leaveDate: date
    leaveTypeCode: str
    leaveCategory: str
    unitOfMeasure: str
    quantity: float   # float avoids Decimal serialisation artifacts in OpenAPI


class LeaveSubmissionResponse(BaseModel):
    submissionId: str
    workerId: str
    status: str
    totalWorkingDaysCreated: int
    leaveDays: List[LeaveDayRecord]
    dq_issues: List[dict] = []   # warnings that passed through


class LeaveDayDetail(BaseModel):
    """Day row as stored in DB — returned by GET endpoint."""
    LeaveDayId:    int
    SubmissionId:  str
    WorkerId:      str
    LeaveDate:     str
    LeaveTypeCode: str
    LeaveCategory: str
    UnitOfMeasure: str
    Quantity:      float


class LeaveSubmissionDetail(BaseModel):
    """Submission header as stored in DB — returned by GET endpoint."""
    SubmissionId:  str
    WorkerId:      str
    StartDatetime: str
    EndDatetime:   str
    TotalDays:     int
    Status:        str
    SubmittedDate: str


class GetSubmissionResponse(BaseModel):
    submission: LeaveSubmissionDetail
    leaveDays:  List[LeaveDayDetail]


class ErrorResponse(BaseModel):
    error: str
    detail: str