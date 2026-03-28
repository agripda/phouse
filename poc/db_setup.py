# -*- coding: utf-8 -*-
"""
db_setup.py — Create and seed the SQLite database.

Creates:  data/leave.db
Tables:   LeaveSubmission (header), LeaveDay (detail)

Schema design decisions
-----------------------
LeaveSubmission
  - SubmissionId (TEXT PK) — spec-defined PK, server-generated LS-YYYY-NNNNNN
  - WorkerId kept as plain TEXT — Worker master data owned by external HRIS

LeaveDay
  - Matches spec columns exactly
  - SubmissionId FK → LeaveSubmission.SubmissionId
  - WorkerId denormalised intentionally (Kimball pattern) for fast calendar queries
  - UNIQUE on (SubmissionId, LeaveDate, LeaveTypeCode) — within-submission dedup
"""

import sqlite3
from pathlib import Path

# ── Path ──────────────────────────────────────────────────────────────────────
DB_DIR  = Path(__file__).parent / "data"
DB_PATH = DB_DIR / "leave.db"

DDL = """
-- -----------------------------------------------------------------------
-- LeaveSubmission  (header — one row per submission)
-- SubmissionId is the spec-defined PK: server-generated LS-YYYY-NNNNNN
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS LeaveSubmission (
    SubmissionId    TEXT    NOT NULL,   -- server-generated LS-YYYY-NNNNNN
    WorkerId        TEXT    NOT NULL,   -- HRIS external reference
    StartDatetime   TEXT    NOT NULL,   -- ISO-8601
    EndDatetime     TEXT    NOT NULL,   -- ISO-8601
    TotalDays       INTEGER NOT NULL,
    Status          TEXT    NOT NULL,
    SubmittedDate   TEXT    NOT NULL,   -- YYYY-MM-DD

    CONSTRAINT PK_LeaveSubmission
        PRIMARY KEY (SubmissionId)
);

-- -----------------------------------------------------------------------
-- LeaveDay  (detail — one row per Mon-Fri working day)
-- Columns match spec exactly; WorkerId denormalised for calendar queries
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS LeaveDay (
    LeaveDayId      INTEGER NOT NULL,
    SubmissionId    TEXT    NOT NULL,   -- FK -> LeaveSubmission.SubmissionId
    WorkerId        TEXT    NOT NULL,   -- denormalised (Kimball pattern)
    LeaveDate       TEXT    NOT NULL,   -- YYYY-MM-DD
    LeaveTypeCode   TEXT    NOT NULL,   -- AL / SL / CL ...
    LeaveCategory   TEXT    NOT NULL,   -- Paid / Unpaid
    UnitOfMeasure   TEXT    NOT NULL,   -- Days / Hours
    Quantity        REAL    NOT NULL,   -- always 1.00 per day row

    CONSTRAINT PK_LeaveDay
        PRIMARY KEY (LeaveDayId AUTOINCREMENT),

    CONSTRAINT FK_LeaveDay_Submission
        FOREIGN KEY (SubmissionId)
        REFERENCES LeaveSubmission (SubmissionId),

    CONSTRAINT UQ_Submission_Date_Type
        UNIQUE (SubmissionId, LeaveDate, LeaveTypeCode)
);

-- -----------------------------------------------------------------------
-- DQResult  (one row per DQ issue per submission)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DQResult (
    DQResultId      INTEGER NOT NULL,
    SubmissionId    TEXT    NOT NULL,
    CheckedAt       TEXT    NOT NULL,   -- ISO-8601 timestamp
    Domain          TEXT    NOT NULL,   -- Accuracy / Completeness / etc.
    Severity        TEXT    NOT NULL,   -- Critical / Warning
    Code            TEXT    NOT NULL,   -- e.g. ACC-001
    Field           TEXT,              -- affected field path
    Message         TEXT    NOT NULL,

    CONSTRAINT PK_DQResult
        PRIMARY KEY (DQResultId AUTOINCREMENT),

    CONSTRAINT FK_DQResult_Submission
        FOREIGN KEY (SubmissionId)
        REFERENCES LeaveSubmission (SubmissionId)
);

CREATE INDEX IF NOT EXISTS IX_DQResult_SubmissionId
    ON DQResult (SubmissionId);

CREATE INDEX IF NOT EXISTS IX_DQResult_Domain_Severity
    ON DQResult (Domain, Severity);


CREATE INDEX IF NOT EXISTS IX_LeaveSubmission_WorkerId
    ON LeaveSubmission (WorkerId);

CREATE INDEX IF NOT EXISTS IX_LeaveDay_SubmissionId
    ON LeaveDay (SubmissionId);

CREATE INDEX IF NOT EXISTS IX_LeaveDay_WorkerId_Date
    ON LeaveDay (WorkerId, LeaveDate);
"""


def create_database() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(DDL)
    conn.commit()
    conn.close()
    print(f"[db_setup] ✅ Database ready at: {DB_PATH}")


if __name__ == "__main__":
    create_database()