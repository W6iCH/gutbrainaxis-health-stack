#!/usr/bin/env python3
"""
Survey Database — SQLite-backed persistent storage for survey submissions.
Stores each submission keyed by student ID (学号), supporting multiple
submissions per individual over time.

Schema v2: Each question stored as separate variable (responses JSON),
           scale scores as individual columns.
"""

import json
import os
import sqlite3
from datetime import datetime

DB_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DB_DIR, "survey_data.db")


def get_conn():
    """Get a connection to the SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Initialize database tables with v2 schema."""
    conn = get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS submissions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id      TEXT NOT NULL,
                submission_id   TEXT UNIQUE,
                name            TEXT,
                email           TEXT,

                -- Per-question responses (JSON: {"question": "answer", ...})
                responses       TEXT,

                -- Per-item scores (JSON: {"item": numeric_score, ...})
                item_scores     TEXT,

                -- Raw API response + analysis (backward compat)
                raw_data        TEXT,
                analysis        TEXT,

                -- Redirect tracking
                redirect_user   TEXT,
                redirect_quest  TEXT,
                redirect_answer TEXT,

                -- DEBQ scores
                debq_emotional_score     REAL,
                debq_external_score      REAL,
                debq_restrained_score    REAL,
                debq_emotional_mean      REAL,
                debq_external_mean       REAL,
                debq_restrained_mean     REAL,
                debq_emotional_level     TEXT,
                debq_external_level      TEXT,
                debq_restrained_level    TEXT,
                debq_total               REAL,
                debq_interpretation      TEXT,

                -- GAD-7
                gad7_score               REAL,
                gad7_n_items             INTEGER,
                gad7_interpretation      TEXT,

                -- PHQ-9
                phq9_score               REAL,
                phq9_n_items             INTEGER,
                phq9_interpretation      TEXT,

                -- PSQI
                psqi_score               REAL,
                psqi_n_items             INTEGER,
                psqi_interpretation      TEXT,

                -- PSS-14
                pss14_score              REAL,
                pss14_n_items            INTEGER,
                pss14_interpretation     TEXT,

                -- GSRS
                gsrs_score               REAL,
                gsrs_n_items             INTEGER,
                gsrs_interpretation      TEXT,

                -- IPAQ-S
                ipaq_met_min_week        REAL,
                ipaq_sedentary_min       REAL,
                ipaq_interpretation      TEXT,

                -- VSI
                vsi_score                REAL,
                vsi_n_items              INTEGER,
                vsi_interpretation       TEXT,

                -- WHOQOL-BREF
                whoqol_score             REAL,
                whoqol_n_items           INTEGER,
                whoqol_interpretation    TEXT,

                -- BMI
                bmi_score              REAL,
                bmi_category           TEXT,
                bmi_interpretation     TEXT,

                -- Timestamps
                submitted_at    TEXT,
                created_at      TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS email_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id   TEXT,
                student_id      TEXT,
                recipient       TEXT NOT NULL,
                status          TEXT DEFAULT 'pending',
                attempts        INTEGER DEFAULT 0,
                last_error      TEXT,
                fallback_sent   INTEGER DEFAULT 0,
                fallback_to     TEXT,
                created_at      TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at      TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (submission_id) REFERENCES submissions(submission_id)
            );

            CREATE INDEX IF NOT EXISTS idx_submissions_student
                ON submissions(student_id);
            CREATE INDEX IF NOT EXISTS idx_submissions_submission
                ON submissions(submission_id);
            CREATE INDEX IF NOT EXISTS idx_email_log_status
                ON email_log(status);
            CREATE INDEX IF NOT EXISTS idx_email_log_submission
                ON email_log(submission_id);
        """)
        # Ensure BMI columns exist (for databases created before v2.3 migration)
        for bmi_col in ["bmi_score", "bmi_category", "bmi_interpretation"]:
            try:
                conn.execute(f"ALTER TABLE submissions ADD COLUMN {bmi_col} REAL")
            except:
                pass  # Column already exists
        conn.commit()
    finally:
        conn.close()


# ── Helpers for extracting scale scores ───────────────────────────────────

def _extract_scale_scores(analysis: dict) -> dict:
    """Extract flat scale score dict from analysis report."""
    if not analysis:
        return {}

    scores = analysis.get("scores", {})
    result = {}

    # DEBQ
    debq = scores.get("DEBQ")
    if debq:
        result["debq_total"] = debq.get("total")
        subs = debq.get("subscales", {})
        if subs:
            for sub_key, sub_val in subs.items():
                if sub_key == "情绪性饮食":
                    result["debq_emotional_score"] = sub_val.get("raw")
                    result["debq_emotional_mean"] = sub_val.get("mean")
                    result["debq_emotional_level"] = sub_val.get("level")
                elif sub_key == "外部性饮食":
                    result["debq_external_score"] = sub_val.get("raw")
                    result["debq_external_mean"] = sub_val.get("mean")
                    result["debq_external_level"] = sub_val.get("level")
                elif sub_key == "限制性饮食":
                    result["debq_restrained_score"] = sub_val.get("raw")
                    result["debq_restrained_mean"] = sub_val.get("mean")
                    result["debq_restrained_level"] = sub_val.get("level")
        result["debq_interpretation"] = debq.get("interpretation")

    # GAD-7
    gad7 = scores.get("GAD-7")
    if gad7:
        result["gad7_score"] = gad7.get("total")
        result["gad7_n_items"] = gad7.get("n_items")
        result["gad7_interpretation"] = gad7.get("interpretation")

    # PHQ-9
    phq9 = scores.get("PHQ-9")
    if phq9:
        result["phq9_score"] = phq9.get("total")
        result["phq9_n_items"] = phq9.get("n_items")
        result["phq9_interpretation"] = phq9.get("interpretation")

    # PSQI
    psqi = scores.get("PSQI")
    if psqi:
        result["psqi_score"] = psqi.get("total")
        result["psqi_n_items"] = psqi.get("n_items")
        result["psqi_interpretation"] = psqi.get("interpretation")

    # PSS-14
    pss = scores.get("PSS-14")
    if pss:
        result["pss14_score"] = pss.get("total")
        result["pss14_n_items"] = pss.get("n_items")
        result["pss14_interpretation"] = pss.get("interpretation")

    # GSRS
    gsrs = scores.get("GSRS")
    if gsrs:
        result["gsrs_score"] = gsrs.get("total")
        result["gsrs_n_items"] = gsrs.get("n_items")
        result["gsrs_interpretation"] = gsrs.get("interpretation")

    # IPAQ-S
    ipaq = scores.get("IPAQ-S")
    if ipaq:
        result["ipaq_met_min_week"] = ipaq.get("total")
        result["ipaq_interpretation"] = ipaq.get("interpretation")
        subs = ipaq.get("subscales", {})
        result["ipaq_sedentary_min"] = subs.get("静坐时间")

    # VSI
    vsi = scores.get("VSI")
    if vsi:
        result["vsi_score"] = vsi.get("total")
        result["vsi_n_items"] = vsi.get("n_items")
        result["vsi_interpretation"] = vsi.get("interpretation")

    # WHOQOL-BREF
    whoqol = scores.get("WHOQOL-BREF")
    if whoqol:
        result["whoqol_score"] = whoqol.get("total")
        result["whoqol_n_items"] = whoqol.get("n_items")
        result["whoqol_interpretation"] = whoqol.get("interpretation")

    # BMI
    bmi = scores.get("BMI")
    if bmi:
        result["bmi_score"] = bmi.get("total")
        result["bmi_category"] = bmi.get("category")
        result["bmi_interpretation"] = bmi.get("interpretation")

    return result


def _extract_responses(analysis: dict, raw_data: dict = None) -> str:
    """Extract per-question responses as JSON string.

    Prefers analysis scales data for clean Q&A pairs.
    Falls back to raw_data answers.
    """
    responses = {}

    # Try from analysis scales first (cleanest)
    for scale_name, items in analysis.get("scales", {}).items():
        for item in items:
            q = item.get("question", "")
            a = item.get("answer_text", "")
            if q:
                responses[q] = a

    # Also get basic info
    for k, v in analysis.get("basic_info", {}).items():
        if k and v:
            responses[k] = str(v)

    # Fallback: extract from raw_data if not already populated
    if raw_data and not responses:
        answers = raw_data.get("answers", [])
        for item in answers:
            q = item.get("question", {})
            title = q.get("title", "")
            ans = item.get("answer", "")
            qtype = q.get("question_type", "")

            if isinstance(ans, dict):
                ans_text = ans.get("label", str(ans))
            else:
                ans_text = str(ans)

            if qtype == "矩阵单选题":
                ans_name = q.get("answer_name", [])
                if isinstance(ans, list):
                    for i, sub_ans in enumerate(ans):
                        sub_text = ""
                        if isinstance(sub_ans, dict):
                            sub_text = sub_ans.get("title", "")
                        else:
                            sub_text = str(sub_ans)
                        q_text = ""
                        if i < len(ans_name):
                            if isinstance(ans_name[i], dict):
                                q_text = ans_name[i].get("key", "")
                            else:
                                q_text = str(ans_name[i])
                        if q_text:
                            responses[q_text] = sub_text
                else:
                    responses[title] = ans_text
            else:
                responses[title] = ans_text

    return json.dumps(responses, ensure_ascii=False)


# ── Submission Operations ────────────────────────────────────────────────

def save_submission(student_id: str, submission_id: str = None,
                    name: str = None, email: str = None,
                    raw_data: dict = None, analysis: dict = None,
                    redirect_params: dict = None) -> int:
    """
    Save or update a submission record with per-question responses
    and scale score columns.

    Returns the submission DB id.
    """
    conn = get_conn()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # If submission_id exists, check for duplicate
        if submission_id:
            existing = conn.execute(
                "SELECT id FROM submissions WHERE submission_id = ?",
                (submission_id,)
            ).fetchone()
            if existing:
                return existing["id"]

        redirect_user = None
        redirect_quest = None
        redirect_answer = None
        if redirect_params:
            redirect_user = redirect_params.get("user")
            redirect_quest = redirect_params.get("quest")
            redirect_answer = redirect_params.get("answer")

        # Extract scale scores and responses from analysis
        scale_scores = _extract_scale_scores(analysis)
        responses_json = _extract_responses(analysis, raw_data) if analysis else None

        # Build column list dynamically based on what we have
        base_cols = [
            "student_id", "submission_id", "name", "email",
            "raw_data", "analysis",
            "redirect_user", "redirect_quest", "redirect_answer",
            "submitted_at",
        ]
        base_vals = [
            student_id, submission_id, name, email,
            json.dumps(raw_data, ensure_ascii=False) if raw_data else None,
            json.dumps(analysis, ensure_ascii=False) if analysis else None,
            redirect_user, redirect_quest, redirect_answer,
            now,
        ]

        # Add scale score columns if we have them
        extra_cols = []
        extra_vals = []
        if responses_json:
            extra_cols.append("responses")
            extra_vals.append(responses_json)
        for col, val in scale_scores.items():
            if val is not None:
                extra_cols.append(col)
                extra_vals.append(val)

        all_cols = ", ".join(base_cols + extra_cols)
        placeholders = ", ".join(["?"] * len(base_vals + extra_vals))

        cursor = conn.execute(
            f"INSERT INTO submissions ({all_cols}) VALUES ({placeholders})",
            base_vals + extra_vals
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_submissions_by_student(student_id: str) -> list:
    """Get all submissions for a student, ordered by time."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT * FROM submissions
            WHERE student_id = ?
            ORDER BY created_at DESC
        """, (student_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_latest_submission() -> dict:
    """Get the most recent submission."""
    conn = get_conn()
    try:
        row = conn.execute("""
            SELECT * FROM submissions
            ORDER BY created_at DESC LIMIT 1
        """).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_latest_submission_by_answer() -> dict:
    """Get the most recent submission by answer ID (higher = newer)."""
    conn = get_conn()
    try:
        row = conn.execute("""
            SELECT * FROM submissions
            WHERE redirect_answer IS NOT NULL
              AND redirect_answer != ''
            ORDER BY CAST(redirect_answer AS INTEGER) DESC
            LIMIT 1
        """).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_submission_by_id(submission_id: str) -> dict:
    """Get a submission by its WJX submission ID."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM submissions WHERE submission_id = ?",
            (submission_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_students() -> list:
    """Get list of unique student IDs."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT student_id, name, COUNT(*) as count "
            "FROM submissions GROUP BY student_id ORDER BY student_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_submission_analysis(db_id: int, analysis: dict):
    """
    Update the analysis for a submission, including re-extracted
    scale scores and responses.
    """
    conn = get_conn()
    try:
        scale_scores = _extract_scale_scores(analysis)
        responses_json = _extract_responses(analysis)

        set_clauses = ["analysis = ?"]
        params = [json.dumps(analysis, ensure_ascii=False)]

        if responses_json:
            set_clauses.append("responses = ?")
            params.append(responses_json)

        for col, val in scale_scores.items():
            if val is not None:
                set_clauses.append(f"{col} = ?")
                params.append(val)
            else:
                set_clauses.append(f"{col} = NULL")

        params.append(db_id)
        conn.execute(
            f"UPDATE submissions SET {', '.join(set_clauses)} WHERE id = ?",
            params
        )
        conn.commit()
    finally:
        conn.close()


# ── Email Log Operations ─────────────────────────────────────────────────

def log_email(submission_id: str, student_id: str, recipient: str) -> int:
    """Create an email log entry.

    ⚠️ 返回的是 **cursor.lastrowid**（`sqlite3.Connection` 并无 `lastrowid` 属性）：
    旧实现 `return conn.lastrowid` 会抛 AttributeError，导致邮件队列写入失败后
    被上层静默吞掉 —— 邮件永远不会发出。本版修正为使用 cursor。
    """
    conn = get_conn()
    try:
        cur = conn.execute("""
            INSERT INTO email_log
                (submission_id, student_id, recipient, status)
            VALUES (?, ?, ?, 'pending')
        """, (submission_id, student_id, recipient))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_email_log(log_id: int, status: str = None,
                     attempts: int = None, last_error: str = None,
                     fallback_sent: int = None):
    """Update email log entry."""
    conn = get_conn()
    try:
        updates = ["updated_at = datetime('now', 'localtime')"]
        params = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if attempts is not None:
            updates.append("attempts = ?")
            params.append(attempts)
        if last_error is not None:
            updates.append("last_error = ?")
            params.append(last_error)
        if fallback_sent is not None:
            updates.append("fallback_sent = ?")
            params.append(fallback_sent)
            if fallback_sent:
                updates.append("fallback_to = ?")
                params.append("operator@example.edu")

        params.append(log_id)
        conn.execute(
            f"UPDATE email_log SET {', '.join(updates)} WHERE id = ?",
            params
        )
        conn.commit()
    finally:
        conn.close()


def get_pending_emails(limit: int = 10) -> list:
    """Get pending email tasks."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT * FROM email_log
            WHERE status IN ('pending', 'retrying')
            ORDER BY created_at ASC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_failed_emails() -> list:
    """Get emails that exhausted retries."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT * FROM email_log
            WHERE status = 'failed' AND fallback_sent = 0
            ORDER BY created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Analysis Storage Helpers ──────────────────────────────────────────────

def extract_student_id(analysis: dict) -> str:
    """Extract student ID from analysis report."""
    basic = analysis.get("basic_info", {})
    sid = basic.get("学工号", basic.get("student_id", ""))
    if not sid:
        sid = "unknown_" + datetime.now().strftime("%Y%m%d%H%M%S")
    return sid


def store_submission(raw_data: dict, analysis: dict, redirect_params: dict = None):
    """
    High-level function: extract student info from analysis,
    save to database with individual responses and scale scores.
    """
    basic = analysis.get("basic_info", {})

    # Try both JSON structures for submission_id
    rows = []
    if isinstance(raw_data, dict):
        if "data" in raw_data:
            rows = raw_data["data"].get("rows", [])
        else:
            rows = raw_data.get("rows", [])

    submission_id = None
    for row in rows:
        for ans in row.get("answers", []):
            q = ans.get("question", {})
            if "流水号" in q.get("title", ""):
                submission_id = str(ans.get("answer", ""))
                break
        if submission_id:
            break

    student_id = basic.get("学工号", "unknown")
    name = basic.get("姓名", "")
    email = basic.get("邮箱", "")

    db_id = save_submission(
        student_id=student_id,
        submission_id=submission_id or redirect_params.get("answer") if redirect_params else None,
        name=name,
        email=email,
        raw_data=raw_data,
        analysis=analysis,
        redirect_params=redirect_params,
    )

    return db_id, student_id, email


# ── Helper: query individual responses ────────────────────────────────────

def get_response_value(submission_id: int, question: str) -> str:
    """Get a single response value by question title (via JSON extract)."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT json_extract(responses, ?) as val FROM submissions WHERE id = ?",
            (f'$."{question}"', submission_id)
        ).fetchone()
        return row["val"] if row else None
    finally:
        conn.close()


# ── Init on import ────────────────────────────────────────────────────────

init_db()
