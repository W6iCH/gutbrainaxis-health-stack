#!/usr/bin/env python3
"""
DB Migration v2 — Add individual question responses and scale score columns.
Run ONCE: python3 migrate_db_v2.py
"""

import json
import sqlite3
import os
import sys

BASE_DIR = os.environ.get("SURVEY_APP_DIR", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, "survey_data.db")
BACKUP_PATH = os.path.join(BASE_DIR, "survey_data.db.v1.bak")

from survey_analysis import analyze_survey_responses


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def extract_scale_scores(analysis):
    """Pull all scale scores into flat dict for DB columns."""
    scores = analysis.get("scores", {})
    result = {}

    # DEBQ
    debq = scores.get("DEBQ", {})
    if debq:
        result["debq_total"] = debq.get("total")
        subs = debq.get("subscales", {})
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
    gad7 = scores.get("GAD-7", {})
    if gad7:
        result["gad7_score"] = gad7.get("total")
        result["gad7_n_items"] = gad7.get("n_items")
        result["gad7_interpretation"] = gad7.get("interpretation")

    # PHQ-9
    phq9 = scores.get("PHQ-9", {})
    if phq9:
        result["phq9_score"] = phq9.get("total")
        result["phq9_n_items"] = phq9.get("n_items")
        result["phq9_interpretation"] = phq9.get("interpretation")

    # PSQI
    psqi = scores.get("PSQI", {})
    if psqi:
        result["psqi_score"] = psqi.get("total")
        result["psqi_n_items"] = psqi.get("n_items")
        result["psqi_interpretation"] = psqi.get("interpretation")

    # PSS-14
    pss = scores.get("PSS-14", {})
    if pss:
        result["pss14_score"] = pss.get("total")
        result["pss14_n_items"] = pss.get("n_items")
        result["pss14_interpretation"] = pss.get("interpretation")

    # GSRS
    gsrs = scores.get("GSRS", {})
    if gsrs:
        result["gsrs_score"] = gsrs.get("total")
        result["gsrs_n_items"] = gsrs.get("n_items")
        result["gsrs_interpretation"] = gsrs.get("interpretation")

    # IPAQ-S
    ipaq = scores.get("IPAQ-S", {})
    if ipaq:
        result["ipaq_met_min_week"] = ipaq.get("total")
        result["ipaq_interpretation"] = ipaq.get("interpretation")
        subs = ipaq.get("subscales", {})
        result["ipaq_sedentary_min"] = subs.get("静坐时间")

    # VSI
    vsi = scores.get("VSI", {})
    if vsi:
        result["vsi_score"] = vsi.get("total")
        result["vsi_n_items"] = vsi.get("n_items")
        result["vsi_interpretation"] = vsi.get("interpretation")

    # WHOQOL-BREF
    whoqol = scores.get("WHOQOL-BREF", {})
    if whoqol:
        result["whoqol_score"] = whoqol.get("total")
        result["whoqol_n_items"] = whoqol.get("n_items")
        result["whoqol_interpretation"] = whoqol.get("interpretation")

    return result


def extract_raw_responses(raw_data):
    """Extract individual question→answer from raw_data.
    Handles both API format: {answers: [...]} and {data: {rows: [...]}}.
    """
    responses = {}

    # Normalize: get a list of answer dicts
    if isinstance(raw_data, dict):
        if "data" in raw_data:
            d = raw_data["data"]
            if isinstance(d, dict) and "rows" in d:
                rows = d["rows"]
            else:
                rows = []
        elif "answers" in raw_data:
            # Direct {answers: [...], ...} format
            rows = [raw_data]
        else:
            rows = []
    elif isinstance(raw_data, list):
        rows = raw_data
    else:
        return json.dumps({}, ensure_ascii=False)

    for row in rows:
        answers = row.get("answers", [])
        if not isinstance(answers, list):
            answers = []
        for item in answers:
            q = item.get("question", {})
            title = q.get("title", "")
            ans = item.get("answer", "")
            qtype = q.get("question_type", "")

            if isinstance(ans, dict):
                ans_text = ans.get("label", str(ans))
            else:
                ans_text = str(ans)

            # For matrix questions, expand sub-questions
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


def main():
    print("=" * 60)
    print("DB Migration v2")
    print("=" * 60)

    # Step 1: Backup
    if os.path.exists(BACKUP_PATH):
        print("Backup already exists, skipping")
    else:
        print("Backing up current DB...")
        import shutil
        shutil.copy2(DB_PATH, BACKUP_PATH)
        size = os.path.getsize(BACKUP_PATH)
        print(f"  Backup created ({size} bytes)")

    conn = get_conn()

    # Step 2: Check current schema
    current_cols = [row["name"] for row in conn.execute("PRAGMA table_info(submissions)").fetchall()]
    print(f"Current columns ({len(current_cols)}): {', '.join(current_cols)}")

    # Step 3: Add new columns
    new_columns = [
        ("responses", "TEXT"),
        ("item_scores", "TEXT"),
        ("debq_emotional_score", "REAL"),
        ("debq_external_score", "REAL"),
        ("debq_restrained_score", "REAL"),
        ("debq_emotional_mean", "REAL"),
        ("debq_external_mean", "REAL"),
        ("debq_restrained_mean", "REAL"),
        ("debq_emotional_level", "TEXT"),
        ("debq_external_level", "TEXT"),
        ("debq_restrained_level", "TEXT"),
        ("debq_total", "REAL"),
        ("debq_interpretation", "TEXT"),
        ("gad7_score", "REAL"),
        ("gad7_n_items", "INTEGER"),
        ("gad7_interpretation", "TEXT"),
        ("phq9_score", "REAL"),
        ("phq9_n_items", "INTEGER"),
        ("phq9_interpretation", "TEXT"),
        ("psqi_score", "REAL"),
        ("psqi_n_items", "INTEGER"),
        ("psqi_interpretation", "TEXT"),
        ("pss14_score", "REAL"),
        ("pss14_n_items", "INTEGER"),
        ("pss14_interpretation", "TEXT"),
        ("gsrs_score", "REAL"),
        ("gsrs_n_items", "INTEGER"),
        ("gsrs_interpretation", "TEXT"),
        ("ipaq_met_min_week", "REAL"),
        ("ipaq_sedentary_min", "REAL"),
        ("ipaq_interpretation", "TEXT"),
        ("vsi_score", "REAL"),
        ("vsi_n_items", "INTEGER"),
        ("vsi_interpretation", "TEXT"),
        ("whoqol_score", "REAL"),
        ("whoqol_n_items", "INTEGER"),
        ("whoqol_interpretation", "TEXT"),
    ]

    added = 0
    for col_name, col_type in new_columns:
        if col_name not in current_cols:
            conn.execute(f"ALTER TABLE submissions ADD COLUMN {col_name} {col_type}")
            added += 1
    conn.commit()
    print(f"Added {added} new columns")

    # Step 4: Migrate existing data
    rows = conn.execute("SELECT id, raw_data, analysis FROM submissions").fetchall()
    print(f"Migrating {len(rows)} existing records...")

    migrated = 0
    errors = 0
    for row in rows:
        try:
            rowd = dict(row)
            sid = rowd["id"]
            raw_data = json.loads(rowd["raw_data"]) if rowd.get("raw_data") else None

            updates = {}

            # Re-run analysis for fresh scores + responses
            if raw_data:
                # Normalize raw_data for the analysis function
                if "answers" in raw_data:
                    # Wrap in API format
                    analysis_input = {"data": {"rows": [raw_data]}}
                elif "data" in raw_data:
                    analysis_input = raw_data
                else:
                    analysis_input = raw_data

                analysis = analyze_survey_responses(analysis_input)
                if "error" not in analysis:
                    updates.update(extract_scale_scores(analysis))

                # Extract individual question responses with scores
                responses_map = {}
                item_scores_map = {}

                # Get scales items (which have question+answer_text pairs)
                for scale_name, items in analysis.get("scales", {}).items():
                    for item in items:
                        q = item.get("question", "")
                        a = item.get("answer_text", "")
                        qtype = item.get("type", "")
                        if q:
                            responses_map[q] = a

                # Also get basic info and other non-scale answers from raw_data
                for item in raw_data.get("answers", []):
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
                                if q_text and q_text not in responses_map:
                                    responses_map[q_text] = sub_text
                        elif title and title not in responses_map:
                            responses_map[title] = ans_text
                    else:
                        if title and title not in responses_map:
                            responses_map[title] = ans_text

                updates["responses"] = json.dumps(responses_map, ensure_ascii=False)
                updates["item_scores"] = json.dumps(item_scores_map, ensure_ascii=False)

            if updates:
                set_clauses = []
                params = []
                for col, val in updates.items():
                    if val is not None:
                        set_clauses.append(f"{col} = ?")
                        params.append(val)
                    else:
                        set_clauses.append(f"{col} = NULL")
                params.append(sid)
                conn.execute(
                    f"UPDATE submissions SET {', '.join(set_clauses)} WHERE id = ?",
                    params
                )
                migrated += 1
        except Exception as e:
            import traceback
            print(f"  Error migrating id={row['id']}: {e}")
            traceback.print_exc()
            errors += 1

    conn.commit()
    conn.close()

    # Step 5: Verify
    conn2 = get_conn()
    sample = conn2.execute("""
        SELECT id, student_id, name,
               debq_total, debq_emotional_score, debq_external_score, debq_restrained_score,
               gad7_score, phq9_score, psqi_score, pss14_score,
               gsrs_score, ipaq_met_min_week, vsi_score, whoqol_score
        FROM submissions ORDER BY id DESC LIMIT 5
    """).fetchall()

    print(f"\nMigration: {migrated} updated, {errors} errors")
    print("Last 5 records (scale scores):")
    for r in sample:
        d = dict(r)
        present = {k: v for k, v in d.items()
                   if k not in ("id", "student_id", "name") and v is not None}
        print(f"  ID={d['id']} {d.get('name','')} ({d.get('student_id','')})")
        for k, v in present.items():
            print(f"    {k}: {v}")

    conn2.close()
    print("\nMigration v2 complete!")


if __name__ == "__main__":
    main()
