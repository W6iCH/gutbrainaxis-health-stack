#!/usr/bin/env python3
"""
Survey Pipeline — Full Feedback Pipeline
=========================================
Orchestrates: fetch data → analyze → store in DB → send email

Triggered by:
  1. Survey redirect to /report (feedback_server.py calls this when params present)
  2. Manual: python3 survey_pipeline.py run [email]
  3. After bot submission (survey_bot.py calls this)
"""

import json
import os
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from survey_analysis import analyze_survey_responses
from survey_database import store_submission, get_latest_submission, extract_student_id


def run_pipeline(recipient_email: str = None, submission_id: str = "",
                 redirect_params: dict = None) -> dict:
    """
    Full pipeline:
    1. Load or fetch survey data
    2. Analyze
    3. Store in database
    4. Send email (with retry + fallback to admin)

    Returns result dict with pipeline status.
    """
    result = {
        "timestamp": datetime.now().isoformat(),
        "status": "started",
        "steps": {},
    }

    # ── Step 1: Load survey data ──────────────────────────────────────
    print("[1/4] Loading survey data...")
    data_path = os.path.join(BASE_DIR, "sjtu_survey_data_page1.json")
    try:
        with open(data_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        result["steps"]["load"] = "ok"
        print(f"  ✓ Loaded {os.path.getsize(data_path)} bytes")
    except Exception as e:
        result["steps"]["load"] = f"error: {e}"
        print(f"  ✗ Failed to load data: {e}")
        result["status"] = "failed"
        return result

    # ── Step 2: Analyze ───────────────────────────────────────────────
    print("[2/4] Analyzing survey data...")
    analysis = analyze_survey_responses(raw_data)
    if "error" in analysis:
        result["steps"]["analyze"] = f"error: {analysis['error']}"
        print(f"  ✗ Analysis failed: {analysis['error']}")
        result["status"] = "failed"
        return result

    scales = list(analysis.get("scores", {}).keys())
    result["steps"]["analyze"] = f"ok ({len(scales)} scales)"
    print(f"  ✓ {len(scales)} scales detected: {', '.join(scales)}")

    # ── Step 3: Store in database ─────────────────────────────────────
    print("[3/4] Storing in database...")
    student_id = None
    try:
        db_id, student_id, db_email = store_submission(
            raw_data, analysis, redirect_params
        )
        # Use the email from database if not provided
        if not recipient_email and db_email:
            recipient_email = db_email
        result["steps"]["store"] = f"ok (id={db_id}, student={student_id})"
        print(f"  ✓ Stored: student={student_id}, email={db_email}")
    except Exception as e:
        result["steps"]["store"] = f"error: {e}"
        print(f"  ✗ Database error: {e}")
        result["status"] = "degraded"

    # ── Step 4: Send email ────────────────────────────────────────────
    print("[4/4] Sending email feedback...")
    if not recipient_email:
        print("  ⚠ No recipient email, skipping email")
        result["steps"]["email"] = "skipped (no email)"
    else:
        try:
            from email_feedback import send_immediate
            redirect_answer = ""
            if redirect_params:
                redirect_answer = str(redirect_params.get("answer", ""))
            item_id = send_immediate(
                recipient=recipient_email,
                submission_id=submission_id or redirect_answer
            )
            if item_id:
                result["steps"]["email"] = f"queued (id={item_id})"
                print(f"  ✓ Email queued for {recipient_email}")
            else:
                result["steps"]["email"] = "failed"
                print(f"  ✗ Email send failed for {recipient_email}")
        except Exception as e:
            result["steps"]["email"] = f"error: {e}"
            print(f"  ✗ Email error: {e}")

    result["status"] = "completed"
    print(f"\n✅ Pipeline completed (student={student_id if 'student_id' in dir() else 'unknown'})")
    return result


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Survey Feedback Pipeline"
    )
    parser.add_argument("action", nargs="?", default="run",
                        choices=["run", "status", "students"])
    parser.add_argument("--email", help="Recipient email")
    parser.add_argument("--submission", help="Submission ID", default="")

    args = parser.parse_args()

    if args.action == "status":
        from survey_database import get_latest_submission, get_all_students
        students = get_all_students()
        latest = get_latest_submission()
        print(f"📊 数据库状态")
        print(f"  学生数: {len(students)}")
        print(f"  最新提交: {latest.get('created_at', 'N/A') if latest else '无'}")
        print(f"  学生列表:")
        for s in students:
            print(f"    {s['student_id']} ({s['name']}) — {s['count']}次")
        return

    if args.action == "students":
        from survey_database import get_all_students
        students = get_all_students()
        for s in students:
            submissions = get_submissions_by_student(s['student_id'])
            print(f"\n{s['student_id']} ({s['name']}) — {s['count']}次")
            for sub in submissions:
                analysis = json.loads(sub['analysis']) if sub.get('analysis') else {}
                scales = list(analysis.get('scores', {}).keys())
                print(f"  [{sub['created_at']}] {sub.get('submission_id','')[:16]} 量表:{len(scales)}")
        return

    # Default: run pipeline
    # pass submission as redirect_params so db stores redirect_answer
    redirect_params = None
    if args.submission:
        redirect_params = {"answer": args.submission}
    result = run_pipeline(
        recipient_email=args.email,
        submission_id=args.submission,
        redirect_params=redirect_params,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
