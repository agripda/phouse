# -*- coding: utf-8 -*-
"""
database_sqlite.py — SQLite persistence layer for the Leave Submission PoC.

Mirrors the pyodbc interface from database.py so the API layer
does not need to know which backend is active.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from models import LeaveDayRecord, LeaveSubmissionPayload


# ── Connection ────────────────────────────────────────────────────────────────

def _get_db_path() -> Path:
    return Path(os.environ.get("POWERHOUSE_DB_PATH", "data/leave_poc.db"))


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    """Yield a SQLite connection with FK enforcement and auto commit/rollback."""
    conn = sqlite3.connect(_get_db_path())
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Queries ───────────────────────────────────────────────────────────────────

def submission_exists(submission_id: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM LeaveSubmission WHERE SubmissionId = ?",
            (submission_id,),
        ).fetchone()
        return row is not None


def persist_submission(
    payload: LeaveSubmissionPayload,
    leave_days: list[LeaveDayRecord],
) -> None:
    """Write header + all day rows in one atomic transaction."""
    with get_connection() as conn:

        # 1. Insert submission header
        conn.execute(
            """
            INSERT INTO LeaveSubmission
                (SubmissionId, WorkerId, StartDatetime, EndDatetime,
                 TotalDays, Status, SubmittedDate)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.submissionId,
                payload.worker.workerId,
                payload.leavePeriod.startDate.isoformat(),
                payload.leavePeriod.endDate.isoformat(),
                payload.leavePeriod.totalWorkingDays,
                payload.status,
                payload.submittedDate.isoformat(),
            ),
        )

        # 2. Bulk-insert leave days
        conn.executemany(
            """
            INSERT INTO LeaveDay
                (SubmissionId, WorkerId, LeaveDate, LeaveTypeCode,
                 LeaveCategory, UnitOfMeasure, Quantity)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    payload.submissionId,
                    payload.worker.workerId,
                    day.leaveDate.isoformat(),
                    day.leaveTypeCode,
                    day.leaveCategory,
                    day.unitOfMeasure,
                    float(day.quantity),
                )
                for day in leave_days
            ],
        )


def fetch_submission(submission_id: str) -> dict | None:
    """Return submission header + all day rows for the GET endpoint."""
    with get_connection() as conn:
        header = conn.execute(
            "SELECT * FROM LeaveSubmission WHERE SubmissionId = ?",
            (submission_id,),
        ).fetchone()
        if not header:
            return None

        days = conn.execute(
            "SELECT * FROM LeaveDay WHERE SubmissionId = ? ORDER BY LeaveDate",
            (submission_id,),
        ).fetchall()

        return {
            "submission": dict(header),
            "leaveDays": [dict(d) for d in days],
        }