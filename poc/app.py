# -*- coding: utf-8 -*-
# set PYTHONUTF8=1
"""
app.py — Streamlit UI for Leave Submission API (PoC)

Pages:
  1. Submit Leave      — POST /api/v1/leave-submissions
  2. Leave Balance     — summary view per worker from local SQLite
  3. Browse DB (Admin) — raw table viewer with filters

Run:
    streamlit run app.py
"""

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
import os

# ── st.dialog availability check (requires Streamlit ≥ 1.36) ─────────────────
_ST_VERSION = tuple(int(x) for x in st.__version__.split(".")[:2])
HAS_DIALOG  = _ST_VERSION >= (1, 36)

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()

BASE_DIR  = Path(__file__).parent
DB_PATH   = Path(os.environ.get("POWERHOUSE_DB_PATH", BASE_DIR / "data" / "leave.db"))
if not DB_PATH.is_absolute():
    DB_PATH = (BASE_DIR / DB_PATH).resolve()

API_PORT  = int(os.environ.get("POWERHOUSE_POC_SERVER_PORT", 8090))
API_BASE  = f"http://localhost:{API_PORT}/api/v1"

# ── Page setup ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Leave Submission",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Sidebar nav ───────────────────────────────────────────────────────────────
st.sidebar.title("📅 Leave System")
st.sidebar.markdown("---")
page = st.sidebar.radio(
    "Navigation",
    ["📝 Submit Leave", "📊 Leave Balance", "🗄️ Browse DB", "🔍 DQ Dashboard"],
    label_visibility="collapsed",
)
st.sidebar.markdown("---")
st.sidebar.caption(f"API: `{API_BASE}`")
st.sidebar.caption(f"DB:  `{DB_PATH.relative_to(BASE_DIR)}`")


# ── Helper ────────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def working_days(start: date, end: date) -> int:
    count, cur = 0, start
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def _dq_issue_rows(dq_issues: list) -> None:
    """Render DQ issues grouped by domain — shared by dialog and expander."""
    domain_groups: dict = {}
    for issue in dq_issues:
        domain_groups.setdefault(issue["domain"], []).append(issue)

    domain_colors = {
        "Accuracy":     ("#e6f1fb", "#185fa5"),
        "Completeness": ("#eaf3de", "#3b6d11"),
        "Consistency":  ("#faeeda", "#854f0b"),
        "Timeliness":   ("#eeedfe", "#534ab7"),
        "Uniqueness":   ("#faece7", "#993c1d"),
    }

    for domain, issues in domain_groups.items():
        bg, fg = domain_colors.get(domain, ("#f1efe8", "#5f5e5a"))
        st.markdown(
            f"<span style='background:{bg};color:{fg};"
            f"padding:2px 10px;border-radius:4px;"
            f"font-size:12px;font-weight:600'>{domain}</span>",
            unsafe_allow_html=True,
        )
        for issue in issues:
            st.markdown(
                f"<div style='border-left:3px solid {fg};"
                f"background:#fafafa;padding:8px 12px;"
                f"margin:4px 0 8px;border-radius:0 4px 4px 0'>"
                f"<span style='background:#faeeda;color:#854f0b;"
                f"padding:1px 6px;border-radius:3px;"
                f"font-size:11px;font-weight:700;margin-right:8px'>"
                f"{issue['code']}</span>"
                f"<span style='font-size:13px;color:#333'>{issue['message']}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )


def show_dq_result(submission_id: str, dq_issues: list, total_days: int) -> None:
    """
    Show submission result with DQ warnings.
    Uses st.dialog on Streamlit ≥ 1.36, falls back to st.toast + st.expander.
    """
    if not dq_issues:
        st.success(f"✅ Submission created — **{submission_id}**  ({total_days} days)")
        return

    if HAS_DIALOG:
        # ── Native modal dialog ───────────────────────────────────────────────
        @st.dialog("⚠️ Submission accepted with DQ warnings")
        def _dialog():
            st.markdown(
                f"**Submission ID:** `{submission_id}`  &nbsp;·&nbsp; "
                f"**{total_days}** working days recorded",
            )
            st.caption(
                "The submission was saved successfully. The following Data Quality "
                "warnings have been recorded for governance review."
            )
            st.divider()
            _dq_issue_rows(dq_issues)
            st.divider()
            if st.button("OK — close", use_container_width=True, type="primary"):
                st.rerun()

        _dialog()

    else:
        # ── Fallback: toast + warning banner + expander ───────────────────────
        st.toast(
            f"⚠️ {len(dq_issues)} DQ warning(s) on {submission_id}",
            icon="⚠️",
        )
        st.warning(
            f"⚠️ Submission created with **{len(dq_issues)} DQ warning(s)** "
            f"— **{submission_id}**"
        )
        with st.expander(
            f"⚠️ {len(dq_issues)} Data Quality warning(s) — click to review",
            expanded=True,
        ):
            st.caption(
                "Submission was accepted. These are soft warnings "
                "recorded for governance review."
            )
            _dq_issue_rows(dq_issues)




def next_submission_id() -> str:
    """
    Generate the next LS-YYYY-NNNNNN by reading MAX sequence from the DB.
    Falls back to LS-{year}-000001 if DB is empty or unavailable.
    """
    year = datetime.now().strftime("%Y")
    try:
        if not DB_PATH.exists():
            return f"LS-{year}-000001"
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT MAX(CAST(SUBSTR(SubmissionId, 9) AS INTEGER)) FROM LeaveSubmission"
        ).fetchone()
        conn.close()
        max_seq = row[0] if row and row[0] is not None else 0
        return f"LS-{year}-{max_seq + 1:06d}"
    except Exception:
        return f"LS-{year}-000001"


# ==============================================================================
# PAGE 1 — SUBMIT LEAVE
# ==============================================================================
if page == "📝 Submit Leave":
    st.title("📝 Submit Leave Request")

    # Auto-generate next SubmissionId outside the form (not cached across reruns)
    auto_id = next_submission_id()

    # ── Date pickers OUTSIDE form — triggers rerun on change ──────────────────
    st.subheader("Leave Period")
    dcol1, dcol2 = st.columns(2)
    with dcol1:
        start_date = st.date_input("Start Date *", value=date.today(), key="start_date")
    with dcol2:
        end_date = st.date_input("End Date *", value=date.today(), key="end_date")

    wd = working_days(start_date, end_date)
    if start_date > end_date:
        st.error("Start date must be ≤ end date.")
    elif wd == 0:
        st.warning("⚠️ No working days in selected range.")
    else:
        st.info(f"⏱ Working days in selected range: **{wd}**")

    st.markdown("---")

    with st.form("leave_form"):
        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Submission")
            st.text_input(
                "Submission ID (auto-generated)",
                value=auto_id,
                disabled=True,
                help="Server-generated from DB MAX sequence. Format: LS-YYYY-NNNNNN",
            )
            submitted_date  = st.date_input("Submitted Date *", value=date.today())
            status          = st.selectbox("Status *", ["Submitted", "Draft", "Pending"])
            comments        = st.text_area("Comments", placeholder="Reason for leave...")

        with col2:
            st.subheader("Worker")
            worker_id       = st.text_input("Worker ID *",       value="W123456")
            employee_no     = st.text_input("Employee Number",   value="90030366")
            source_system   = st.text_input("Source System",     value="HRIS")

        st.subheader("Leave Details")
        lcol1, lcol2, lcol3 = st.columns(3)
        with lcol1:
            leave_type_code = st.selectbox("Leave Type Code *", ["AL", "SL", "CL", "UL", "PL", "LWP"])
            leave_type_desc = {"AL": "Annual Leave", "SL": "Sick Leave", "CL": "Casual Leave",
                               "UL": "Unpaid Leave", "PL": "Parental Leave", "LWP": "Leave Without Pay"}
        with lcol2:
            leave_category  = st.selectbox("Leave Category *", ["Paid", "Unpaid"])
        with lcol3:
            uom      = st.selectbox("Unit of Measure *", ["Days", "Hours"])
            quantity = st.number_input(
                "Quantity *",
                min_value=0.01,
                value=float(max(wd, 1)),  # auto-filled from live wd
                step=0.5,
                help="Auto-filled from working days in selected range.",
            )

        st.subheader("Approver")
        acol1, acol2 = st.columns(2)
        with acol1:
            approver_id     = st.text_input("Approver ID", value="M987654")
        with acol2:
            approval_status = st.selectbox("Approval Status", ["Pending", "Approved", "Rejected"])

        submitted = st.form_submit_button("🚀 Submit Leave Request", use_container_width=True)

    if submitted:
        errors = []
        if not worker_id.strip():
            errors.append("Worker ID is required.")
        if start_date > end_date:
            errors.append("Start date must be ≤ end date.")
        if wd < 1:
            errors.append("Date range must include at least one working day.")

        if errors:
            for e in errors:
                st.error(e)
        else:
            total_weeks = (end_date - start_date).days // 7

            payload = {
                "leaveSubmission": {
                    "submissionId":  auto_id,
                    "submittedDate": submitted_date.isoformat(),
                    "status":        status,
                    "worker": {
                        "workerId":       worker_id.strip(),
                        "employeeNumber": employee_no.strip(),
                        "sourceSystem":   source_system.strip(),
                    },
                    "leavePeriod": {
                        "startDate":        f"{start_date} 00:00:00.00",
                        "endDate":          f"{end_date} 23:59:59.99",
                        "totalWeeks":       total_weeks,
                        "totalWorkingDays": wd,
                    },
                    "leaveDetails": [{
                        "leaveTypeCode":        leave_type_code,
                        "leaveTypeDescription": leave_type_desc.get(leave_type_code, ""),
                        "leaveCategory":        leave_category,
                        "unitOfMeasure":        uom,
                        "quantity":             quantity,
                    }],
                    "approver": {
                        "approverId":     approver_id.strip(),
                        "approvalStatus": approval_status,
                    },
                    "comments": comments.strip(),
                }
            }

            with st.spinner("Submitting..."):
                try:
                    resp = requests.post(f"{API_BASE}/leave-submissions", json=payload, timeout=10)
                    if resp.status_code == 201:
                        data = resp.json()
                        dq_issues = data.get("dq_issues", [])

                        # Show result — modal dialog or fallback based on Streamlit version
                        show_dq_result(
                            submission_id=data["submissionId"],
                            dq_issues=dq_issues,
                            total_days=data.get("totalWorkingDaysCreated", 0),
                        )

                        # Submission detail (always available, collapsed)
                        if not dq_issues:
                            with st.expander("📋 Submission details", expanded=False):
                                st.json(data)

                    elif resp.status_code == 409:
                        # Rare race condition — retry once with refreshed ID
                        retry_id = next_submission_id()
                        payload["leaveSubmission"]["submissionId"] = retry_id
                        resp2 = requests.post(f"{API_BASE}/leave-submissions", json=payload, timeout=10)
                        if resp2.status_code == 201:
                            data = resp2.json()
                            st.success(f"✅ Submission created (retry) — **{data['submissionId']}**")
                            with st.expander("📋 Submission details", expanded=False):
                                st.json(data)
                        else:
                            st.error(f"❌ Retry failed HTTP {resp2.status_code}: {resp2.text}")
                    elif resp.status_code == 400:
                        detail = resp.json().get("detail", resp.text)
                        st.error("❌ Validation failed")
                        st.markdown(
                            f"<div style='background:#fcebeb;border-left:4px solid #a32d2d;"
                            f"padding:10px 14px;border-radius:4px;font-size:13px;color:#a32d2d'>"
                            f"{detail}</div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.error(f"❌ HTTP {resp.status_code}: {resp.text}")
                except requests.ConnectionError:
                    st.error(f"❌ Cannot reach API at `{API_BASE}`. Is the server running?")


# ==============================================================================
# PAGE 2 — LEAVE BALANCE
# ==============================================================================
elif page == "📊 Leave Balance":
    st.title("📊 Leave Balance")
    st.markdown("Days taken per worker and leave type, direct from SQLite.")

    if not DB_PATH.exists():
        st.warning(f"Database not found at `{DB_PATH.relative_to(BASE_DIR)}`. Start the API first.")
        st.stop()

    conn = get_db()

    # Worker filter
    workers_raw = conn.execute(
        "SELECT DISTINCT WorkerId FROM LeaveSubmission ORDER BY WorkerId"
    ).fetchall()
    worker_ids = [r["WorkerId"] for r in workers_raw]

    if not worker_ids:
        st.info("No submissions found yet.")
        conn.close()
        st.stop()

    selected_worker = st.selectbox(
        "Filter by Worker (leave blank for all)",
        ["— All workers —"] + worker_ids,
    )

    where = "" if selected_worker == "— All workers —" else f"WHERE ld.WorkerId = '{selected_worker}'"

    # Balance summary
    summary = pd.read_sql_query(
        f"""
        SELECT
            ld.WorkerId,
            ld.LeaveTypeCode,
            ld.LeaveCategory,
            ld.UnitOfMeasure,
            COUNT(*)         AS DaysTaken,
            SUM(ld.Quantity) AS TotalQuantity,
            MIN(ld.LeaveDate) AS EarliestDate,
            MAX(ld.LeaveDate) AS LatestDate
        FROM LeaveDay ld
        {where}
        GROUP BY ld.WorkerId, ld.LeaveTypeCode, ld.LeaveCategory, ld.UnitOfMeasure
        ORDER BY ld.WorkerId, DaysTaken DESC
        """,
        conn,
    )

    if summary.empty:
        st.info("No leave days found for selected worker.")
    else:
        # KPI cards
        total_days   = int(summary["DaysTaken"].sum())
        total_workers = summary["WorkerId"].nunique()
        leave_types  = summary["LeaveTypeCode"].nunique()

        k1, k2, k3 = st.columns(3)
        k1.metric("Total Days Taken", total_days)
        k2.metric("Workers", total_workers)
        k3.metric("Leave Types Used", leave_types)

        st.dataframe(summary, use_container_width=True, hide_index=True)

        # Per-worker bar chart
        if selected_worker == "— All workers —":
            chart_data = summary.groupby("WorkerId")["DaysTaken"].sum().reset_index()
            st.bar_chart(chart_data.set_index("WorkerId"))
        else:
            st.bar_chart(summary.set_index("LeaveTypeCode")[["DaysTaken"]])

    conn.close()


# ==============================================================================
# PAGE 3 — BROWSE DB (ADMIN)
# ==============================================================================
elif page == "🗄️ Browse DB":
    st.title("🗄️ Browse DB — Admin")
    st.markdown("Direct read from SQLite. Filters applied in-memory.")

    if not DB_PATH.exists():
        st.warning(f"Database not found at `{DB_PATH.relative_to(BASE_DIR)}`. Start the API first.")
        st.stop()

    conn = get_db()
    tab1, tab2 = st.tabs(["LeaveSubmission", "LeaveDay"])

    with tab1:
        st.subheader("dbo.LeaveSubmission")

        sub_df = pd.read_sql_query(
            "SELECT * FROM LeaveSubmission ORDER BY SubmittedDate DESC",
            conn,
        )

        if sub_df.empty:
            st.info("No submissions yet.")
        else:
            # Filters
            fcol1, fcol2 = st.columns(2)
            with fcol1:
                f_worker = st.multiselect(
                    "Filter WorkerId",
                    options=sorted(sub_df["WorkerId"].unique()),
                )
            with fcol2:
                f_status = st.multiselect(
                    "Filter Status",
                    options=sorted(sub_df["Status"].unique()),
                )

            filtered = sub_df.copy()
            if f_worker:
                filtered = filtered[filtered["WorkerId"].isin(f_worker)]
            if f_status:
                filtered = filtered[filtered["Status"].isin(f_status)]

            st.caption(f"Showing {len(filtered)} of {len(sub_df)} rows")
            st.dataframe(filtered, use_container_width=True, hide_index=True)

            # Detail drill-down
            st.markdown("---")
            st.subheader("Drill-down — Leave Days for Submission")
            selected_id = st.selectbox(
                "Select SubmissionId",
                options=[""] + filtered["SubmissionId"].tolist(),
            )
            if selected_id:
                days_df = pd.read_sql_query(
                    "SELECT * FROM LeaveDay WHERE SubmissionId = ? ORDER BY LeaveDate",
                    conn,
                    params=(selected_id,),
                )
                st.caption(f"{len(days_df)} day rows for `{selected_id}`")
                st.dataframe(days_df, use_container_width=True, hide_index=True)

    with tab2:
        st.subheader("dbo.LeaveDay")

        day_df = pd.read_sql_query(
            "SELECT * FROM LeaveDay ORDER BY LeaveDate DESC",
            conn,
        )

        if day_df.empty:
            st.info("No leave days yet.")
        else:
            dcol1, dcol2, dcol3 = st.columns(3)
            with dcol1:
                fd_worker = st.multiselect(
                    "Filter WorkerId ",
                    options=sorted(day_df["WorkerId"].unique()),
                    key="day_worker",
                )
            with dcol2:
                fd_type = st.multiselect(
                    "Filter LeaveTypeCode",
                    options=sorted(day_df["LeaveTypeCode"].unique()),
                )
            with dcol3:
                date_range = st.date_input(
                    "Filter Date Range",
                    value=(
                        date.fromisoformat(day_df["LeaveDate"].min()),
                        date.fromisoformat(day_df["LeaveDate"].max()),
                    ),
                )

            filtered_days = day_df.copy()
            if fd_worker:
                filtered_days = filtered_days[filtered_days["WorkerId"].isin(fd_worker)]
            if fd_type:
                filtered_days = filtered_days[filtered_days["LeaveTypeCode"].isin(fd_type)]
            if isinstance(date_range, tuple) and len(date_range) == 2:
                filtered_days = filtered_days[
                    (filtered_days["LeaveDate"] >= date_range[0].isoformat()) &
                    (filtered_days["LeaveDate"] <= date_range[1].isoformat())
                ]

            st.caption(f"Showing {len(filtered_days)} of {len(day_df)} rows")
            st.dataframe(filtered_days, use_container_width=True, hide_index=True)

    conn.close()

    # DB stats footer
    st.markdown("---")
    st.subheader("DB Stats")
    conn2 = get_db()
    sc1, sc2, sc3 = st.columns(3)
    sc1.metric("LeaveSubmission rows", conn2.execute("SELECT COUNT(*) FROM LeaveSubmission").fetchone()[0])
    sc2.metric("LeaveDay rows",        conn2.execute("SELECT COUNT(*) FROM LeaveDay").fetchone()[0])
    sc3.metric("DB size",              f"{DB_PATH.stat().st_size // 1024} KB" if DB_PATH.exists() else "—")
    conn2.close()


# ==============================================================================
# PAGE 4 — DQ DASHBOARD
# ==============================================================================
elif page == "🔍 DQ Dashboard":
    st.title("🔍 DQ Dashboard")
    st.markdown("Data Quality checks across all submissions — 5 domains, soft warnings.")

    tab_report, tab_rules = st.tabs(["📋 Report", "📖 Rules"])

    # ── TAB 1: REPORT ─────────────────────────────────────────────────────────
    with tab_report:
        if not DB_PATH.exists():
            st.warning(f"Database not found at `{DB_PATH.relative_to(BASE_DIR)}`.")
            st.stop()

        conn = get_db()
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='DQResult'"
        ).fetchone()
        if not tbl:
            st.info("DQResult table not found. Submit a leave request to trigger DQ checks.")
            conn.close()
            st.stop()

        dq_df = pd.read_sql_query(
            "SELECT * FROM DQResult ORDER BY CheckedAt DESC", conn
        )
        conn.close()

        if dq_df.empty:
            st.success("✅ No DQ issues recorded. All submissions passed.")
        else:
            # KPI cards
            total    = len(dq_df)
            critical = len(dq_df[dq_df["Severity"] == "Critical"])
            warnings = len(dq_df[dq_df["Severity"] == "Warning"])
            affected = dq_df["SubmissionId"].nunique()

            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Total issues",         total)
            k2.metric("Critical",             critical, delta=None if critical == 0 else f"🔴 {critical}")
            k3.metric("Warnings",             warnings, delta=None if warnings == 0 else f"🟡 {warnings}")
            k4.metric("Affected submissions", affected)

            st.markdown("---")

            col1, col2 = st.columns(2)
            with col1:
                st.subheader("Issues by domain")
                by_domain = (
                    dq_df.groupby(["Domain", "Severity"])
                    .size().reset_index(name="Count")
                )
                st.dataframe(by_domain, use_container_width=True, hide_index=True)

            with col2:
                st.subheader("Issues by code")
                by_code = (
                    dq_df.groupby(["Code", "Domain", "Severity"])
                    .size().reset_index(name="Count")
                    .sort_values("Count", ascending=False)
                )
                st.dataframe(by_code, use_container_width=True, hide_index=True)

            st.markdown("---")
            st.subheader("Issue log")

            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                f_domain = st.multiselect("Domain",       sorted(dq_df["Domain"].unique()))
            with fc2:
                f_sev    = st.multiselect("Severity",     ["Critical", "Warning"])
            with fc3:
                f_sub    = st.multiselect("SubmissionId", sorted(dq_df["SubmissionId"].unique()))

            filtered = dq_df.copy()
            if f_domain: filtered = filtered[filtered["Domain"].isin(f_domain)]
            if f_sev:    filtered = filtered[filtered["Severity"].isin(f_sev)]
            if f_sub:    filtered = filtered[filtered["SubmissionId"].isin(f_sub)]

            def colour_severity(val):
                if val == "Critical": return "background-color:#fcebeb;color:#a32d2d"
                if val == "Warning":  return "background-color:#faeeda;color:#854f0b"
                return ""

            st.caption(f"Showing {len(filtered)} of {len(dq_df)} issues")
            cols = ["SubmissionId", "CheckedAt", "Domain", "Severity", "Code", "Field", "Message"]
            st.dataframe(
                filtered[cols].style.applymap(colour_severity, subset=["Severity"]),
                use_container_width=True, hide_index=True,
            )

    # ── TAB 2: RULES ─────────────────────────────────────────────────────────
    with tab_rules:
        st.subheader("Defined DQ rules")
        st.markdown(
            "All rules run on every POST submission. "
            "All issues are **soft warnings** — submission is never rejected."
        )

        RULES = [
            # domain, severity, code, field, description, logic
            ("Accuracy",     "Warning",  "ACC-001", "worker.workerId",
             "WorkerId format",
             "Must match `W######` (W + 6 digits). e.g. W123456"),
            ("Accuracy",     "Warning",  "ACC-002", "submissionId",
             "SubmissionId format",
             "Must match `LS-YYYY-NNNNNN`. e.g. LS-2026-000001"),
            ("Accuracy",     "Warning",  "ACC-003", "leaveDetails[*].leaveTypeCode",
             "Leave type code reference check",
             f"Must be one of: AL, SL, CL, UL, PL, LWP"),
            ("Accuracy",     "Warning",  "ACC-004", "leaveDetails[*].leaveCategory",
             "Leave category reference check",
             "Must be one of: Paid, Unpaid"),
            ("Accuracy",     "Warning",  "ACC-005", "leaveDetails[*].unitOfMeasure",
             "Unit of measure reference check",
             "Must be one of: Days, Hours"),
            ("Accuracy",     "Warning",  "ACC-006", "status",
             "Status reference check",
             "Must be one of: Submitted, Draft, Pending"),
            ("Completeness", "Warning",  "CMP-001", "approver.approverId",
             "Approver ID required for non-Draft",
             "For status ≠ Draft, approver.approverId must not be blank"),
            ("Completeness", "Warning",  "CMP-002", "comments",
             "Comments recommended for Pending",
             "For status = Pending, comments should be provided"),
            ("Completeness", "Warning",  "CMP-003", "worker.employeeNumber",
             "Employee number not empty",
             "worker.employeeNumber must not be blank"),
            ("Consistency",  "Warning",  "CON-001", "leavePeriod.totalWeeks",
             "Total weeks consistent with date range",
             "totalWeeks must be within ±1 of ceil((endDate − startDate + 1) / 7)"),
            ("Consistency",  "Warning",  "CON-002", "leaveDetails[*].quantity",
             "Quantity sum matches working days",
             "Sum of quantity (Days UOM) must equal totalWorkingDays"),
            ("Consistency",  "Warning",  "CON-003", "submittedDate",
             "Submitted date ≤ leave start date",
             "submittedDate should not be after leavePeriod.startDate"),
            ("Timeliness",   "Warning",  "TML-001", "leavePeriod.startDate",
             "Backdated submission check",
             "startDate > 30 days in the past triggers a warning"),
            ("Timeliness",   "Warning",  "TML-002", "leavePeriod.startDate",
             "Far-future submission check",
             "startDate > 365 days in the future triggers a warning"),
            ("Timeliness",   "Warning",  "TML-003", "submittedDate",
             "Submitted date not in future",
             "submittedDate must not be a future date"),
            ("Uniqueness",   "Warning",  "UNQ-001", "leavePeriod",
             "Overlapping leave dates for same worker",
             "Checks if any dates in the new submission overlap existing LeaveDay rows for the same workerId"),
        ]

        rules_df = pd.DataFrame(RULES, columns=[
            "Domain", "Severity", "Code", "Field", "Rule", "Logic"
        ])

        # Domain filter
        domain_filter = st.multiselect(
            "Filter by domain",
            options=["Accuracy", "Completeness", "Consistency", "Timeliness", "Uniqueness"],
            default=["Accuracy", "Completeness", "Consistency", "Timeliness", "Uniqueness"],
            key="rules_domain_filter",
        )
        shown = rules_df[rules_df["Domain"].isin(domain_filter)]
        st.caption(f"Showing {len(shown)} of {len(rules_df)} rules")

        def colour_domain(val):
            m = {
                "Accuracy":     "background-color:#e6f1fb;color:#185fa5",
                "Completeness": "background-color:#eaf3de;color:#3b6d11",
                "Consistency":  "background-color:#faeeda;color:#854f0b",
                "Timeliness":   "background-color:#eeedfe;color:#534ab7",
                "Uniqueness":   "background-color:#faece7;color:#993c1d",
            }
            return m.get(val, "")

        st.dataframe(
            shown.style.applymap(colour_domain, subset=["Domain"]),
            use_container_width=True, hide_index=True,
        )

        st.markdown("---")
        st.subheader("Reference values")
        ref1, ref2, ref3 = st.columns(3)
        with ref1:
            st.markdown("**Leave type codes**")
            st.table(pd.DataFrame({
                "Code": ["AL", "SL", "CL", "UL", "PL", "LWP"],
                "Description": ["Annual Leave", "Sick Leave", "Casual Leave",
                                 "Unpaid Leave", "Parental Leave", "Leave Without Pay"],
            }))
        with ref2:
            st.markdown("**Categories & UOM**")
            st.table(pd.DataFrame({
                "Field": ["leaveCategory", "leaveCategory", "unitOfMeasure", "unitOfMeasure"],
                "Value": ["Paid", "Unpaid", "Days", "Hours"],
            }))
        with ref3:
            st.markdown("**Thresholds**")
            st.table(pd.DataFrame({
                "Rule": ["Backdated warning", "Far-future warning"],
                "Threshold": ["> 30 days past", "> 365 days future"],
            }))