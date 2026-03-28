# Leave Submission API

REST API that accepts a worker leave submission (JSON) and persists the data
at day-by-day granularity. Assessment task implementation with SQLite PoC.

---

## File structure

```
poc/                          ← SQLite PoC (run locally, no infrastructure)
├── .env                      # Environment config
├── db_setup.py               # Creates data/leave.db (run once)
├── main.py                   # FastAPI app  ←  mirrors BDAXAI lifespan / logger pattern
├── database_sqlite.py        # SQLite persistence layer
├── database.py               # SQL Server persistence layer (same interface)
├── business_logic.py         # Working-day decomposition (shared)
├── models.py                 # Pydantic v2 schemas (shared)
├── dq_engine.py              # DQ engine — 5 domains, 16 rules ⭐ bonus
├── app.py                    # Streamlit UI — 4 pages ⭐ bonus
├── requirements.txt
├── schema.sql                # SQL Server DDL + usp_PersistLeaveSubmission
├── data/
│   └── leave.db              # Created by db_setup.py
├── logs/
│   └── main-YYYYMMDD.log
└── tests/
    └── test.py               # In-memory SQLite, no file I/O required
```

---

## Installation and CLI

### Installation

**Clone the repository:**

```bash
git clone https://github.com/agripda/phouse.git
cd phouse/poc
```

**Create and activate a virtual environment:**

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

**Install dependencies:**

```bash
pip install -r requirements.txt
```

---

## Quick start — SQLite PoC

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start API  (port from .env: POWERHOUSE_POC_SERVER_PORT=8090)
#    DB is created automatically on first startup
uvicorn main:app --port 8090 --reload

# 3. Start Streamlit UI  (optional)
streamlit run app.py

# 4. Run tests  (no DB file needed — uses in-memory SQLite)
pytest tests/ -v
```

---

## Quick start — SQL Server (production)

```bash
# 1. Run DDL + SP against target database
sqlcmd -S <server> -d LeaveDB -i schema.sql

# 2. Set connection env vars (see .env)
POWERHOUSE_DB_ENGINE=sqlserver
POWERHOUSE_MSSQL_SERVER=<server>
POWERHOUSE_MSSQL_DATABASE=LeaveDB
POWERHOUSE_MSSQL_UID=<user>
POWERHOUSE_MSSQL_PWD=<password>

# 3. Start API
uvicorn main:app --port 8090 --reload
```

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/leave-submissions` | Submit a leave request |
| `GET`  | `/api/v1/leave-submissions/{id}` | Retrieve submission + day records |
| `GET`  | `/health` | Liveness check |

---

## Swapping SQLite ↔ SQL Server

`database.py` and `database_sqlite.py` expose the **same 6 function signatures**:

| Function | Purpose |
|---|---|
| `get_next_sequence()` | Next SubmissionId sequence from DB MAX |
| `submission_exists(id)` | Duplicate check |
| `persist_submission(payload, days)` | Atomic header + day INSERT |
| `persist_dq_results(id, issues)` | Write DQ warnings to DQResult |
| `get_existing_leave_dates(worker_id)` | UNQ-001 overlap check |
| `fetch_submission(id)` | GET endpoint query |

Switch in `main.py` via env var — no other code changes required:

```python
if os.environ.get("POWERHOUSE_DB_ENGINE") == "sqlserver":
    from database import (get_next_sequence, submission_exists,
                          persist_submission, persist_dq_results,
                          get_existing_leave_dates, fetch_submission)
else:
    from database_sqlite import (get_next_sequence, submission_exists,
                                 persist_submission, persist_dq_results,
                                 get_existing_leave_dates, fetch_submission)
```

---

## BDAXAI patterns applied

| Pattern | Where used |
|---|---|
| `load_dotenv()` + `os.environ` config | `main.py` §2 |
| `setup_logger` / `writelog` | `main.py` §3 |
| `@asynccontextmanager lifespan` | `main.py` §4 |
| Feature flag env var (`POWERHOUSE_LEAVE_API_ENABLED`) | `main.py` §4 |
| `app.state.*` for service readiness | `main.py` §4–5 |
| `POWERHOUSE_*` env var prefix | Throughout |
| Structured `[MODULE.function]` log prefixes | Throughout |

---

## DQ Engine ⭐ Bonus

16 rules across 5 domains. UNQ-001 is the only Critical rule (hard reject).
All others are soft warnings — submission proceeds and issues are recorded.

| Domain | Rules | Critical |
|---|---|---|
| Accuracy | ACC-001 – ACC-006 | — |
| Completeness | CMP-001 – CMP-003 | — |
| Consistency | CON-001 – CON-003 | — |
| Timeliness | TML-001 – TML-003 | — |
| Uniqueness | UNQ-001 | ✅ HTTP 400 reject |