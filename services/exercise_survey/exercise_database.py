#!/usr/bin/env python3
"""
Exercise Survey Database — SQLite-backed persistent storage for exercise diary submissions.
Stores each submission keyed by student ID (学号), supporting multiple
submissions per individual over time.

Mirrors diet_database.py structure for consistency.
"""

import json
import os
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "exercise_data.db")


def get_conn():
    """Get a connection to the SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Initialize database tables."""
    conn = get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS submissions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id      TEXT NOT NULL,
                submission_id   TEXT UNIQUE,
                name            TEXT,
                email           TEXT,

                -- Per-question responses (JSON)
                responses       TEXT,
                raw_data        TEXT,

                -- Exercise-specific columns
                walk_days       INTEGER,
                walk_minutes    INTEGER,
                sedentary_hours REAL,

                -- Exercise details (JSON array: [{name, times, duration_hours}, ...])
                exercises       TEXT,

                -- Organization info from user profile
                organization    TEXT,

                -- Redirect tracking
                redirect_user   TEXT,
                redirect_quest  TEXT,
                redirect_answer TEXT,

                -- Timestamps
                submitted_at    TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_exercise_submissions_student
                ON submissions(student_id);
            CREATE INDEX IF NOT EXISTS idx_exercise_submissions_submission
                ON submissions(submission_id);
        """)
        conn.commit()
    finally:
        conn.close()


# ── Helpers ──────────────────────────────────────────────────────────────

def _extract_responses(raw_data: dict) -> str:
    """Extract per-question responses from API data."""
    responses = {}
    answers = raw_data.get("answers", [])
    for item in answers:
        q = item.get("question", {})
        title = q.get("title", "")
        ans = item.get("answer", "")

        if isinstance(ans, dict):
            ans_text = ans.get("label", str(ans))
        elif isinstance(ans, list):
            ans_text = json.dumps(ans, ensure_ascii=False)
        else:
            ans_text = str(ans)

        if title:
            responses[title] = ans_text

    return json.dumps(responses, ensure_ascii=False)


def _extract_exercise_info(raw_data: dict) -> dict:
    """Extract exercise-specific fields from API data."""
    result = {
        "walk_days": None,
        "walk_minutes": None,
        "sedentary_hours": None,
        "exercises": None,
    }

    answers = raw_data.get("answers", [])
    for item in answers:
        q = item.get("question", {})
        title = q.get("title", "")
        ans = item.get("answer", "")

        if "步行" in title and "几天" in title:
            if isinstance(ans, dict):
                try:
                    result["walk_days"] = int(ans.get("label", 0))
                except (ValueError, TypeError):
                    pass
        elif "步行" in title and "分钟" in title:
            try:
                result["walk_minutes"] = int(ans) if ans else None
            except (ValueError, TypeError):
                pass
        elif "坐着" in title:
            try:
                result["sedentary_hours"] = float(ans) if ans else None
            except (ValueError, TypeError):
                pass
        elif "运动" in title and isinstance(ans, list):
            # Table question with exercise details
            exercises = []
            for ex in ans:
                if isinstance(ex, dict):
                    exercises.append({
                        "name": ex.get("运动名称", ""),
                        "times_per_week": ex.get("过去一周的运动次数（次）", ""),
                        "duration_hours": ex.get("单次运动时长（小时）", ""),
                    })
            result["exercises"] = json.dumps(exercises, ensure_ascii=False)

    return result


# ── Submission Operations ────────────────────────────────────────────────

def save_submission(student_id: str, submission_id: str = None,
                    name: str = None, email: str = None,
                    raw_data: dict = None, redirect_params: dict = None) -> int:
    """Save an exercise submission. Returns the submission DB id."""
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
        ex_info = _extract_exercise_info(raw_data) if raw_data else {}
        organization = raw_data.get("user", {}).get("organization", "") if raw_data else ""

        submitted_at = raw_data.get("submitted_at", now) if raw_data else now

        conn.execute("""
            INSERT INTO submissions
                (student_id, submission_id, name, email,
                 responses, raw_data,
                 walk_days, walk_minutes, sedentary_hours, exercises,
                 organization,
                 redirect_user, redirect_quest, redirect_answer,
                 submitted_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            student_id, submission_id, name, email,
            responses_json,
            json.dumps(raw_data, ensure_ascii=False) if raw_data else None,
            ex_info.get("walk_days"),
            ex_info.get("walk_minutes"),
            ex_info.get("sedentary_hours"),
            ex_info.get("exercises"),
            organization,
            redirect_user, redirect_quest, redirect_answer,
            submitted_at, now
        ))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()


def get_submission_by_answer_id(answer_id: str) -> dict:
    """Get a submission by its WJX answer ID."""
    conn = get_conn()
    try:
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
        row = conn.execute(
            "SELECT * FROM submissions WHERE submission_id = ?",
            (str(answer_id),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_answer_ids() -> set:
    """Get all existing answer IDs in the database."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT redirect_answer, submission_id FROM submissions "
            "WHERE redirect_answer IS NOT NULL OR submission_id IS NOT NULL"
        ).fetchall()
        ids = set()
        for r in rows:
            if r["redirect_answer"]:
                ids.add(str(r["redirect_answer"]))
            if r["submission_id"]:
                ids.add(str(r["submission_id"]))
        return ids
    finally:
        conn.close()


def get_submissions_by_student(student_id: str) -> list:
    """Get all exercise submissions for a student."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT * FROM submissions
            WHERE student_id = ?
            ORDER BY submitted_at DESC
        """, (student_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_exercise_list(student_id=None, date_from=None, date_to=None,
                      keyword=None, page=1, per_page=50):
    """Get paginated exercise records list."""
    conn = get_conn()
    cur = conn.cursor()
    conditions = []
    params = []
    if student_id:
        conditions.append('student_id = ?')
        params.append(student_id)
    if date_from:
        conditions.append('DATE(submitted_at) >= ?')
        params.append(date_from)
    if date_to:
        conditions.append('DATE(submitted_at) <= ?')
        params.append(date_to)
    if keyword:
        conditions.append("(exercises LIKE ? OR name LIKE ?)")
        params.extend(['%' + keyword + '%', '%' + keyword + '%'])

    where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''

    cur.execute(f'SELECT COUNT(*) FROM submissions {where}', params)
    total = cur.fetchone()[0]

    offset = (page - 1) * per_page
    cur.execute(f'''SELECT id, student_id, name, submitted_at,
                    walk_days, walk_minutes, sedentary_hours, exercises, organization
                    FROM submissions {where}
                    ORDER BY submitted_at DESC LIMIT ? OFFSET ?''',
                params + [per_page, offset])
    rows = []
    for r in cur.fetchall():
        try:
            exercises = json.loads(r[7]) if r[7] else []
        except:
            exercises = []
        rows.append({
            'id': r[0], 'student_id': r[1], 'name': r[2],
            'submitted_at': str(r[3])[:19] if r[3] else '',
            'walk_days': r[4] or 0,
            'walk_minutes': r[5] or 0,
            'sedentary_hours': r[6] or 0,
            'exercises': exercises,
            'organization': r[8] or '',
        })
    conn.close()
    return {'rows': rows, 'total': total, 'page': page, 'per_page': per_page}


def get_exercise_summary():
    """Get exercise data summary stats."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM submissions')
    total = cur.fetchone()[0]
    cur.execute('SELECT COUNT(DISTINCT student_id) FROM submissions')
    students = cur.fetchone()[0]
    cur.execute('SELECT MIN(submitted_at), MAX(submitted_at) FROM submissions')
    dr = cur.fetchone()
    cur.execute('SELECT AVG(walk_days) FROM submissions WHERE walk_days IS NOT NULL')
    avg_walk_days = cur.fetchone()[0]
    conn.close()
    return {
        'total': total,
        'students': students,
        'avg_walk_days': round(float(avg_walk_days), 1) if avg_walk_days else 0,
        'date_start': str(dr[0])[:10] if dr[0] else '',
        'date_end': str(dr[1])[:10] if dr[1] else ''
    }


def get_exercise_trend():
    """Get daily exercise submission trend."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DATE(submitted_at) as d, COUNT(*) as cnt FROM submissions GROUP BY d ORDER BY d")
    rows = [{'date': r[0], 'count': r[1]} for r in cur.fetchall()]
    conn.close()
    return rows


def get_exercise_student_distribution():
    """Get student exercise submission distribution."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT student_id, name, COUNT(*) as cnt FROM submissions GROUP BY student_id ORDER BY cnt DESC')
    rows = [{'student_id': r[0], 'name': r[1], 'count': r[2]} for r in cur.fetchall()]
    conn.close()
    return rows


# ── Init on import ────────────────────────────────────────────────────────

init_db()
