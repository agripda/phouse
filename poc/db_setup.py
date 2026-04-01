"""
db_setup.py — SQLite PoC schema initialisation
Version : 2.2
Date    : 2026-04-01
Author  : David Kim
Changes : Added CreatedDatetime / UpdatedDatetime audit fields to
          LeaveSubmission and LeaveDay; SCD type labels in comments.

Self-healing: safe to run multiple times — uses CREATE TABLE IF NOT EXISTS.
"""

import sqlite3
import os
from pathlib import Path


# ---------------------------------------------------------------------------
# Resolve DB path relative to this file — independent of working directory
# ---------------------------------------------------------------------------
# _DIR = os.path.dirname(os.path.abspath(__file__))
# DB_PATH = os.path.join(_DIR, "data", "leave.db")

_DIR = Path(os.path.abspath(__file__)).parent
DB_PATH = _DIR / "data" / "leave.db"

DDL = """
-- ===========================================================================
-- 1. LeaveSubmission  (SCD Type 1)
--    One row per submission.  Status updated in-place.
--    Audit: CreatedDatetime set on INSERT; UpdatedDatetime refreshed on UPDATE.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS LeaveSubmission (
    SubmissionId      TEXT        NOT NULL,
    WorkerId          TEXT        NOT NULL,
    StartDatetime     TEXT        NOT NULL,
    EndDatetime       TEXT        NOT NULL,
    TotalDays         INTEGER     NOT NULL,
    Status            TEXT        NOT NULL,
    SubmittedDate     TEXT        NOT NULL,

    -- Audit fields
    CreatedDatetime   TEXT        NOT NULL  DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    UpdatedDatetime   TEXT        NOT NULL  DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),

    PRIMARY KEY (SubmissionId)
);

-- ===========================================================================
-- 2. LeaveDay  (Fact Table — append-only)
--    One row per Mon–Fri working day.  Rows are never updated after INSERT.
--    Audit: CreatedDatetime only (no UpdatedDatetime).
-- ===========================================================================
CREATE TABLE IF NOT EXISTS LeaveDay (
    LeaveDayId        INTEGER     NOT NULL  PRIMARY KEY AUTOINCREMENT,
    SubmissionId      TEXT        NOT NULL,
    WorkerId          TEXT        NOT NULL,
    LeaveDate         TEXT        NOT NULL,
    LeaveTypeCode     TEXT        NOT NULL,
    LeaveCategory     TEXT        NOT NULL,
    UnitOfMeasure     TEXT        NOT NULL,
    Quantity          REAL        NOT NULL,

    -- Audit field (append-only — no UpdatedDatetime)
    CreatedDatetime   TEXT        NOT NULL  DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),

    FOREIGN KEY (SubmissionId) REFERENCES LeaveSubmission (SubmissionId),
    UNIQUE (SubmissionId, LeaveDate, LeaveTypeCode)
);

CREATE INDEX IF NOT EXISTS IX_LeaveSubmission_WorkerId ON LeaveSubmission (WorkerId);
CREATE INDEX IF NOT EXISTS IX_LeaveDay_SubmissionId    ON LeaveDay (SubmissionId);
CREATE INDEX IF NOT EXISTS IX_LeaveDay_WorkerId_Date   ON LeaveDay (WorkerId, LeaveDate);

-- ===========================================================================
-- 3. DQResult  (Type 0 / Append-Only Event Log)
--    One row per DQ warning per submission.  Rows are never updated.
--    CheckedAt serves as the creation timestamp — no separate audit fields.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS DQResult (
    DQResultId        INTEGER     NOT NULL  PRIMARY KEY AUTOINCREMENT,
    SubmissionId      TEXT        NOT NULL,
    CheckedAt         TEXT        NOT NULL  DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    Domain            TEXT        NOT NULL,
    Severity          TEXT        NOT NULL,
    Code              TEXT        NOT NULL,
    Field             TEXT,
    Message           TEXT,

    FOREIGN KEY (SubmissionId) REFERENCES LeaveSubmission (SubmissionId)
);
"""


def init_db(db_path: str = DB_PATH) -> None:
    """
    Initialise (or self-heal) the SQLite database.
    Creates the data directory if it does not exist.
    Safe to call on every application startup.
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DDL)
        conn.commit()
        print(f"[db_setup] ✅ Database ready: {db_path}")
    finally:
        conn.close()


# Alias for backward compatibility — main.py imports create_database
create_database = init_db

if __name__ == "__main__":
    init_db()