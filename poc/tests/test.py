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

# submissionId omitted — server generates it
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
        # DB already has 5 submissions — next should be 000006
        sid = generate_submission_id(
            get_next_seq_fn=lambda: 6,
            exists_fn=lambda _: False,
            now=datetime(2026, 3, 27, 10, 30),
        )
        assert sid == "LS-2026-000006"

    def test_collision_safety_increments(self):
        # get_next_seq says 3, but LS-2026-000003 already exists
        existing = {"LS-2026-000003"}
        sid = generate_submission_id(
            get_next_seq_fn=lambda: 3,
            exists_fn=lambda x: x in existing,
            now=datetime(2026, 3, 27, 10, 30),
        )
        assert sid == "LS-2026-000004"

    def test_year_changes_prefix_sequence_continues(self):
        # In 2027, global seq continues from 1000 (not reset to 1)
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


# ── Integration: POST endpoint ────────────────────────────────────────────────

class TestPostLeaveSubmissions:
    def test_valid_payload_returns_201_with_generated_id(self):
        resp = client.post("/api/v1/leave-submissions", json=VALID_PAYLOAD)
        assert resp.status_code == 201
        body = resp.json()
        # LS-YYYY-NNNNNN format e.g. LS-2026-000001
        assert body["submissionId"].startswith("LS-")
        assert len(body["submissionId"]) == 16
        assert body["totalWorkingDaysCreated"] == 15
        assert len(body["leaveDays"]) == 15

    def test_two_submissions_get_unique_ids(self):
        r1 = client.post("/api/v1/leave-submissions", json=VALID_PAYLOAD)
        r2 = client.post("/api/v1/leave-submissions", json=VALID_PAYLOAD)
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["submissionId"] != r2.json()["submissionId"]

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


# ── Integration: GET endpoint ─────────────────────────────────────────────────

class TestGetLeaveSubmission:
    def test_get_existing_returns_200(self):
        post_resp = client.post("/api/v1/leave-submissions", json=VALID_PAYLOAD)
        assert post_resp.status_code == 201
        generated_id = post_resp.json()["submissionId"]

        get_resp = client.get(f"/api/v1/leave-submissions/{generated_id}")
        assert get_resp.status_code == 200
        body = get_resp.json()
        assert body["submission"]["SubmissionId"] == generated_id
        assert len(body["leaveDays"]) == 15

    def test_get_missing_returns_404(self):
        assert client.get("/api/v1/leave-submissions/DOES-NOT-EXIST").status_code == 404


# ── Health check ──────────────────────────────────────────────────────────────

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"