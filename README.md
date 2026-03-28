# Leave Submission API — SQLite PoC

Drop-in PoC that replaces the SQL Server (`pyodbc`) backend with SQLite.  
Zero infrastructure required — runs entirely on the local filesystem.

## File structure

```
poc/
├── .env                  # Environment config
├── db_setup.py           # Creates data/leave.db (run once)
├── main.py               # FastAPI app  ←  mirrors BDAXAI lifespan / logger pattern
├── database_sqlite.py    # SQLite persistence layer
├── business_logic.py     # Working-day decomposition (shared with production)
├── models.py             # Pydantic v2 schemas (shared with production)
├── requirements.txt
├── data/
│   └── leave.db          # Created by db_setup.py
├── logs/
│   └── main-YYYYMMDD.log
└── tests/
    └── test.py           # In-memory SQLite, no file I/O required
```

## Quick start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Create DB
python db_setup.py

# 3. Start API  (port from .env: POWERHOUSE_POC_SERVER_PORT=8090)
uvicorn main:app --port 8090 --reload

# 4. Run tests  (no DB file needed — uses in-memory SQLite)
pytest tests/ -v
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/leave-submissions` | Submit a leave request |
| `GET`  | `/api/v1/leave-submissions/{id}` | Retrieve submission + day records |
| `GET`  | `/health` | Liveness check |

## BDAXAI patterns applied

| Pattern | Where used |
|---|---|
| `load_dotenv()` + `os.environ` config | `main.py` §2 |
| `setup_logger` / `writelog` | `main.py` §3 |
| `@asynccontextmanager lifespan` | `main.py` §4 |
| Feature flag env var (`POWERHOUSE_LEAVE_API_ENABLED`) | `main.py` §4 |
| `app.state.*` for service readiness | `main.py` §4–5 |
| Structured `[MODULE.function]` log prefixes | Throughout |

## Swapping back to SQL Server

Replace the import in `main.py`:

```python
# SQLite PoC
from database_sqlite import fetch_submission, persist_submission, submission_exists

# SQL Server production
from database import persist_submission, submission_exists
```

Both modules expose the same three function signatures.