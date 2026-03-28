# -*- coding: utf-8 -*-
"""
database_sqlite.py — SQLite persistence layer.

Schema is applied on every connection open (all DDL uses IF NOT EXISTS)
so the database is self-healing — no need to run db_setup.py manually.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from db_setup import DDL, DB_PATH as SETUP_DB_PATH
from models import LeaveDayRecord, LeaveSubmissionPayload


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log(msg: str, level: str = "INFO") -> None:
    """Standalone print logger for database_sqlite — no logger dependency."""
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} | {level:<8} | {msg}")


# ── Connection ────────────────────────────────────────────────────────────────

def _get_db_path() -> Path:
    env_path = os.environ.get("POWERHOUSE_DB_PATH")
    # Base directory = the poc/ folder (where this file lives)
    base_dir = Path(__file__).parent

    if env_path:
        p = Path(env_path)
        if not p.is_absolute():
            p = (base_dir / p).resolve()
        rel = p.relative_to(base_dir) if p.is_relative_to(base_dir) else p
        _log(f"[DB] POWERHOUSE_DB_PATH={env_path} → {rel}")
    else:
        p = (base_dir / "data" / "leave.db").resolve()
        rel = p.relative_to(base_dir)
        _log(f"[DB] POWERHOUSE_DB_PATH not set → default: {rel}")
    return p


def _check_constraints(conn: sqlite3.Connection) -> None:
    """Print actual UNIQUE constraints on LeaveDay for verification."""
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='LeaveDay'"
    ).fetchall()
    if rows:
        _log(f"[DB] LeaveDay DDL in DB:\n{rows[0][0]}")
    else:
        _log("[DB] LeaveDay table does not exist yet (will be created)")

    # Also show index list
    idx_rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='LeaveDay'"
    ).fetchall()
    for idx in idx_rows:
        _log(f"[DB] Index: {idx[0]} → {idx[1]}")


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    """
    Yield a SQLite connection with FK enforcement and auto commit/rollback.
    Applies DDL on every open (idempotent — all statements use IF NOT EXISTS).
    """
    db_path = _get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    base_dir  = Path(__file__).parent
    db_rel    = db_path.relative_to(base_dir) if db_path.is_relative_to(base_dir) else db_path
    setup_rel = SETUP_DB_PATH.relative_to(base_dir) if SETUP_DB_PATH.is_relative_to(base_dir) else SETUP_DB_PATH

    _log(f"[DB] Connecting to: {db_rel}")
    _log(f"[DB] db_setup.DB_PATH: {setup_rel}")

    if db_path.resolve() != SETUP_DB_PATH.resolve():
        _log(f"[DB] ⚠️  PATH MISMATCH — {db_rel} vs {setup_rel}", "WARNING")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")

    # Show constraints BEFORE applying DDL
    _log("[DB] --- Constraints BEFORE DDL ---")
    _check_constraints(conn)

    # Show which DDL will be applied
    uq_line = next(
        (l.strip() for l in DDL.splitlines() if "UNIQUE" in l and "LeaveDay" not in l and "Submission" in l or
         "UNIQUE" in l and "WorkerId" in l or
         "UNIQUE" in l and "SubmissionId" in l and "LeaveDate" in l),
        "not found"
    )
    _log(f"[DB] DDL UNIQUE constraint line: {uq_line}")

    conn.executescript(DDL)

    # Show constraints AFTER applying DDL
    _log("[DB] --- Constraints AFTER DDL ---")
    _check_constraints(conn)

    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as exc:
        _log(f"[DB] ❌ Exception — rolling back: {exc}", "ERROR")
        conn.rollback()
        raise
    finally:
        conn.close()
        _log("[DB] Connection closed")


# ── Queries ───────────────────────────────────────────────────────────────────

def get_next_sequence() -> int:
    """
    Return the next global sequence number for SubmissionId generation.

    Parses the numeric suffix of all existing SubmissionIds (LS-YYYY-NNNNNN)
    and returns MAX + 1, regardless of year. Returns 1 if no submissions exist.
    """
    _log("[DB.get_next_sequence] Querying MAX global sequence")
    with get_connection() as conn:
        row = conn.execute(
            # Extract the 6-digit suffix after the second '-' and cast to int
            "SELECT MAX(CAST(SUBSTR(SubmissionId, 9) AS INTEGER)) FROM LeaveSubmission"
        ).fetchone()
        max_seq = row[0] if row[0] is not None else 0
        next_seq = max_seq + 1
        _log(f"[DB.get_next_sequence] MAX={max_seq} → next={next_seq}")
        return next_seq


def submission_exists(submission_id: str) -> bool:
    _log(f"[DB.submission_exists] Checking submissionId={submission_id}")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM LeaveSubmission WHERE SubmissionId = ?",
            (submission_id,),
        ).fetchone()
        exists = row is not None
        _log(f"[DB.submission_exists] Result: {exists}")
        return exists


def persist_submission(
    payload: LeaveSubmissionPayload,
    leave_days: list[LeaveDayRecord],
) -> str:
    """
    Write header + all day rows in one atomic transaction.
    SubmissionId is finalised inside the transaction (SELECT MAX → +1)
    so concurrent submissions never collide — SQLite holds a DB-level
    write lock for the entire transaction.
    Returns the finalised SubmissionId.
    """
    _log(f"[DB.persist_submission] START workerId={payload.worker.workerId} days={len(leave_days)}")
    with get_connection() as conn:

        # Atomic sequence: MAX read + INSERT happen under the same write lock
        row = conn.execute(
            "SELECT MAX(CAST(SUBSTR(SubmissionId, 9) AS INTEGER)) FROM LeaveSubmission"
        ).fetchone()
        max_seq  = row[0] if row and row[0] is not None else 0
        next_seq = max_seq + 1
        year     = payload.submittedDate.strftime("%Y")
        final_id = f"LS-{year}-{next_seq:06d}"
        _log(f"[DB.persist_submission] Atomic sequence → {final_id} (MAX was {max_seq})")

        # 1. Insert submission header
        _log(f"[DB.persist_submission] Inserting LeaveSubmission header")
        conn.execute(
            """
            INSERT INTO LeaveSubmission
                (SubmissionId, WorkerId, StartDatetime, EndDatetime,
                 TotalDays, Status, SubmittedDate)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                final_id,
                payload.worker.workerId,
                payload.leavePeriod.startDate.isoformat(),
                payload.leavePeriod.endDate.isoformat(),
                payload.leavePeriod.totalWorkingDays,
                payload.status,
                payload.submittedDate.isoformat(),
            ),
        )
        _log(f"[DB.persist_submission] ✅ LeaveSubmission inserted as {final_id}")

        # 2. Bulk-insert leave days
        rows = [
            (
                final_id,
                payload.worker.workerId,
                day.leaveDate.isoformat(),
                day.leaveTypeCode,
                day.leaveCategory,
                day.unitOfMeasure,
                float(day.quantity),
            )
            for day in leave_days
        ]
        _log(f"[DB.persist_submission] Inserting {len(rows)} LeaveDay rows")
        for r in rows:
            _log(f"[DB.persist_submission]   → {r}")

        conn.executemany(
            """
            INSERT INTO LeaveDay
                (SubmissionId, WorkerId, LeaveDate, LeaveTypeCode,
                 LeaveCategory, UnitOfMeasure, Quantity)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        _log(f"[DB.persist_submission] ✅ LeaveDay rows inserted")
        return final_id


def fetch_submission(submission_id: str) -> dict | None:
    """Return submission header + all day rows for the GET endpoint."""
    _log(f"[DB.fetch_submission] Fetching submissionId={submission_id}")
    with get_connection() as conn:
        header = conn.execute(
            "SELECT * FROM LeaveSubmission WHERE SubmissionId = ?",
            (submission_id,),
        ).fetchone()
        if not header:
            _log(f"[DB.fetch_submission] Not found: {submission_id}", "WARNING")
            return None

        days = conn.execute(
            """
            SELECT LeaveDayId, SubmissionId, WorkerId, LeaveDate,
                   LeaveTypeCode, LeaveCategory, UnitOfMeasure, Quantity
            FROM   LeaveDay
            WHERE  SubmissionId = ?
            ORDER  BY LeaveDate
            """,
            (submission_id,),
        ).fetchall()

        _log(f"[DB.fetch_submission] Found header + {len(days)} day rows")
        return {
            "submission": dict(header),
            "leaveDays": [dict(d) for d in days],
        }


# ── DQ helpers ────────────────────────────────────────────────────────────────

def get_existing_leave_dates(worker_id: str) -> set:
    """Return all LeaveDate strings already recorded for the given worker."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT LeaveDate FROM LeaveDay WHERE WorkerId = ?",
            (worker_id,),
        ).fetchall()
        return {r["LeaveDate"] for r in rows}


def persist_dq_results(submission_id: str, dq_issues: list) -> None:
    """Write DQ issues to the DQResult table."""
    if not dq_issues:
        return
    checked_at = __import__("datetime").datetime.now().isoformat()
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO DQResult
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
    _log(f"[DB.persist_dq_results] Saved {len(dq_issues)} DQ issues for {submission_id}")