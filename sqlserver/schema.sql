-- ============================================================
-- schema.sql — SQL Server DDL for Leave Submission API
-- Mirrors SQLite PoC schema (db_setup.py DDL)
-- Run once against target database before first API startup.
-- ============================================================

USE [LeaveDB];   -- change to your target database
GO

-- ──────────────────────────────────────────────────────────
-- 1. LeaveSubmission  (header — one row per submission)
-- ──────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.LeaveSubmission', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.LeaveSubmission (
        SubmissionId    VARCHAR(50)  NOT NULL,  -- caller-supplied or LS-YYYY-NNNNNN
        WorkerId        VARCHAR(50)  NOT NULL,  -- HRIS external reference
        StartDatetime   DATETIME     NOT NULL,  -- ISO-8601 start incl. time
        EndDatetime     DATETIME     NOT NULL,  -- 23:59:59.99 end-of-day
        TotalDays       INT          NOT NULL,  -- validated working-day count
        Status          VARCHAR(20)  NOT NULL,  -- Submitted / Draft / Pending
        SubmittedDate   DATE         NOT NULL,  -- date only

        CONSTRAINT PK_LeaveSubmission
            PRIMARY KEY CLUSTERED (SubmissionId)
    );

    CREATE INDEX IX_LeaveSubmission_WorkerId
        ON dbo.LeaveSubmission (WorkerId);

    PRINT '[schema] ✅ LeaveSubmission created.';
END
ELSE
    PRINT '[schema] LeaveSubmission already exists — skipped.';
GO

-- ──────────────────────────────────────────────────────────
-- 2. LeaveDay  (detail — one row per Mon-Fri working day)
-- Columns match assessment spec exactly.
-- WorkerId denormalised (Kimball) for fast calendar queries.
-- ──────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.LeaveDay', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.LeaveDay (
        LeaveDayId      INT IDENTITY(1,1) NOT NULL,
        SubmissionId    VARCHAR(50)       NOT NULL,  -- FK → LeaveSubmission
        WorkerId        VARCHAR(50)       NOT NULL,  -- denormalised (Kimball)
        LeaveDate       DATE              NOT NULL,  -- YYYY-MM-DD
        LeaveTypeCode   VARCHAR(10)       NOT NULL,  -- AL / SL / CL …
        LeaveCategory   VARCHAR(20)       NOT NULL,  -- Paid / Unpaid
        UnitOfMeasure   VARCHAR(10)       NOT NULL,  -- Days / Hours
        Quantity        DECIMAL(5,2)      NOT NULL,  -- always 1.00 per row

        CONSTRAINT PK_LeaveDay
            PRIMARY KEY CLUSTERED (LeaveDayId),

        CONSTRAINT FK_LeaveDay_Submission
            FOREIGN KEY (SubmissionId)
            REFERENCES dbo.LeaveSubmission (SubmissionId),

        CONSTRAINT UQ_Submission_Date_Type
            UNIQUE (SubmissionId, LeaveDate, LeaveTypeCode)
    );

    CREATE INDEX IX_LeaveDay_SubmissionId
        ON dbo.LeaveDay (SubmissionId);

    CREATE INDEX IX_LeaveDay_WorkerId_Date
        ON dbo.LeaveDay (WorkerId, LeaveDate);

    PRINT '[schema] ✅ LeaveDay created.';
END
ELSE
    PRINT '[schema] LeaveDay already exists — skipped.';
GO

-- ──────────────────────────────────────────────────────────
-- 3. DQResult  (one row per DQ issue per submission)
-- ⭐ Bonus — beyond assessment scope
-- ──────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.DQResult', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.DQResult (
        DQResultId      INT IDENTITY(1,1) NOT NULL,
        SubmissionId    VARCHAR(50)       NOT NULL,
        CheckedAt       DATETIME          NOT NULL DEFAULT GETDATE(),
        Domain          VARCHAR(20)       NOT NULL,  -- Accuracy / Completeness …
        Severity        VARCHAR(10)       NOT NULL,  -- Critical / Warning
        Code            VARCHAR(10)       NOT NULL,  -- e.g. ACC-001
        Field           VARCHAR(100)      NULL,      -- affected payload field path
        Message         NVARCHAR(MAX)     NOT NULL,

        CONSTRAINT PK_DQResult
            PRIMARY KEY CLUSTERED (DQResultId),

        CONSTRAINT FK_DQResult_Submission
            FOREIGN KEY (SubmissionId)
            REFERENCES dbo.LeaveSubmission (SubmissionId)
    );

    CREATE INDEX IX_DQResult_SubmissionId
        ON dbo.DQResult (SubmissionId);

    CREATE INDEX IX_DQResult_Domain_Severity
        ON dbo.DQResult (Domain, Severity);

    PRINT '[schema] ✅ DQResult created.';
END
ELSE
    PRINT '[schema] DQResult already exists — skipped.';
GO

-- ──────────────────────────────────────────────────────────
-- 4. usp_PersistLeaveSubmission
--    Replaces: database_sqlite.persist_submission()
--              + database_sqlite.persist_dq_results()
--
--    Accepts day rows and DQ issues as JSON strings so the
--    Python caller makes a single SP call per submission
--    (no N+1 inserts, no separate DQ round-trip).
-- ──────────────────────────────────────────────────────────
CREATE OR ALTER PROCEDURE dbo.usp_PersistLeaveSubmission
    -- Submission header fields (mirrors LeaveSubmissionPayload)
    @SubmissionId    VARCHAR(50),
    @WorkerId        VARCHAR(50),
    @StartDatetime   DATETIME,
    @EndDatetime     DATETIME,
    @TotalDays       INT,
    @Status          VARCHAR(20),
    @SubmittedDate   DATE,

    -- JSON arrays serialised by the Python caller
    -- LeaveDay rows: [{"leaveDate":"…","leaveTypeCode":"AL","leaveCategory":"Paid","unitOfMeasure":"Days","quantity":1.0}, …]
    @LeaveDaysJson   NVARCHAR(MAX),

    -- DQ issues: [{"domain":"…","severity":"Warning","code":"ACC-001","field":"…","message":"…"}, …]
    -- Pass NULL or '[]' if no issues.
    @DQIssuesJson    NVARCHAR(MAX) = NULL,

    -- OUTPUT: echoes back the SubmissionId used (caller confirmation)
    @FinalId         VARCHAR(50)   OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;   -- any error auto-rolls back the whole transaction

    BEGIN TRANSACTION;

    -- ── 1. Duplicate guard ──────────────────────────────────────────────
    IF EXISTS (
        SELECT 1
        FROM   dbo.LeaveSubmission WITH (UPDLOCK, HOLDLOCK)
        WHERE  SubmissionId = @SubmissionId
    )
    BEGIN
        ROLLBACK;
        THROW 50409, 'SubmissionId already exists.', 1;
        -- Python caller catches error number 50409 → HTTP 409
    END;

    -- ── 2. Insert submission header ─────────────────────────────────────
    INSERT INTO dbo.LeaveSubmission
        (SubmissionId, WorkerId, StartDatetime, EndDatetime,
         TotalDays,    Status,   SubmittedDate)
    VALUES
        (@SubmissionId, @WorkerId, @StartDatetime, @EndDatetime,
         @TotalDays,    @Status,   @SubmittedDate);

    -- ── 3. Bulk-insert LeaveDay rows from JSON ──────────────────────────
    --    OPENJSON + set-based INSERT replaces Python executemany loop.
    --    For a 15-day submission: 1 SP call vs 15 individual round-trips.
    INSERT INTO dbo.LeaveDay
        (SubmissionId, WorkerId, LeaveDate,
         LeaveTypeCode, LeaveCategory, UnitOfMeasure, Quantity)
    SELECT
        @SubmissionId,
        @WorkerId,
        CAST(j.LeaveDate    AS DATE),
        j.LeaveTypeCode,
        j.LeaveCategory,
        j.UnitOfMeasure,
        CAST(j.Quantity     AS DECIMAL(5,2))
    FROM OPENJSON(@LeaveDaysJson)
    WITH (
        LeaveDate       VARCHAR(10)  '$.leaveDate',
        LeaveTypeCode   VARCHAR(10)  '$.leaveTypeCode',
        LeaveCategory   VARCHAR(20)  '$.leaveCategory',
        UnitOfMeasure   VARCHAR(10)  '$.unitOfMeasure',
        Quantity        FLOAT        '$.quantity'
    ) AS j;

    -- ── 4. Insert DQ issues (warning-severity only; Critical never reaches here)
    IF @DQIssuesJson IS NOT NULL AND LEN(@DQIssuesJson) > 2  -- '[]' = 2 chars
    BEGIN
        INSERT INTO dbo.DQResult
            (SubmissionId, CheckedAt, Domain, Severity, Code, Field, Message)
        SELECT
            @SubmissionId,
            GETDATE(),
            d.Domain,
            d.Severity,
            d.Code,
            d.Field,
            d.Message
        FROM OPENJSON(@DQIssuesJson)
        WITH (
            Domain    VARCHAR(20)    '$.domain',
            Severity  VARCHAR(10)    '$.severity',
            Code      VARCHAR(10)    '$.code',
            Field     VARCHAR(100)   '$.field',
            Message   NVARCHAR(MAX)  '$.message'
        ) AS d;
    END;

    COMMIT;
    SET @FinalId = @SubmissionId;
END;
GO

PRINT '[schema] ✅ usp_PersistLeaveSubmission created / updated.';
GO