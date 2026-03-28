# -*- coding: utf-8 -*-
# set PYTHONUTF8=1

"""
main.py — Leave Submission API  ·  SQLite PoC

Mirrors BDAXAI architectural patterns:
  - load_dotenv() + os.environ config
  - setup_logger / writelog
  - @asynccontextmanager lifespan (startup / shutdown)
  - Feature-flag env vars  (POWERHOUSE_LEAVE_API_ENABLED)
  - FastAPI app with title / version

Run:
    python db_setup.py       # create DB once
    uvicorn main:app --port 8090 --reload
"""

# ==============================================================================
# 1. IMPORTS
# ==============================================================================

# --- Standard Library ---
import os
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

# --- Third-Party ---
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

# --- Local ---
from business_logic import (
    decompose_to_leave_days,
    validate_working_day_alignment,
)


if os.environ.get("POWERHOUSE_DB_ENGINE") == "sqlserver":
    from database import (get_next_sequence, submission_exists,
                          persist_submission, persist_dq_results,
                          get_existing_leave_dates, fetch_submission)
else:
    from database_sqlite import (
        fetch_submission,
        get_existing_leave_dates,
        get_next_sequence,
        persist_dq_results,
        persist_submission,
        submission_exists,
    )
from dq_engine import run_dq_checks


def _generate_id_from_db() -> str:
    """Generate next LS-YYYY-NNNNNN from DB MAX sequence (server fallback)."""
    year = datetime.now().strftime("%Y")
    seq  = get_next_sequence()
    return f"LS-{year}-{seq:06d}"
from db_setup import create_database
from models import ErrorResponse, GetSubmissionResponse, LeaveSubmissionResponse, SubmitLeaveRequest

# ==============================================================================
# 2. CONFIGURATION & CONSTANTS
# ==============================================================================

load_dotenv()

_BASE_DIR = Path(__file__).parent
DB_PATH           = str((_BASE_DIR / os.environ.get("POWERHOUSE_DB_PATH", "data/leave.db")).resolve()) \
                    if not Path(os.environ.get("POWERHOUSE_DB_PATH", "data/leave.db")).is_absolute() \
                    else os.environ.get("POWERHOUSE_DB_PATH", "data/leave.db")
_raw_log          = os.environ.get("POWERHOUSE_LOG_DATAPATH", "logs")
LOG_DATAPATH      = str((_BASE_DIR / _raw_log).resolve()) \
                    if not Path(_raw_log).is_absolute() \
                    else _raw_log
POC_SERVER_PORT   = int(os.environ.get("POWERHOUSE_POC_SERVER_PORT", 8090))
LEAVE_API_ENABLED = os.environ.get("POWERHOUSE_LEAVE_API_ENABLED", "true").lower() == "true"

# Override env so database_sqlite._get_db_path() picks up the resolved absolute path
os.environ["POWERHOUSE_DB_PATH"] = DB_PATH

# ==============================================================================
# 3. LOGGING SETUP  (mirrors BDAXAI setup_logger / writelog pattern)
# ==============================================================================

import logging

def _setup_logger(name: str, log_dir: str, log_filename: str) -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(Path(log_dir) / log_filename, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


def writelog(logger: logging.Logger, message: str, level: str = "info") -> bool:
    getattr(logger, level.lower(), logger.info)(message)
    return True  # truthy return prevents `or print(msg)` fallback firing


module_name  = Path(__file__).stem
timestamp    = datetime.now().strftime("%Y%m%d")
log_filename = f"{module_name}-{timestamp}.log"
logger       = _setup_logger(
    name=f"logger_{module_name}",
    log_dir=LOG_DATAPATH,
    log_filename=log_filename,
)

# ==============================================================================
# 4. API LIFESPAN MANAGEMENT  (mirrors BDAXAI @asynccontextmanager lifespan)
# ==============================================================================

def _initialize_leave_api(app: FastAPI) -> None:
    """Ensure the SQLite DB file and schema exist before first request."""
    msg = "[LEAVE_POC._initialize_leave_api] --- START ---"
    logger and writelog(logger, msg, "info") or print(msg)
    try:
        create_database()
        app.state.leave_api_ready = True
        msg = f"[LEAVE_POC._initialize_leave_api] ✅ SQLite DB ready at: {DB_PATH}"
        logger and writelog(logger, msg, "info") or print(msg)
    except Exception as e:
        msg = f"[LEAVE_POC._initialize_leave_api] ❌ FAILED: {e}"
        logger and writelog(logger, msg, "error") or print(msg)
        app.state.leave_api_ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    msg = "\n" + "="*80 + "\n[LEAVE_POC.lifespan] --- API STARTUP ---"
    logger and writelog(logger, msg, "info") or print(msg)
    app.state.leave_api_ready = False

    try:
        if LEAVE_API_ENABLED:
            _initialize_leave_api(app)
        else:
            msg = "[LEAVE_POC.lifespan] Leave API disabled via POWERHOUSE_LEAVE_API_ENABLED=false"
            logger and writelog(logger, msg, "info") or print(msg)

        msg = "[LEAVE_POC.lifespan] --- API STARTUP COMPLETE ---\n" + "="*80
        logger and writelog(logger, msg, "info") or print(msg)

    except Exception as e:
        msg = f"[LEAVE_POC.lifespan] --- FATAL ERROR during startup: {e} ---"
        logger and writelog(logger, msg, "critical") or print(msg)
        traceback.print_exc()

    yield  # ── application runs here ──────────────────────────────────────────

    msg = "[LEAVE_POC.lifespan] --- API SHUTDOWN ---"
    logger and writelog(logger, msg, "info") or print(msg)


# ==============================================================================
# 5. FASTAPI APP INITIALISATION
# ==============================================================================

app = FastAPI(
    title="Leave Submission API — SQLite PoC",
    description="PoC implementation using SQLite. Mirrors the SQL Server production interface.",
    version="0.1.0-poc",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
# Allows Swagger UI (and any browser client) to call the API without
# "Failed to fetch" / CORS preflight errors.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # tighten to specific origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==============================================================================
# 6. GLOBAL EXCEPTION HANDLERS
# ==============================================================================

@app.exception_handler(ValidationError)
async def pydantic_validation_handler(request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": "Validation error", "detail": exc.errors()},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    msg = f"[LEAVE_POC] Unhandled error: {exc}"
    logger and writelog(logger, msg, "error") or print(msg)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "Internal server error", "detail": str(exc)},
    )


# ==============================================================================
# 7. API ENDPOINTS
# ==============================================================================

# ── Sample payload (mirrors BDAXAI SAMPLE_RRSPT_WORKFLOW pattern) ─────────────
SAMPLE_LEAVE_SUBMISSION = {
    "leaveSubmission": {
        "submissionId": "LS-2026-000123",
        "submittedDate": "2026-02-15",
        "status": "Submitted",
        "worker": {
            "workerId": "W123456",
            "employeeNumber": "90030366",
            "sourceSystem": "HRIS"
        },
        "leavePeriod": {
            "startDate": "2026-03-02 00:00:00.00",
            "endDate": "2026-03-20 23:59:59.99",
            "totalWeeks": 3,
            "totalWorkingDays": 15
        },
        "leaveDetails": [
            {
                "leaveTypeCode": "AL",
                "leaveTypeDescription": "Annual Leave",
                "leaveCategory": "Paid",
                "unitOfMeasure": "Days",
                "quantity": 15
            }
        ],
        "approver": {
            "approverId": "M987654",
            "approvalStatus": "Pending"
        },
        "comments": "Planned annual leave for personal travel."
    }
}

# ── POST /api/v1/leave-submissions ────────────────────────────────────────────

@app.post(
    "/api/v1/leave-submissions",
    response_model=LeaveSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Submission created successfully"},
        400: {"model": ErrorResponse, "description": "Business rule violation"},
        409: {"model": ErrorResponse, "description": "Submission already exists"},
        422: {"model": ErrorResponse, "description": "Request validation failed"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
    summary="Submit a worker leave request",
    tags=["Leave"],
    description="Submit a worker leave request. `submissionId` is caller-supplied per spec (e.g. LS-2026-000123). If omitted, server auto-generates LS-YYYY-NNNNNN. Duplicate submissionId returns HTTP 409.",
)
async def submit_leave(
    body: SubmitLeaveRequest = Body(
        ...,
        example=SAMPLE_LEAVE_SUBMISSION,
        description="Leave submission payload. Use the sample schema as reference.",
    ),
) -> LeaveSubmissionResponse:
    submission = body.leaveSubmission
    msg = f"[LEAVE_POC.submit_leave] Received request workerId={submission.worker.workerId} submissionId={submission.submissionId}"
    logger and writelog(logger, msg, "info") or print(msg)

    # Step 0 — SubmissionId: use caller-supplied if present, else server-generate
    if submission.submissionId:
        # Spec path: caller supplied — check for duplicate
        if submission_exists(submission.submissionId):
            msg = f"[LEAVE_POC.submit_leave] ⚠️  Duplicate submissionId={submission.submissionId}"
            logger and writelog(logger, msg, "warning") or print(msg)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Submission '{submission.submissionId}' already exists.",
            )
        msg = f"[LEAVE_POC.submit_leave] Using caller-supplied submissionId={submission.submissionId}"
        logger and writelog(logger, msg, "info") or print(msg)
    else:
        # Fallback: server generates LS-YYYY-NNNNNN from DB MAX sequence
        submission.submissionId = _generate_id_from_db()
        msg = f"[LEAVE_POC.submit_leave] Server-generated submissionId={submission.submissionId}"
        logger and writelog(logger, msg, "info") or print(msg)

    # Step 1 — Working-day alignment
    try:
        validate_working_day_alignment(submission.leavePeriod, submission.leaveDetails)
    except ValueError as exc:
        msg = f"[LEAVE_POC.submit_leave] Alignment check failed: {exc}"
        logger and writelog(logger, msg, "warning") or print(msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    # Step 2 — DQ checks (5 domains)
    # Critical issues (e.g. UNQ-001 overlapping dates) → HTTP 400, nothing saved
    # Warning issues → submission proceeds, recorded in DQResult
    dq_result = run_dq_checks(
        submission,
        existing_dates_fn=get_existing_leave_dates,
    )
    if not dq_result.passed:
        critical = dq_result.critical_issues
        msg = f"[LEAVE_POC.submit_leave] ❌ DQ CRITICAL — rejected: {[i.code for i in critical]}"
        logger and writelog(logger, msg, "warning") or print(msg)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Submission rejected due to critical DQ issue.",
                "dq_issues": dq_result.to_dict_list(),
            },
        )
    if dq_result.warning_issues:
        msg = f"[LEAVE_POC.submit_leave] DQ WARNING — {len(dq_result.warning_issues)} issue(s): {[i.code for i in dq_result.warning_issues]}"
        logger and writelog(logger, msg, "info") or print(msg)

    # Step 2 — Decompose into working days
    leave_days = decompose_to_leave_days(submission.leavePeriod, submission.leaveDetails)
    msg = f"[LEAVE_POC.submit_leave] Decomposed into {len(leave_days)} working-day records"
    logger and writelog(logger, msg, "info") or print(msg)

    # Step 3 — Persist
    try:
        result = persist_submission(submission, leave_days)
        final_id = result if result and isinstance(result, str) else submission.submissionId
        msg = f"[LEAVE_POC.submit_leave] ✅ Persisted submissionId={final_id}"
        logger and writelog(logger, msg, "info") or print(msg)
        if dq_result.issues:
            persist_dq_results(final_id, dq_result.to_dict_list())
    except Exception as exc:
        msg = f"[LEAVE_POC.submit_leave] ❌ DB error: {exc}"
        logger and writelog(logger, msg, "error") or print(msg)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"DB error: {exc}",
        ) from exc

    return LeaveSubmissionResponse(
        submissionId=final_id,
        workerId=submission.worker.workerId,
        status=submission.status,
        totalWorkingDaysCreated=len(leave_days),
        leaveDays=leave_days,
        dq_issues=dq_result.to_dict_list(),
    )


# ── GET /api/v1/leave-submissions/{submission_id} ─────────────────────────────

@app.get(
    "/api/v1/leave-submissions/{submission_id}",
    response_model=GetSubmissionResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Submission found"},
        404: {"model": ErrorResponse, "description": "Submission not found"},
    },
    summary="Retrieve a leave submission with its day records",
    tags=["Leave"],
)
async def get_leave_submission(submission_id: str) -> GetSubmissionResponse:
    msg = f"[LEAVE_POC.get_leave_submission] GET submissionId={submission_id}"
    logger and writelog(logger, msg, "info") or print(msg)
    result = fetch_submission(submission_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Submission '{submission_id}' not found.",
        )
    return result


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health", include_in_schema=False)
async def health() -> dict:
    return {
        "status": "ok",
        "leave_api_ready": getattr(app.state, "leave_api_ready", False),
        "db_path": DB_PATH,
    }


# ==============================================================================
# 8. ENTRYPOINT
# ==============================================================================

if __name__ == "__main__":
    import uvicorn
    msg = f"[LEAVE_POC] Starting server on port {POC_SERVER_PORT}"
    logger and writelog(logger, msg, "info") or print(msg)
    uvicorn.run("main:app", host="0.0.0.0", port=POC_SERVER_PORT, reload=True)