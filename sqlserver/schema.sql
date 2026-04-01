-- =============================================================================
-- Leave Submission API — SQL Server Production Schema
-- Version : 2.2
-- Date    : 2026-04-01
-- Author  : David Kim
-- Changes : Added CreatedDatetime / UpdatedDatetime audit fields to
--           LeaveSubmission and LeaveDay; SCD type labels in comments.
-- =============================================================================

USE LeaveDB;
GO

-- -----------------------------------------------------------------------------
-- 1. LeaveSubmission  (SCD Type 1)
--    One row per submission.  Status updated in-place.
--    Audit: CreatedDatetime set on INSERT; UpdatedDatetime refreshed on UPDATE.
-- -----------------------------------------------------------------------------
IF OBJECT_ID('dbo.LeaveSubmission', 'U') IS NOT NULL
    DROP TABLE dbo.LeaveSubmission;
GO

CREATE TABLE dbo.LeaveSubmission (
    SubmissionId      VARCHAR(50)   NOT NULL,
    WorkerId          VARCHAR(50)   NOT NULL,
    StartDatetime     DATETIME      NOT NULL,
    EndDatetime       DATETIME      NOT NULL,
    TotalDays         INT           NOT NULL,
    Status            VARCHAR(20)   NOT NULL,
    SubmittedDate     DATE          NOT NULL,

    -- Audit fields
    CreatedDatetime   DATETIME      NOT NULL  CONSTRAINT DF_LeaveSubmission_Created  DEFAULT GETDATE(),
    UpdatedDatetime   DATETIME      NOT NULL  CONSTRAINT DF_LeaveSubmission_Updated  DEFAULT GETDATE(),

    CONSTRAINT PK_LeaveSubmission PRIMARY KEY (SubmissionId)
);
GO

-- -----------------------------------------------------------------------------
-- 2. LeaveDay  (Fact Table — append-only)
--    One row per Mon–Fri working day.  Rows are never updated after INSERT.
--    Audit: CreatedDatetime only (no UpdatedDatetime).
-- -----------------------------------------------------------------------------
IF OBJECT_ID('dbo.LeaveDay', 'U') IS NOT NULL
    DROP TABLE dbo.LeaveDay;
GO

CREATE TABLE dbo.LeaveDay (
    LeaveDayId        INT           NOT NULL  IDENTITY(1,1),
    SubmissionId      VARCHAR(50)   NOT NULL,
    WorkerId          VARCHAR(50)   NOT NULL,
    LeaveDate         DATE          NOT NULL,
    LeaveTypeCode     VARCHAR(10)   NOT NULL,
    LeaveCategory     VARCHAR(20)   NOT NULL,
    UnitOfMeasure     VARCHAR(10)   NOT NULL,
    Quantity          DECIMAL(5,2)  NOT NULL,

    -- Audit field (append-only — no UpdatedDatetime)
    CreatedDatetime   DATETIME      NOT NULL  CONSTRAINT DF_LeaveDay_Created  DEFAULT GETDATE(),

    CONSTRAINT PK_LeaveDay        PRIMARY KEY (LeaveDayId),
    CONSTRAINT FK_LeaveDay_Sub    FOREIGN KEY (SubmissionId)
                                  REFERENCES dbo.LeaveSubmission (SubmissionId),
    CONSTRAINT UQ_LeaveDay        UNIQUE (SubmissionId, LeaveDate, LeaveTypeCode)
);
GO

CREATE INDEX IX_LeaveSubmission_WorkerId  ON dbo.LeaveSubmission (WorkerId);
CREATE INDEX IX_LeaveDay_SubmissionId    ON dbo.LeaveDay (SubmissionId);
CREATE INDEX IX_LeaveDay_WorkerId_Date   ON dbo.LeaveDay (WorkerId, LeaveDate);
GO

-- -----------------------------------------------------------------------------
-- 3. DQResult  (Type 0 / Append-Only Event Log)
--    One row per DQ warning per submission.  Rows are never updated.
--    CheckedAt serves as the creation timestamp — no separate audit fields.
-- -----------------------------------------------------------------------------
IF OBJECT_ID('dbo.DQResult', 'U') IS NOT NULL
    DROP TABLE dbo.DQResult;
GO

CREATE TABLE dbo.DQResult (
    DQResultId        INT           NOT NULL  IDENTITY(1,1),
    SubmissionId      VARCHAR(50)   NOT NULL,
    CheckedAt         DATETIME      NOT NULL  CONSTRAINT DF_DQResult_CheckedAt  DEFAULT GETDATE(),
    Domain            VARCHAR(20)   NOT NULL,
    Severity          VARCHAR(10)   NOT NULL,
    Code              VARCHAR(10)   NOT NULL,
    Field             VARCHAR(100)  NULL,
    Message           TEXT          NULL,

    CONSTRAINT PK_DQResult        PRIMARY KEY (DQResultId),
    CONSTRAINT FK_DQResult_Sub    FOREIGN KEY (SubmissionId)
                                  REFERENCES dbo.LeaveSubmission (SubmissionId)
);
GO

-- =============================================================================
-- 4. usp_PersistLeaveSubmission
--    Atomic INSERT of LeaveSubmission header + LeaveDay rows + DQResult rows.
--    Duplicate guard uses UPDLOCK/HOLDLOCK to prevent concurrent race.
--    Audit fields are set via column DEFAULT — no explicit value required.
-- =============================================================================
IF OBJECT_ID('dbo.usp_PersistLeaveSubmission', 'P') IS NOT NULL
    DROP PROCEDURE dbo.usp_PersistLeaveSubmission;
GO

CREATE PROCEDURE dbo.usp_PersistLeaveSubmission
    @SubmissionId     VARCHAR(50),
    @WorkerId         VARCHAR(50),
    @StartDatetime    DATETIME,
    @EndDatetime      DATETIME,
    @TotalDays        INT,
    @Status           VARCHAR(20),
    @SubmittedDate    DATE,
    @LeaveDaysJson    NVARCHAR(MAX),   -- JSON array of LeaveDay rows
    @DQIssuesJson     NVARCHAR(MAX)    -- JSON array of DQResult rows (may be '[]')
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;  -- auto-rollback on any error

    BEGIN TRANSACTION;

        -- Duplicate guard
        IF EXISTS (
            SELECT 1
            FROM dbo.LeaveSubmission WITH (UPDLOCK, HOLDLOCK)
            WHERE SubmissionId = @SubmissionId
        )
        BEGIN
            THROW 50409, 'Duplicate SubmissionId', 1;
        END

        -- Insert header (CreatedDatetime + UpdatedDatetime set by DEFAULT)
        INSERT INTO dbo.LeaveSubmission
            (SubmissionId, WorkerId, StartDatetime, EndDatetime,
             TotalDays, Status, SubmittedDate)
        VALUES
            (@SubmissionId, @WorkerId, @StartDatetime, @EndDatetime,
             @TotalDays, @Status, @SubmittedDate);

        -- Bulk insert day rows (CreatedDatetime set by DEFAULT)
        INSERT INTO dbo.LeaveDay
            (SubmissionId, WorkerId, LeaveDate, LeaveTypeCode,
             LeaveCategory, UnitOfMeasure, Quantity)
        SELECT
            @SubmissionId,
            @WorkerId,
            CAST(j.LeaveDate      AS DATE),
            j.LeaveTypeCode,
            j.LeaveCategory,
            j.UnitOfMeasure,
            CAST(j.Quantity       AS DECIMAL(5,2))
        FROM OPENJSON(@LeaveDaysJson) WITH (
            LeaveDate       NVARCHAR(20)  '$.leave_date',
            LeaveTypeCode   NVARCHAR(10)  '$.leave_type_code',
            LeaveCategory   NVARCHAR(20)  '$.leave_category',
            UnitOfMeasure   NVARCHAR(10)  '$.unit_of_measure',
            Quantity        NVARCHAR(10)  '$.quantity'
        ) AS j;

        -- Insert DQ issues if any (CheckedAt set by DEFAULT)
        IF @DQIssuesJson <> '[]'
        BEGIN
            INSERT INTO dbo.DQResult
                (SubmissionId, Domain, Severity, Code, Field, Message)
            SELECT
                @SubmissionId,
                j.Domain,
                j.Severity,
                j.Code,
                j.Field,
                j.Message
            FROM OPENJSON(@DQIssuesJson) WITH (
                Domain    NVARCHAR(20)   '$.domain',
                Severity  NVARCHAR(10)   '$.severity',
                Code      NVARCHAR(10)   '$.code',
                Field     NVARCHAR(100)  '$.field',
                Message   NVARCHAR(MAX)  '$.message'
            ) AS j;
        END

    COMMIT TRANSACTION;
END;
GO