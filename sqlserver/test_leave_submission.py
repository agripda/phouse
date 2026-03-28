"""
tests/test_leave_submission.py

Run with: pytest tests/ -v

The database layer is mocked so no SQL Server connection is required.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from business_logic import decompose_to_leave_days, working_days_in_range
from main import app
from models import LeaveDetail, LeavePeriod

client = TestClient(app)

VALID_PAYLOAD = {
    "leaveSubmission": {
        "submissionId": "LS-2026-000123",
        "submittedDate": "2026-02-15",
        "status": "Submitted",
        "worker": {
            "workerId": "W123456",
            "employeeNumber": "90030366",
            "sourceSystem": "HRIS",
        },
        "leavePeriod": {
            "startDate": "2026-03-02 00:00:00.00",
            "endDate": "2026-03-20 23:59:59.99",
            "totalWeeks": 3,
            "totalWorkingDays": 15,
        },
        "leaveDetails": [
            {
                "leaveTypeCode": "AL",
                "leaveTypeDescription": "Annual Leave",
                "leaveCategory": "Paid",
                "unitOfMeasure": "Days",
                "quantity": 15,
            }
        ],
        "approver": {
            "approverId": "M987654",
            "approvalStatus": "Pending",
        },
        "comments": "Planned annual leave for personal travel.",
    }
}


# ------------------------------------------------------------------ #
#  Unit tests — business_logic                                        #
# ------------------------------------------------------------------ #

class TestWorkingDaysInRange:
    def test_full_week(self):
        days = working_days_in_range(date(2026, 3, 2), date(2026, 3, 6))
        assert len(days) == 5
        assert all(d.weekday() < 5 for d in days)

    def test_spans_weekend(self):
        # Mon 2 Mar → Fri 13 Mar → should give exactly 10 working days
        days = working_days_in_range(date(2026, 3, 2), date(2026, 3, 13))
        assert len(days) == 10

    def test_starts_saturday(self):
        # Saturday + Sunday = 0 working days
        days = working_days_in_range(date(2026, 3, 7), date(2026, 3, 8))
        assert len(days) == 0

    def test_single_working_day(self):
        days = working_days_in_range(date(2026, 3, 2), date(2026, 3, 2))
        assert len(days) == 1

    def test_3_week_period(self):
        # 2 Mar 2026 (Mon) – 20 Mar 2026 (Fri) → 15 working days
        days = working_days_in_range(date(2026, 3, 2), date(2026, 3, 20))
        assert len(days) == 15


class TestDecomposeToLeaveDays:
    def _make_detail(self, code="AL", qty=15):
        return LeaveDetail(
            leaveTypeCode=code,
            leaveTypeDescription="Annual Leave",
            leaveCategory="Paid",
            unitOfMeasure="Days",
            quantity=Decimal(qty),
        )

    def _make_period(self):
        from datetime import datetime
        return LeavePeriod(
            startDate=datetime(2026, 3, 2),
            endDate=datetime(2026, 3, 20, 23, 59, 59),
            totalWeeks=3,
            totalWorkingDays=15,
        )

    def test_correct_day_count(self):
        records = decompose_to_leave_days(
            self._make_period(), [self._make_detail()]
        )
        assert len(records) == 15

    def test_each_day_quantity_is_one(self):
        records = decompose_to_leave_days(
            self._make_period(), [self._make_detail()]
        )
        assert all(r.quantity == Decimal("1.00") for r in records)

    def test_leave_type_propagated(self):
        records = decompose_to_leave_days(
            self._make_period(), [self._make_detail(code="SL")]
        )
        assert all(r.leaveTypeCode == "SL" for r in records)

    def test_no_weekends_in_output(self):
        records = decompose_to_leave_days(
            self._make_period(), [self._make_detail()]
        )
        assert all(r.leaveDate.weekday() < 5 for r in records)


# ------------------------------------------------------------------ #
#  Integration tests — API endpoint                                   #
# ------------------------------------------------------------------ #

class TestPostLeaveSubmissions:
    @patch("main.submission_exists", return_value=False)
    @patch("main.persist_submission", return_value=None)
    def test_valid_payload_returns_201(self, mock_persist, mock_exists):
        resp = client.post("/api/v1/leave-submissions", json=VALID_PAYLOAD)
        assert resp.status_code == 201
        body = resp.json()
        assert body["submissionId"] == "LS-2026-000123"
        assert body["totalWorkingDaysCreated"] == 15
        assert len(body["leaveDays"]) == 15

    @patch("main.submission_exists", return_value=True)
    def test_duplicate_submission_returns_409(self, mock_exists):
        resp = client.post("/api/v1/leave-submissions", json=VALID_PAYLOAD)
        assert resp.status_code == 409

    def test_missing_worker_id_returns_422(self):
        bad = {
            "leaveSubmission": {
                **VALID_PAYLOAD["leaveSubmission"],
                "worker": {"employeeNumber": "x", "sourceSystem": "HRIS"},
            }
        }
        resp = client.post("/api/v1/leave-submissions", json=bad)
        assert resp.status_code == 422

    def test_start_after_end_returns_422(self):
        bad_period = {
            "startDate": "2026-03-20 00:00:00",
            "endDate": "2026-03-02 23:59:59",
            "totalWeeks": 3,
            "totalWorkingDays": 15,
        }
        bad = {
            "leaveSubmission": {
                **VALID_PAYLOAD["leaveSubmission"],
                "leavePeriod": bad_period,
            }
        }
        resp = client.post("/api/v1/leave-submissions", json=bad)
        assert resp.status_code == 422

    @patch("main.submission_exists", return_value=False)
    @patch("main.persist_submission", return_value=None)
    def test_wrong_total_days_returns_400(self, mock_persist, mock_exists):
        bad = {
            "leaveSubmission": {
                **VALID_PAYLOAD["leaveSubmission"],
                "leavePeriod": {
                    **VALID_PAYLOAD["leaveSubmission"]["leavePeriod"],
                    "totalWorkingDays": 99,  # wrong — actual is 15
                },
            }
        }
        resp = client.post("/api/v1/leave-submissions", json=bad)
        assert resp.status_code == 400
        assert "working-day count" in resp.json()["detail"]

    @patch("main.submission_exists", return_value=False)
    @patch("main.persist_submission", side_effect=RuntimeError("DB down"))
    def test_db_error_returns_500(self, mock_persist, mock_exists):
        resp = client.post("/api/v1/leave-submissions", json=VALID_PAYLOAD)
        assert resp.status_code == 500
