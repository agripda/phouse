-- ============================================================
-- Leave Submission System — SQL Server DDL
-- ============================================================

USE LeaveDB;
GO

-- ------------------------------------------------------------
-- Table: LeaveSubmission
-- Stores the raw submission header exactly as received.
-- SubmissionId is caller-supplied (e.g. "LS-2026-000123").
-- ------------------------------------------------------------
CREATE TABLE dbo.LeaveSubmission (
    SubmissionId    VARCHAR(50)  NOT NULL,
    WorkerId        VARCHAR(50)  NOT NULL,
    StartDatetime   DATETIME     NOT NULL,
    EndDatetime     DATETIME     NOT NULL,
    TotalDays       INT          NOT NULL,
    Status          VARCHAR(20)  NOT NULL,
    SubmittedDate   DATE         NOT NULL,

    CONSTRAINT PK_LeaveSubmission PRIMARY KEY (SubmissionId)
);
GO

-- ------------------------------------------------------------
-- Table: LeaveDay
-- One row per working day (Mon–Fri) within the leave period.
-- LeaveDayId is a surrogate key; SubmissionId FK links back
-- to the parent submission for full traceability.
-- ------------------------------------------------------------
CREATE TABLE dbo.LeaveDay (
    LeaveDayId      INT           NOT NULL IDENTITY(1,1),
    SubmissionId    VARCHAR(50)   NOT NULL,
    WorkerId        VARCHAR(50)   NOT NULL,
    LeaveDate       DATE          NOT NULL,
    LeaveTypeCode   VARCHAR(10)   NOT NULL,
    LeaveCategory   VARCHAR(20)   NOT NULL,
    UnitOfMeasure   VARCHAR(10)   NOT NULL,
    Quantity        DECIMAL(5,2)  NOT NULL,

    CONSTRAINT PK_LeaveDay
        PRIMARY KEY (LeaveDayId),

    CONSTRAINT FK_LeaveDay_LeaveSubmission
        FOREIGN KEY (SubmissionId)
        REFERENCES dbo.LeaveSubmission (SubmissionId),

    -- Prevent duplicate entries for same worker/day/leave-type
    CONSTRAINT UQ_LeaveDay_Worker_Date_Type
        UNIQUE (WorkerId, LeaveDate, LeaveTypeCode)
);
GO

-- Useful query index: fetch all days for a submission
CREATE INDEX IX_LeaveDay_SubmissionId
    ON dbo.LeaveDay (SubmissionId);
GO

-- Useful query index: fetch calendar view per worker
CREATE INDEX IX_LeaveDay_WorkerId_Date
    ON dbo.LeaveDay (WorkerId, LeaveDate);
GO
