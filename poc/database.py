# -*- coding: utf-8 -*-
"""
database.py — SQL Server persistence layer.

Drop-in replacement for database_sqlite.py.
Exposes the same public functions so main.py needs no changes:

    get_next_sequence()
    submission_exists(submission_id)
    persist_submission(payload, leave_days)
    persist_dq_results(submission_id, dq_issues)
    get_existing_leave_dates(worker_id)
    fetch_submission(submission_id)

Switch via environment variable in main.py:
    if os.environ.get("POWERHOUSE_DB_ENGINE") == "sqlserver":
        from database import ...
    else:
        from database_sqlite import ...

Prerequisites:
    pip install pyodbc
    Set POWERHOUSE_MSSQL_CONN env var (see _get_conn_str below).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Generator

import pyodbc

from models import LeaveDayRecord, LeaveSubmissionPayload


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} | {level:<8} | {msg}")


# ── Connection ────────────────────────────────────────────────────────────────

def _get_conn_str() -> str:
    """
    Build pyodbc connection string from environment.

    Option A — full connection string:
        POWERHOUSE_MSSQL_CONN=DRIVER={ODBC Driver 18 for SQL Server};SERVER=...

    Option B — individual parts:
        POWERHOUSE_MSSQL_SERVER   (default: localhost)
        POWERHOUSE_MSSQL_DATABASE (default: LeaveDB)
        POWERHOUSE_MSSQL_UID
        POWERHOUSE_MSSQL_PWD
        POWERHOUSE_MSSQL_DRIVER   (default: ODBC Driver 18 for SQL Server)
    """
    conn_str = os.environ.get("POWERHOUSE_MSSQL_CONN")
    if conn_str:
        return conn_str

    server   = os.environ.get("POWERHOUSE_MSSQL_SERVER",   "localhost")
    database = os.environ.get("POWERHOUSE_MSSQL_DATABASE", "LeaveDB")
    uid      = os.environ.get("POWERHOUSE_MSSQL_UID",      "")
    pwd      = os.environ.get("POWERHOUSE_MSSQL_PWD",      "")
    driver   = os.environ.get("POWERHOUSE_MSSQL_DRIVER",   "ODBC Driver 18 for SQL Server")

    auth = f"UID={uid};PWD={pwd};" if uid else "Trusted_Connection=yes;"
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"{auth}"
        f"TrustServerCertificate=yes;"
    )


@contextmanager
def get_connection() -> Generator[pyodbc.Connection, None, None]:
    """Yield a pyodbc connection with auto commit/rollback."""
    conn = pyodbc.connect(_get_conn_str(), autocommit=False)
    _log("[DB] Connected to SQL Server")
    try:
        yield conn
        conn.commit()
        _log("[DB] Transaction committed")
    except Exception as exc:
        conn.rollback()
        _log(f"[DB] Rolled back: {exc}", "ERROR")
        raise
    finally:
        conn.close()
        _log("[DB] Connection closed")


# ── Sequence / existence ──────────────────────────────────────────────────────

def get_next_sequence() -> int:
    """Next global sequence for SubmissionId — mirrors database_sqlite version."""
    _log("[DB.get_next_sequence] Querying MAX global sequence")
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT MAX(CAST(SUBSTRING(SubmissionId, 9, 6) AS INT)) "
            "FROM dbo.LeaveSubmission"
        )
        row = cur.fetchone()
        max_seq  = row[0] if row and row[0] is not None else 0
        next_seq = max_seq + 1
        _log(f"[DB.get_next_sequence] MAX={max_seq} -> next={next_seq}")
        return next_seq


def submission_exists(submission_id: str) -> bool:
    """True if SubmissionId already in dbo.LeaveSubmission."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM dbo.LeaveSubmission WHERE SubmissionId = ?",
            (submission_id,),
        )
        return cur.fetchone() is not None


# ── Core persistence — via SP ─────────────────────────────────────────────────

def persist_submission(
    payload: LeaveSubmissionPayload,
    leave_days: list[LeaveDayRecord],
) -> str:
    """
    Persist header + day rows by calling usp_PersistLeaveSubmission.

    Single SP call replaces:
        database_sqlite.persist_submission()  -- header + day rows
        database_sqlite.persist_dq_results()  -- DQ INSERT (see note below)

    DQ issues are NOT passed here because main.py calls persist_dq_results()
    separately after getting final_id — matching the SQLite PoC flow exactly.
    To collapse into one SP call, pass @DQIssuesJson and remove the
    separate persist_dq_results() call in main.py.
    """
    _log(f"[DB.persist_submission] START submissionId={payload.submissionId} days={len(leave_days)}")

    days_json = json.dumps([
        {
            "leaveDate":     day.leaveDate.isoformat(),
            "leaveTypeCode": day.leaveTypeCode,
            "leaveCategory": day.leaveCategory,
            "unitOfMeasure": day.unitOfMeasure,
            "quantity":      float(day.quantity),
        }
        for day in leave_days
    ])

    with get_connection() as conn:
        cur = conn.cursor()
        # DECLARE + EXEC + SELECT produces multiple result sets.
        # Advance with nextset() until we reach the SELECT @FinalId result.
        cur.execute(
            """
            DECLARE @FinalId VARCHAR(50);
            EXEC dbo.usp_PersistLeaveSubmission
                @SubmissionId  = ?,
                @WorkerId      = ?,
                @StartDatetime = ?,
                @EndDatetime   = ?,
                @TotalDays     = ?,
                @Status        = ?,
                @SubmittedDate = ?,
                @LeaveDaysJson = ?,
                @DQIssuesJson  = NULL,
                @FinalId       = @FinalId OUTPUT;
            SELECT @FinalId;
            """,
            (
                payload.submissionId,
                payload.worker.workerId,
                payload.leavePeriod.startDate.isoformat(),
                payload.leavePeriod.endDate.isoformat(),
                payload.leavePeriod.totalWorkingDays,
                payload.status,
                payload.submittedDate.isoformat(),
                days_json,
            ),
        )
        # Skip any rowcount-only result sets until we find the SELECT result
        row = cur.fetchone()
        while row is None and cur.nextset():
            row = cur.fetchone()

        final_id = row[0] if row else payload.submissionId
        _log(f"[DB.persist_submission] SP completed -> submissionId={final_id}")
        return final_id


def persist_dq_results(submission_id: str, dq_issues: list) -> None:
    """
    Write DQ warning issues to dbo.DQResult.
    Mirrors database_sqlite.persist_dq_results() — same signature.
    """
    if not dq_issues:
        return

    _log(f"[DB.persist_dq_results] Saving {len(dq_issues)} DQ issues for {submission_id}")
    checked_at = datetime.now().isoformat()

    with get_connection() as conn:
        cur = conn.cursor()
        cur.executemany(
            """
            INSERT INTO dbo.DQResult
                (SubmissionId, CheckedAt, Domain, Severity, Code, Field, Message)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    submission_id,
                    checked_at,
                    i["domain"],
                    i["severity"],
                    i["code"],
                    i.get("field", ""),
                    i["message"],
                )
                for i in dq_issues
            ],
        )
    _log(f"[DB.persist_dq_results] Saved {len(dq_issues)} issues")


# ── DQ overlap check ──────────────────────────────────────────────────────────

def get_existing_leave_dates(worker_id: str) -> set:
    """
    Return all LeaveDate strings already recorded for the given worker.
    Mirrors database_sqlite.get_existing_leave_dates() exactly.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT CONVERT(VARCHAR(10), LeaveDate, 120) "
            "FROM dbo.LeaveDay WHERE WorkerId = ?",
            (worker_id,),
        )
        return {r[0] for r in cur.fetchall()}


# ── GET endpoint ──────────────────────────────────────────────────────────────

def fetch_submission(submission_id: str) -> dict | None:
    """
    Return submission header + all day rows for the GET endpoint.
    Mirrors database_sqlite.fetch_submission() exactly.
    """
    _log(f"[DB.fetch_submission] Fetching submissionId={submission_id}")
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM dbo.LeaveSubmission WHERE SubmissionId = ?",
            (submission_id,),
        )
        header = cur.fetchone()
        if not header:
            _log(f"[DB.fetch_submission] Not found: {submission_id}", "WARNING")
            return None

        h_cols = [col[0] for col in cur.description]
        h_dict = dict(zip(h_cols, header))

        cur.execute(
            """
            SELECT LeaveDayId, SubmissionId, WorkerId, LeaveDate,
                   LeaveTypeCode, LeaveCategory, UnitOfMeasure, Quantity
            FROM   dbo.LeaveDay
            WHERE  SubmissionId = ?
            ORDER  BY LeaveDate
            """,
            (submission_id,),
        )
        days     = cur.fetchall()
        day_cols = [col[0] for col in cur.description]

        _log(f"[DB.fetch_submission] Found header + {len(days)} day rows")
        return {
            "submission": h_dict,
            "leaveDays":  [dict(zip(day_cols, d)) for d in days],
        }