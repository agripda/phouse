# -*- coding: utf-8 -*-
"""
tests/test.py — Test suite (in-memory SQLite, no file I/O)

Run:  pytest tests/ -v
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

# Point to an in-memory DB for tests
os.environ["POWERHOUSE_DB_PATH"] = ":memory:"
os.environ["POWERHOUSE_LEAVE_API_ENABLED"] = "true"
os.environ["POWERHOUSE_LOG_DATAPATH"] = "logs"

from main import app
from business_logic import generate_submission_id, working_days_in_range
from database_sqlite import get_connection
from db_setup import DDL
from models import LeaveDetail, LeavePeriod


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def setup_in_memory_db(monkeypatch):
    """
    Fresh in-memory SQLite schema for every test.
    Monkeypatches get_connection so all layers share the same connection.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(DDL)
    conn.commit()

    @contextmanager
    def mock_get_connection():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr("database_sqlite.get_connection", mock_get_connection)
    yield
    conn.close()


client = TestClient(app)

# Base payload — no submissionId (server generates)
VALID_PAYLOAD = {
    "leaveSubmission": {
        "submittedDate": "2026-02-15",
        "status": "Submitted",
        "worker": {
            "workerId": "W123456",
            "employeeNumber": "90030366",
            "sourceSystem": "HRIS",
        },
        "leavePeriod": {
            "startDate": "2026-03-02 00:00:00",
            "endDate": "2026-03-20 23:59:59",
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
        "approver": {"approverId": "M987654", "approvalStatus": "Pending"},
        "comments": "Planned annual leave.",
    }
}

# Spec payload — caller supplies SubmissionId (per assessment spec)
SPEC_PAYLOAD = {
    "leaveSubmission": {
        **VALID_PAYLOAD["leaveSubmission"],
        "submissionId": "LS-2026-000123",
    }
}


# ── Unit: generate_submission_id ──────────────────────────────────────────────

class TestGenerateSubmissionId:
    def test_format_first_submission(self):
        sid = generate_submission_id(
            get_next_seq_fn=lambda: 1,
            exists_fn=lambda _: False,
            now=datetime(2026, 3, 27, 10, 30),
        )
        assert sid == "LS-2026-000001"

    def test_global_sequence_continues_from_db_max(self):
        sid = generate_submission_id(
            get_next_seq_fn=lambda: 6,
            exists_fn=lambda _: False,
            now=datetime(2026, 3, 27, 10, 30),
        )
        assert sid == "LS-2026-000006"

    def test_collision_safety_increments(self):
        existing = {"LS-2026-000003"}
        sid = generate_submission_id(
            get_next_seq_fn=lambda: 3,
            exists_fn=lambda x: x in existing,
            now=datetime(2026, 3, 27, 10, 30),
        )
        assert sid == "LS-2026-000004"

    def test_year_changes_prefix_sequence_continues(self):
        sid = generate_submission_id(
            get_next_seq_fn=lambda: 1000,
            exists_fn=lambda _: False,
            now=datetime(2027, 1, 1),
        )
        assert sid == "LS-2027-001000"


# ── Unit: working_days_in_range ───────────────────────────────────────────────

class TestWorkingDays:
    def test_3_week_period(self):
        assert len(working_days_in_range(date(2026, 3, 2), date(2026, 3, 20))) == 15

    def test_no_weekends(self):
        days = working_days_in_range(date(2026, 3, 2), date(2026, 3, 20))
        assert all(d.weekday() < 5 for d in days)

    def test_weekend_only_range(self):
        assert working_days_in_range(date(2026, 3, 7), date(2026, 3, 8)) == []

    def test_single_day(self):
        assert len(working_days_in_range(date(2026, 3, 2), date(2026, 3, 2))) == 1


# ── Integration: POST — server-generated SubmissionId (no submissionId in payload) ──

class TestPostServerGeneratedId:
    def test_valid_payload_returns_201_with_generated_id(self):
        resp = client.post("/api/v1/leave-submissions", json=VALID_PAYLOAD)
        assert resp.status_code == 201
        body = resp.json()
        # LS-YYYY-NNNNNN = 16 chars
        assert body["submissionId"].startswith("LS-")
        assert len(body["submissionId"]) == 16
        assert body["totalWorkingDaysCreated"] == 15
        assert len(body["leaveDays"]) == 15
        assert "dq_issues" in body          # field always present

    def test_two_submissions_without_id_get_unique_ids(self):
        r1 = client.post("/api/v1/leave-submissions", json=VALID_PAYLOAD)
        r2 = client.post("/api/v1/leave-submissions", json=VALID_PAYLOAD)
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["submissionId"] != r2.json()["submissionId"]

    def test_response_contains_dq_issues_array(self):
        resp = client.post("/api/v1/leave-submissions", json=VALID_PAYLOAD)
        assert resp.status_code == 201
        body = resp.json()
        assert isinstance(body["dq_issues"], list)


# ── Integration: POST — caller-supplied SubmissionId (per assessment spec) ────

class TestPostCallerSuppliedId:
    def test_caller_supplied_id_is_used(self):
        """Assessment spec: submissionId provided in payload must be honoured."""
        resp = client.post("/api/v1/leave-submissions", json=SPEC_PAYLOAD)
        assert resp.status_code == 201
        assert resp.json()["submissionId"] == "LS-2026-000123"

    def test_duplicate_caller_supplied_id_returns_409(self):
        """Second submission with same SubmissionId must return 409 Conflict."""
        client.post("/api/v1/leave-submissions", json=SPEC_PAYLOAD)
        resp = client.post("/api/v1/leave-submissions", json=SPEC_PAYLOAD)
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    def test_different_caller_ids_both_succeed(self):
        p1 = {"leaveSubmission": {**SPEC_PAYLOAD["leaveSubmission"], "submissionId": "LS-2026-000100"}}
        p2 = {"leaveSubmission": {**SPEC_PAYLOAD["leaveSubmission"], "submissionId": "LS-2026-000101"}}
        assert client.post("/api/v1/leave-submissions", json=p1).status_code == 201
        assert client.post("/api/v1/leave-submissions", json=p2).status_code == 201


# ── Integration: POST — validation errors ─────────────────────────────────────

class TestPostValidation:
    def test_start_after_end_returns_422(self):
        bad = {
            "leaveSubmission": {
                **VALID_PAYLOAD["leaveSubmission"],
                "leavePeriod": {
                    "startDate": "2026-03-20 00:00:00",
                    "endDate": "2026-03-02 23:59:59",
                    "totalWeeks": 3,
                    "totalWorkingDays": 15,
                },
            }
        }
        assert client.post("/api/v1/leave-submissions", json=bad).status_code == 422

    def test_wrong_total_days_returns_400(self):
        bad = {
            "leaveSubmission": {
                **VALID_PAYLOAD["leaveSubmission"],
                "leavePeriod": {
                    **VALID_PAYLOAD["leaveSubmission"]["leavePeriod"],
                    "totalWorkingDays": 99,
                },
            }
        }
        assert client.post("/api/v1/leave-submissions", json=bad).status_code == 400

    def test_missing_worker_id_returns_422(self):
        bad = {
            "leaveSubmission": {
                **VALID_PAYLOAD["leaveSubmission"],
                "worker": {"employeeNumber": "x", "sourceSystem": "HRIS"},
            }
        }
        assert client.post("/api/v1/leave-submissions", json=bad).status_code == 422

    def test_leave_days_count_in_response(self):
        """Each Mon-Fri day in range must produce exactly one LeaveDay row."""
        resp = client.post("/api/v1/leave-submissions", json=VALID_PAYLOAD)
        assert resp.status_code == 201
        body = resp.json()
        assert body["totalWorkingDaysCreated"] == 15
        assert all(d["leaveTypeCode"] == "AL" for d in body["leaveDays"])
        assert all(d["quantity"] == 1.0 for d in body["leaveDays"])
        # No weekends in leaveDays
        from datetime import date as _date
        for d in body["leaveDays"]:
            assert _date.fromisoformat(d["leaveDate"]).weekday() < 5


# ── Integration: GET endpoint ─────────────────────────────────────────────────

class TestGetLeaveSubmission:
    def test_get_server_generated_id_returns_200(self):
        post_resp = client.post("/api/v1/leave-submissions", json=VALID_PAYLOAD)
        assert post_resp.status_code == 201
        generated_id = post_resp.json()["submissionId"]

        get_resp = client.get(f"/api/v1/leave-submissions/{generated_id}")
        assert get_resp.status_code == 200
        body = get_resp.json()
        assert body["submission"]["SubmissionId"] == generated_id
        assert len(body["leaveDays"]) == 15

    def test_get_caller_supplied_id_returns_200(self):
        """Caller-supplied SubmissionId must be retrievable via GET."""
        client.post("/api/v1/leave-submissions", json=SPEC_PAYLOAD)
        get_resp = client.get("/api/v1/leave-submissions/LS-2026-000123")
        assert get_resp.status_code == 200
        assert get_resp.json()["submission"]["SubmissionId"] == "LS-2026-000123"

    def test_get_missing_returns_404(self):
        assert client.get("/api/v1/leave-submissions/DOES-NOT-EXIST").status_code == 404

    def test_get_response_structure(self):
        """GET response must include submission header and leaveDays list."""
        post_resp = client.post("/api/v1/leave-submissions", json=VALID_PAYLOAD)
        sid = post_resp.json()["submissionId"]
        body = client.get(f"/api/v1/leave-submissions/{sid}").json()
        assert "submission" in body
        assert "leaveDays" in body
        assert body["submission"]["WorkerId"] == "W123456"
        assert body["submission"]["TotalDays"] == 15


# ── Health check ──────────────────────────────────────────────────────────────

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"