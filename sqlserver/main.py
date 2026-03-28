# -*- coding: utf-8 -*-
# set PYTHONUTF8=1

"""
main.py — Leave Submission API  ·  SQLite PoC

Mirrors BDAXAI architectural patterns:
  - load_dotenv() + os.environ config
  - setup_logger / writelog
  - @asynccontextmanager lifespan (startup / shutdown)
  - Feature-flag env vars  (LEAVE_API_ENABLED)
  - FastAPI app with title / version

Run:
    python db_setup.py          # create DB once
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
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

# --- Local ---
from business_logic import decompose_to_leave_days, validate_working_day_alignment
from database_sqlite import fetch_submission, persist_submission, submission_exists
from db_setup import create_database
from models import ErrorResponse, LeaveSubmissionResponse, SubmitLeaveRequest

# ==============================================================================
# 2. CONFIGURATION & CONSTANTS
# ==============================================================================

load_dotenv()

DB_PATH         = os.environ.get("POWERHOUSE_DB_PATH",         "data/leave_poc.db")
LOG_DATAPATH    = os.environ.get("POWERHOUSE_LOG_DATAPATH",    "logs")
POC_SERVER_PORT = int(os.environ.get("POWERHOUSE_POC_SERVER_PORT", 8090))
LEAVE_API_ENABLED = os.environ.get("POWERHOUSE_LEAVE_API_ENABLED", "true").lower() == "true"

# ==============================================================================
# 3. LOGGING SETUP  (mirrors BDAXAI setup_logger / writelog pattern)
# ==============================================================================

# Inline logger so the PoC has zero dependency on bdaxai internals
import logging

def _setup_logger(name: str, log_dir: str, log_filename: str) -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    # File handler
    fh = logging.FileHandler(Path(log_dir) / log_filename, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


def writelog(logger: logging.Logger, message: str, level: str = "info") -> None:
    getattr(logger, level.lower(), logger.info)(message)


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
    writelog(logger, "[LEAVE_POC._initialize_leave_api] --- START ---", "info")
    try:
        create_database()
        app.state.leave_api_ready = True
        writelog(logger, f"[LEAVE_POC._initialize_leave_api] ✅ SQLite DB ready at: {DB_PATH}", "info")
    except Exception as e:
        writelog(logger, f"[LEAVE_POC._initialize_leave_api] ❌ FAILED: {e}", "error")
        app.state.leave_api_ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    writelog(logger, "\n" + "="*80 + "\n[LEAVE_POC.lifespan] --- API STARTUP ---", "info")
    app.state.leave_api_ready = False

    try:
        if LEAVE_API_ENABLED:
            _initialize_leave_api(app)
        else:
            writelog(logger, "[LEAVE_POC.lifespan] Leave API disabled via LEAVE_API_ENABLED=false", "info")

        writelog(logger, "[LEAVE_POC.lifespan] --- API STARTUP COMPLETE ---\n" + "="*80, "info")

    except Exception as e:
        writelog(logger, f"[LEAVE_POC.lifespan] --- FATAL ERROR during startup: {e} ---", "critical")
        traceback.print_exc()

    yield  # ── application runs here ──────────────────────────────────────────

    writelog(logger, "[LEAVE_POC.lifespan] --- API SHUTDOWN ---", "info")


# ==============================================================================
# 5. FASTAPI APP INITIALISATION
# ==============================================================================

app = FastAPI(
    title="Leave Submission API — SQLite PoC",
    description="PoC implementation using SQLite. Mirrors the SQL Server production interface.",
    version="0.1.0-poc",
    lifespan=lifespan,
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
    writelog(logger, f"[LEAVE_POC] Unhandled error: {exc}", "error")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "Internal server error", "detail": str(exc)},
    )


# ==============================================================================
# 7. API ENDPOINTS
# ==============================================================================

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
)
async def submit_leave(body: SubmitLeaveRequest) -> LeaveSubmissionResponse:
    submission = body.leaveSubmission
    writelog(logger, f"[LEAVE_POC.submit_leave] Received submissionId={submission.submissionId}", "info")

    # Step 1 — Working-day alignment
    try:
        validate_working_day_alignment(submission.leavePeriod, submission.leaveDetails)
    except ValueError as exc:
        writelog(logger, f"[LEAVE_POC.submit_leave] Alignment check failed: {exc}", "warning")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    # Step 2 — Idempotency check
    if submission_exists(submission.submissionId):
        writelog(logger, f"[LEAVE_POC.submit_leave] Duplicate submissionId={submission.submissionId}", "warning")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Submission '{submission.submissionId}' already exists.",
        )

    # Step 3 — Decompose into working days
    leave_days = decompose_to_leave_days(submission.leavePeriod, submission.leaveDetails)
    writelog(logger, f"[LEAVE_POC.submit_leave] Decomposed into {len(leave_days)} working-day records", "info")

    # Step 4 — Persist
    try:
        persist_submission(submission, leave_days)
        writelog(logger, f"[LEAVE_POC.submit_leave] ✅ Persisted submissionId={submission.submissionId}", "info")
    except Exception as exc:
        writelog(logger, f"[LEAVE_POC.submit_leave] ❌ DB error: {exc}", "error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist submission. Please retry.",
        ) from exc

    return LeaveSubmissionResponse(
        submissionId=submission.submissionId,
        workerId=submission.worker.workerId,
        status=submission.status,
        totalWorkingDaysCreated=len(leave_days),
        leaveDays=leave_days,
    )


# ── GET /api/v1/leave-submissions/{submission_id} ─────────────────────────────

@app.get(
    "/api/v1/leave-submissions/{submission_id}",
    status_code=status.HTTP_200_OK,
    summary="Retrieve a leave submission with its day records",
    tags=["Leave"],
)
async def get_leave_submission(submission_id: str) -> dict:
    writelog(logger, f"[LEAVE_POC.get_leave_submission] GET submissionId={submission_id}", "info")
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
    writelog(logger, f"[LEAVE_POC] Starting server on port {POC_SERVER_PORT}", "info")
    uvicorn.run("main:app", host="0.0.0.0", port=POC_SERVER_PORT, reload=True)