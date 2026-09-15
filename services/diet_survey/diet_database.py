#!/usr/bin/env python3
"""
Diet Survey Database v2 — SQLite-backed persistent storage for diet diary submissions.
Stores each submission keyed by student ID (学号), supporting multiple
submissions per individual over time.

v2 Changes:
  - Added dietary_advice (TEXT) — LLM-generated dietary advice
  - Added fulfillment_report (TEXT) — previous advice fulfillment report
  - Auto-migration for existing databases
"""

import json
import os
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "diet_data.db")


def get_conn():
    """Get a connection to the SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Initialize database tables with v2 schema (including dietary_advice columns)."""
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
                raw_data        TEXT,

                -- Diet-specific columns
                record_date     TEXT,
                diet_description TEXT,
                meal_count      INTEGER,
                organization    TEXT,

                -- LLM-generated dietary advice (v2)
                dietary_advice  TEXT,
                fulfillment_report TEXT,

                -- Redirect tracking
                redirect_user   TEXT,
                redirect_quest  TEXT,
                redirect_answer TEXT,

                -- Timestamps
                submitted_at    TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_diet_submissions_student
                ON submissions(student_id);
            CREATE INDEX IF NOT EXISTS idx_diet_submissions_submission
                ON submissions(submission_id);
        """)

        # ── Migration: add v2 columns if they don't exist ────────────────
        v2_columns = {
            "dietary_advice": "TEXT",
            "fulfillment_report": "TEXT",
        }
        existing = {
            row["name"] for row in
            conn.execute("PRAGMA table_info(submissions)").fetchall()
        }
        for col, col_type in v2_columns.items():
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE submissions ADD COLUMN {col} {col_type}")
                    print(f"  ✓ Migration: added column '{col}' to submissions")
                except Exception as e:
                    print(f"  ⚠ Migration warning for {col}: {e}")

        conn.commit()
    finally:
        conn.close()


# ── Helpers ──────────────────────────────────────────────────────────────

def _extract_responses(raw_data: dict) -> str:
    """Extract per-question responses from webhook callback data."""
    responses = {}

    answer_sheet = raw_data.get("answer_sheet", [])
    for sheet in answer_sheet:
        answers = sheet.get("answers", [])
        for item in answers:
            q = item.get("question", {})
            title = q.get("title", "")
            ans = item.get("answer", "")

            if isinstance(ans, dict):
                ans_text = ans.get("label", str(ans))
            else:
                ans_text = str(ans)

            if title:
                responses[title] = ans_text

    for field in ["device", "ip", "duration"]:
        if field in raw_data:
            responses[field] = str(raw_data[field])

    return json.dumps(responses, ensure_ascii=False)


def _extract_diet_info(raw_data: dict) -> dict:
    """Extract diet-specific fields from callback data."""
    result = {}

    answer_sheet = raw_data.get("answer_sheet", [])
    for sheet in answer_sheet:
        answers = sheet.get("answers", [])
        for item in answers:
            q = item.get("question", {})
            title = q.get("title", "")
            ans = item.get("answer", "")
            ans_text = str(ans) if not isinstance(ans, dict) else ans.get("label", str(ans))

            if "记录日期" in title or "record_date" in title.lower():
                result["record_date"] = ans_text
            if "饮食" in title or "diet" in title.lower() or "描述" in title:
                result["diet_description"] = ans_text
                meals = 0
                for keyword in ["早餐", "午餐", "晚餐", "breakfast", "lunch", "dinner"]:
                    if keyword in ans_text:
                        meals += 1
                if meals == 0 and ans_text.strip():
                    meals = 1
                result["meal_count"] = meals

    return result


# ── Submission Operations ────────────────────────────────────────────────

def save_submission(student_id: str, submission_id: str = None,
                    name: str = None, email: str = None,
                    raw_data: dict = None, redirect_params: dict = None,
                    dietary_advice: str = None,
                    fulfillment_report: str = None) -> int:
    """
    Save a diet diary submission with per-question responses.
    Returns the submission DB id.
    """
    conn = get_conn()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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

        responses_json = _extract_responses(raw_data) if raw_data else None
        diet_info = _extract_diet_info(raw_data) if raw_data else {}
        organization = raw_data.get("organization") if raw_data else None

        base_cols = [
            "student_id", "submission_id", "name", "email",
            "responses", "raw_data",
            "organization",
            "dietary_advice", "fulfillment_report",
            "redirect_user", "redirect_quest", "redirect_answer",
            "submitted_at",
        ]
        base_vals = [
            student_id, submission_id, name, email,
            responses_json,
            json.dumps(raw_data, ensure_ascii=False) if raw_data else None,
            organization,
            dietary_advice, fulfillment_report,
            redirect_user, redirect_quest, redirect_answer,
            now,
        ]

        extra_cols = []
        extra_vals = []
        for col in ["record_date", "diet_description", "meal_count"]:
            val = diet_info.get(col)
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


def update_submission_advice(db_id: int, dietary_advice: str = None,
                              fulfillment_report: str = None):
    """Update dietary advice for an existing submission."""
    conn = get_conn()
    try:
        updates = []
        params = []
        if dietary_advice is not None:
            updates.append("dietary_advice = ?")
            params.append(dietary_advice)
        if fulfillment_report is not None:
            updates.append("fulfillment_report = ?")
            params.append(fulfillment_report)
        if not updates:
            return
        params.append(db_id)
        conn.execute(
            f"UPDATE submissions SET {', '.join(updates)} WHERE id = ?",
            params
        )
        conn.commit()
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


def get_latest_by_answer() -> dict:
    """Get the most recent submission by answer ID."""
    conn = get_conn()
    try:
        row = conn.execute("""
            SELECT * FROM submissions
            WHERE redirect_answer IS NOT NULL AND redirect_answer != ''
            ORDER BY CAST(redirect_answer AS INTEGER) DESC
            LIMIT 1
        """).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_submission_by_answer_id(answer_id: str) -> dict:
    """Get a submission by its WJX answer ID."""
    conn = get_conn()
    try:
        # Try redirect_answer first
        try:
            row = conn.execute(
                "SELECT * FROM submissions WHERE CAST(redirect_answer AS INTEGER) = ?",
                (int(answer_id),)
            ).fetchone()
            if row:
                return dict(row)
        except (ValueError, TypeError):
            pass
        row = conn.execute(
            "SELECT * FROM submissions WHERE redirect_answer = ?",
            (str(answer_id),)
        ).fetchone()
        if row:
            return dict(row)
        # Fall back to submission_id match
        row = conn.execute(
            "SELECT * FROM submissions WHERE submission_id = ?",
            (str(answer_id),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_submission_by_id(submission_id: str) -> dict:
    """Get a submission by submission ID."""
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


def get_response_value(submission_db_id: int, question: str) -> str:
    """Get a single response value by question title (via JSON extract)."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT json_extract(responses, ?) as val FROM submissions WHERE id = ?",
            (f'$."{question}"', submission_db_id)
        ).fetchone()
        return row["val"] if row else None
    finally:
        conn.close()


def has_existing_submission(student_id: str, submission_id: str = None) -> bool:
    """Check if this student has any previous submissions (excluding current one)."""
    conn = get_conn()
    try:
        if submission_id:
            row = conn.execute("""
                SELECT COUNT(*) as cnt FROM submissions
                WHERE student_id = ? AND (submission_id != ? OR submission_id IS NULL)
            """, (student_id, submission_id)).fetchone()
        else:
            row = conn.execute("""
                SELECT COUNT(*) as cnt FROM submissions
                WHERE student_id = ?
            """, (student_id,)).fetchone()
        return row["cnt"] > 0
    finally:
        conn.close()


def store_submission(raw_data: dict, redirect_params: dict = None):
    """
    High-level function: extract student info from webhook callback,
    save to database. Returns (db_id, student_id, email, is_first_submission).
    """
    answer_sheet = raw_data.get("answer_sheet", [])
    student_id = raw_data.get("account", "unknown")
    name = raw_data.get("name", "")
    email = ""
    ans_id = None

    for sheet in answer_sheet:
        ans_id = str(sheet.get("id", ""))
        for item in sheet.get("answers", []):
            q = item.get("question", {})
            title = q.get("title", "")
            ans = item.get("answer", "")
            ans_text = str(ans) if not isinstance(ans, dict) else ans.get("label", str(ans))
            if "学工号" in title or "学号" in title:
                student_id = ans_text
            if "邮箱" in title:
                email = ans_text
            if "姓名" in title:
                name = ans_text

    submission_id = str(ans_id) if ans_id else None

    # Check if this is the first submission for this student
    is_first = not has_existing_submission(student_id, submission_id)

    # Always set redirect_answer so feedback page can find this record
    if redirect_params is None:
        redirect_params = {}
    if not redirect_params.get("answer"):
        redirect_params["answer"] = submission_id

    db_id = save_submission(
        student_id=student_id,
        submission_id=submission_id or (redirect_params.get("answer") if redirect_params else None),
        name=name,
        email=email,
        raw_data=raw_data,
        redirect_params=redirect_params,
    )

    return db_id, student_id, email, is_first


# ── Init on import ────────────────────────────────────────────────────────

init_db()
