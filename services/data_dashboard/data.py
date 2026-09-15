#!/usr/bin/env python3
"""
Data Dashboard v2 — Data access layer for the rebuilt data.gutbrainaxis.online
=============================================================================
Supports: Survey, Diet, Exercise data + Completion Status statistics.
"""

import sqlite3
import json
import statistics
import os
from datetime import datetime, timedelta

_BASE = os.environ.get('APP_BASE', '/opt')
SURVEY_DB = os.environ.get('SURVEY_DB', os.path.join(_BASE, 'sjtu_survey_pro', 'survey_data.db'))
DIET_DB = os.environ.get('DIET_DB', os.path.join(_BASE, 'diet_survey', 'diet_data.db'))
EXERCISE_DB = os.environ.get('EXERCISE_DB', os.path.join(_BASE, 'exercise_survey', 'exercise_data.db'))
ROSTER_PATH = os.environ.get('ROSTER_PATH', os.path.join(_BASE, 'data_dashboard', 'roster.json'))

# ── Today's date for completion calculations ──────────────────────────
# Use yesterday as cutoff: today's records are not counted until the day is complete
TODAY_DT = datetime.now() - timedelta(days=1)
TODAY = TODAY_DT.strftime("%Y-%m-%d")


def get_survey_conn():
    return sqlite3.connect(SURVEY_DB)

def get_diet_conn():
    return sqlite3.connect(DIET_DB)

def get_exercise_conn():
    return sqlite3.connect(EXERCISE_DB)


# =========================================================================
# Scale definitions
# =========================================================================

SCALE_DEFS = {
    'debq_emotional': {
        'name': 'DEBQ-情绪性饮食', 'short': 'DEBQ情绪',
        'full_name': '荷兰饮食行为量表-情绪性饮食',
        'description': '评估因情绪变化（如焦虑、孤独、愤怒）而进食的倾向。包含13个条目，评分1-5分。',
        'range': '1-5 (均分)',
        'score_col': 'debq_emotional_mean',
        'level_col': 'debq_emotional_level',
        'normal': '低水平', 'borderline': '中等水平', 'abnormal': '高水平',
        'max_val': 5.0, 'min_val': 1.0
    },
    'debq_external': {
        'name': 'DEBQ-外部性饮食', 'short': 'DEBQ外部',
        'full_name': '荷兰饮食行为量表-外部性饮食',
        'description': '评估对外部食物线索的倾向。包含10个条目，评分1-5分。',
        'range': '1-5 (均分)',
        'score_col': 'debq_external_mean',
        'level_col': 'debq_external_level',
        'normal': '低水平', 'borderline': '中等水平', 'abnormal': '高水平',
        'max_val': 5.0, 'min_val': 1.0
    },
    'debq_restrained': {
        'name': 'DEBQ-限制性饮食', 'short': 'DEBQ限制',
        'full_name': '荷兰饮食行为量表-限制性饮食',
        'description': '评估为控制体重而限制食物摄入的倾向。包含10个条目，评分1-5分。',
        'range': '1-5 (均分)',
        'score_col': 'debq_restrained_mean',
        'level_col': 'debq_restrained_level',
        'normal': '低水平', 'borderline': '中等水平', 'abnormal': '高水平',
        'max_val': 5.0, 'min_val': 1.0
    },
    'gad7': {
        'name': 'GAD-7', 'short': 'GAD7',
        'full_name': '广泛性焦虑障碍量表',
        'description': '评估过去两周内的焦虑症状。包含7个条目，总分0-21分。',
        'range': '0-21',
        'score_col': 'gad7_score',
        'interpretation_col': 'gad7_interpretation',
        'normal': lambda s: s < 5, 'borderline': lambda s: 5 <= s < 10, 'abnormal': lambda s: s >= 10,
        'normal_label': '正常', 'borderline_label': '轻度', 'abnormal_label': '中重度',
        'max_val': 21.0, 'min_val': 0.0
    },
    'phq9': {
        'name': 'PHQ-9', 'short': 'PHQ9',
        'full_name': '病人健康问卷抑郁量表',
        'description': '评估过去两周内的抑郁症状。包含9个条目，总分0-27分。',
        'range': '0-27',
        'score_col': 'phq9_score',
        'interpretation_col': 'phq9_interpretation',
        'normal': lambda s: s < 5, 'borderline': lambda s: 5 <= s < 10, 'abnormal': lambda s: s >= 10,
        'normal_label': '正常', 'borderline_label': '轻度', 'abnormal_label': '中重度',
        'max_val': 27.0, 'min_val': 0.0
    },
    'psqi': {
        'name': 'PSQI', 'short': 'PSQI',
        'full_name': '匹兹堡睡眠质量指数',
        'description': '评估近一个月的睡眠质量。包含19个自评条目，总分0-21分。',
        'range': '0-21',
        'score_col': 'psqi_score',
        'interpretation_col': 'psqi_interpretation',
        'normal': lambda s: s <= 5, 'borderline': lambda s: 5 < s < 10, 'abnormal': lambda s: s >= 10,
        'normal_label': '良好', 'borderline_label': '一般', 'abnormal_label': '较差',
        'max_val': 21.0, 'min_val': 0.0
    },
    'pss14': {
        'name': 'PSS-14', 'short': 'PSS14',
        'full_name': '知觉压力量表',
        'description': '评估过去一个月的压力感知水平。包含14个条目，总分0-56分。',
        'range': '0-56',
        'score_col': 'pss14_score',
        'interpretation_col': 'pss14_interpretation',
        'normal': lambda s: s <= 15, 'borderline': lambda s: 15 < s <= 30, 'abnormal': lambda s: s > 30,
        'normal_label': '低压力', 'borderline_label': '中等压力', 'abnormal_label': '高压力',
        'max_val': 56.0, 'min_val': 0.0
    },
    'gsrs': {
        'name': 'GSRS', 'short': 'GSRS',
        'full_name': '胃肠道症状评定量表',
        'description': '评估胃肠道症状的严重程度。包含15个条目，总分15-105分。',
        'range': '15-105',
        'score_col': 'gsrs_score',
        'interpretation_col': 'gsrs_interpretation',
        'normal': lambda s: s < 30, 'borderline': lambda s: 30 <= s < 50, 'abnormal': lambda s: s >= 50,
        'normal_label': '无明显症状', 'borderline_label': '轻度症状', 'abnormal_label': '明显症状',
        'max_val': 105.0, 'min_val': 15.0
    },
    'ipaq': {
        'name': 'IPAQ', 'short': 'IPAQ(MET)',
        'full_name': '国际体力活动量表',
        'description': '评估一周体力活动水平，以MET-min/周为单位。',
        'range': '0-3000+ (MET-min/周)',
        'score_col': 'ipaq_met_min_week',
        'interpretation_col': 'ipaq_interpretation',
        'is_level_based': True,
        'normal': lambda s: s >= 1500, 'borderline': lambda s: 600 <= s < 1500, 'abnormal': lambda s: s < 600,
        'normal_label': '高体力活动', 'borderline_label': '中等体力活动', 'abnormal_label': '低体力活动',
        'max_val': 3000.0, 'min_val': 0.0
    },
    'vsi': {
        'name': 'VSI', 'short': 'VSI',
        'full_name': '内脏敏感性指数',
        'description': '评估与胃肠道症状相关的焦虑和过度警觉。包含15个条目，总分15-105分。',
        'range': '15-105',
        'score_col': 'vsi_score',
        'interpretation_col': 'vsi_interpretation',
        'normal': lambda s: s < 30, 'borderline': lambda s: 30 <= s < 50, 'abnormal': lambda s: s >= 50,
        'normal_label': '低内脏焦虑', 'borderline_label': '中等内脏焦虑', 'abnormal_label': '高内脏焦虑',
        'max_val': 105.0, 'min_val': 15.0
    },
    'whoqol': {
        'name': 'WHOQOL-BREF', 'short': 'WHOQOL',
        'full_name': '世界卫生组织生存质量简表',
        'description': '评估个体在生理、心理、社会和环境领域的生存质量。',
        'range': '26-130',
        'score_col': 'whoqol_score',
        'interpretation_col': 'whoqol_interpretation',
        'normal': lambda s: s >= 80, 'borderline': lambda s: 60 <= s < 80, 'abnormal': lambda s: s < 60,
        'normal_label': '良好', 'borderline_label': '一般', 'abnormal_label': '较差',
        'max_val': 130.0, 'min_val': 26.0
    },
    'bmi': {
        'name': 'BMI', 'short': 'BMI',
        'full_name': '身体质量指数',
        'description': '基于身高和体重计算的身体质量指数。',
        'range': '0-50',
        'score_col': 'bmi_score',
        'interpretation_col': 'bmi_interpretation',
        'normal': lambda s: 18.5 <= s <= 23.9, 'borderline': lambda s: 24.0 <= s <= 27.9, 'abnormal': lambda s: s < 18.5 or s > 27.9,
        'normal_label': '正常', 'borderline_label': '超重', 'abnormal_label': '异常',
        'max_val': 40.0, 'min_val': 10.0
    }
}

SCALE_NAMES = list(SCALE_DEFS.keys())


def get_scale_level(scale, score):
    sd = SCALE_DEFS[scale]
    if isinstance(sd.get('normal'), str):
        if score <= 2.0:
            return '正常'
        elif score <= 3.0:
            return '临界'
        else:
            return '异常'
    if sd.get('is_level_based'):
        if sd['normal'](score):
            return '正常'
        elif sd['borderline'](score):
            return '临界'
        else:
            return '异常'
    if callable(sd['normal']):
        if sd['normal'](score):
            return '正常'
        elif sd['borderline'](score):
            return '临界'
        else:
            return '异常'
    return '正常'


# =========================================================================
# ROSTER — Student list for completion tracking
# =========================================================================

def load_roster():
    """Load the target student roster from JSON file."""
    if os.path.exists(ROSTER_PATH):
        with open(ROSTER_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


# =========================================================================
# COMPLETION STATUS — Scoring & color logic
# =========================================================================

# ── Scale completion ──────────────────────────────────────────────────
SCALE_TASKS = [
    {"name": "第1次量表", "deadline": "2026-07-06", "window_start": "2026-07-03",
     "window_end": "2026-07-13", "extended_end": "2026-07-13"},
    {"name": "第2次量表", "deadline": "2026-07-15", "window_start": "2026-07-12",
     "window_end": "2026-07-18"},
    {"name": "第3次量表", "deadline": "2026-07-27", "window_start": "2026-07-24",
     "window_end": "2026-07-30"},
]

EXERCISE_TASKS = [
    {"name": "第1次运动", "deadline": "2026-07-13", "window_start": "2026-07-10",
     "window_end": "2026-07-16"},
    {"name": "第2次运动", "deadline": "2026-07-20", "window_start": "2026-07-17",
     "window_end": "2026-07-23"},
    {"name": "第3次运动", "deadline": "2026-07-27", "window_start": "2026-07-24",
     "window_end": "2026-07-30"},
]

DIET_START = "2026-07-06"
DIET_END = "2026-07-29"
DIET_MAX_SCORE = 22


def check_scale_completion(student_id):
    """Check scale completion status and score for a student."""
    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT submitted_at FROM submissions WHERE student_id = ? ORDER BY submitted_at",
        (student_id,))
    submissions = [r[0] for r in cur.fetchall()]
    conn.close()

    completed = 0
    task_statuses = []
    all_done = True
    any_past_fail = False

    for task in SCALE_TASKS:
        ws = datetime.strptime(task["window_start"], "%Y-%m-%d")
        we = datetime.strptime(task["window_end"], "%Y-%m-%d")
        deadline_dt = datetime.strptime(task["deadline"], "%Y-%m-%d")

        # Check if any submission falls within window
        task_done = False
        # Parse submission to date-only to avoid time-portion skew
        for sub_at in submissions:
            try:
                sub_dt = datetime.strptime(sub_at[:10], "%Y-%m-%d")
            except:
                continue
            if ws <= sub_dt <= we:
                task_done = True
                break

        if not task_done and task.get("extended_end"):
            ee = datetime.strptime(task["extended_end"], "%Y-%m-%d")
            for sub_at in submissions:
                try:
                    sub_dt = datetime.strptime(sub_at[:10], "%Y-%m-%d")
                except:
                    continue
                if ws <= sub_dt <= ee:
                    task_done = True
                    break

        if task_done:
            completed += 1

        deadline_passed = TODAY_DT > deadline_dt + timedelta(days=3)
        task_statuses.append({
            "name": task["name"],
            "done": task_done,
            "deadline_passed": deadline_passed,
        })

        if not task_done:
            all_done = False
            if deadline_passed:
                any_past_fail = True

    score = 10 if completed == 3 else round(completed * 3.33, 1)

    if all_done:
        status = "completed"
        color = "green"
        label = "已完成"
    elif any_past_fail:
        status = "failed"
        color = "red"
        label = "未达标"
    else:
        status = "in_progress"
        color = "yellow"
        label = "进行中"

    return {
        "completed": completed,
        "total": len(SCALE_TASKS),
        "score": score,
        "max_score": 10,
        "status": status,
        "color": color,
        "label": label,
        "tasks": task_statuses,
    }


def check_exercise_completion(student_id):
    """Check exercise completion status and score."""
    conn = get_exercise_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT submitted_at FROM submissions WHERE student_id = ? ORDER BY submitted_at",
        (student_id,))
    submissions = [r[0] for r in cur.fetchall()]
    conn.close()

    completed = 0
    task_statuses = []
    all_done = True
    any_past_fail = False

    for task in EXERCISE_TASKS:
        ws = datetime.strptime(task["window_start"], "%Y-%m-%d")
        we = datetime.strptime(task["window_end"], "%Y-%m-%d")
        deadline_dt = datetime.strptime(task["deadline"], "%Y-%m-%d")

        task_done = False
        for sub_at in submissions:
            try:
                sub_dt = datetime.strptime(sub_at[:10], "%Y-%m-%d")
            except:
                continue
            if ws <= sub_dt <= we:
                task_done = True
                break

        if task_done:
            completed += 1

        deadline_passed = TODAY_DT > deadline_dt + timedelta(days=3)
        task_statuses.append({
            "name": task["name"],
            "done": task_done,
            "deadline_passed": deadline_passed,
        })

        if not task_done:
            all_done = False
            if deadline_passed:
                any_past_fail = True

    score = completed

    if all_done:
        status = "completed"
        color = "green"
        label = "已完成"
    elif any_past_fail:
        status = "failed"
        color = "red"
        label = "未达标"
    else:
        status = "in_progress"
        color = "yellow"
        label = "进行中"

    return {
        "completed": completed,
        "total": len(EXERCISE_TASKS),
        "score": score,
        "max_score": 3,
        "status": status,
        "color": color,
        "label": label,
        "tasks": task_statuses,
    }


def check_diet_completion(student_id):
    """Check diet completion status and score."""
    conn = get_diet_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT DATE(submitted_at) as d FROM submissions "
        "WHERE student_id = ? ORDER BY d",
        (student_id,))
    diet_dates = set(r[0] for r in cur.fetchall())
    conn.close()

    # Also check record_date field
    conn = get_diet_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT record_date FROM submissions "
        "WHERE student_id = ? AND record_date IS NOT NULL",
        (student_id,))
    for r in cur.fetchall():
        rd = str(r[0])[:10]
        if rd:
            diet_dates.add(rd)
    conn.close()

    start = datetime.strptime(DIET_START, "%Y-%m-%d")
    end = datetime.strptime(DIET_END, "%Y-%m-%d")

    total_days = (end - start).days + 1
    days_with_records = 0
    missing_days = 0
    past_missing = 0

    day_statuses = []
    d = start
    while d <= end:
        date_str = d.strftime("%Y-%m-%d")
        has_record = date_str in diet_dates
        if has_record:
            days_with_records += 1
        else:
            missing_days += 1
            if d <= TODAY_DT:
                past_missing += 1

        day_statuses.append({
            "date": date_str,
            "has_record": has_record,
        })
        d += timedelta(days=1)

    score = min(DIET_MAX_SCORE, max(0, DIET_MAX_SCORE - past_missing))

    # Determine color
    if days_with_records == total_days:
        status = "completed"
        color = "green"
        label = "已完成"
    elif past_missing > 0:
        status = "failed"
        color = "red"
        label = "未达标"
    else:
        status = "in_progress"
        color = "yellow"
        label = "进行中"

    return {
        "completed": days_with_records,
        "total": total_days,
        "missing": missing_days,
        "past_missing": past_missing,
        "score": score,
        "max_score": DIET_MAX_SCORE,
        "status": status,
        "color": color,
        "label": label,
        "days": day_statuses,
    }


def _build_diet_segments(diet):
    """Build per-day color list for diet bar display (24 days)."""
    segments = []
    raw_days = diet.get("days", [])
    for d in raw_days:
        if d["has_record"]:
            segments.append("#27ae60")
        elif d["date"] <= TODAY:
            segments.append("#e74c3c")
        else:
            segments.append("#ccc")
    return segments


def get_completion_table():
    """Get completion statistics table for all roster students."""
    roster = load_roster()
    rows = []

    for student in roster:
        sid = str(student.get("student_id", student.get("学号", "")))
        name = student.get("name", student.get("姓名", ""))

        scale = check_scale_completion(sid)
        exercise = check_exercise_completion(sid)
        diet = check_diet_completion(sid)

        total_score = scale["score"] + exercise["score"] + diet["score"]
        max_total = scale["max_score"] + exercise["max_score"] + diet["max_score"]

        rows.append({
            "student_id": sid,
            "name": name,
            "scale_completed": f"{scale['completed']}/{scale['total']}",
            "scale_color": scale["color"],
            "scale_label": scale["label"],
            "scale_score": scale["score"],
            "scale_dots": [
                "green" if t["done"] else ("red" if t["deadline_passed"] else "empty")
                for t in scale["tasks"]
            ],
            "exercise_completed": f"{exercise['completed']}/{exercise['total']}",
            "exercise_color": exercise["color"],
            "exercise_label": exercise["label"],
            "exercise_score": exercise["score"],
            "exercise_dots": [
                "green" if t["done"] else ("red" if t["deadline_passed"] else "empty")
                for t in exercise["tasks"]
            ],
            "diet_completed": f"{diet['completed']}/{diet['total']}",
            "diet_color": diet["color"],
            "diet_label": diet["label"],
            "diet_score": diet["score"],
            "diet_segments": _build_diet_segments(diet),
            "diet_dots": [
                "green" if d["has_record"] else ("red" if d["date"] <= TODAY else "empty")
                for d in diet["days"]
            ],
            "total_score": round(total_score, 1),
            "max_total": max_total,
        })

    # Sort by total score descending
    rows.sort(key=lambda x: x["total_score"], reverse=True)
    return rows


def get_completion_summary():
    """Get summary statistics for completion panel."""
    table = get_completion_table()
    total = len(table)
    scale_green = sum(1 for r in table if r["scale_color"] == "green")
    scale_red = sum(1 for r in table if r["scale_color"] == "red")
    scale_yellow = sum(1 for r in table if r["scale_color"] == "yellow")
    ex_green = sum(1 for r in table if r["exercise_color"] == "green")
    ex_red = sum(1 for r in table if r["exercise_color"] == "red")
    ex_yellow = sum(1 for r in table if r["exercise_color"] == "yellow")
    diet_green = sum(1 for r in table if r["diet_color"] == "green")
    diet_red = sum(1 for r in table if r["diet_color"] == "red")
    diet_yellow = sum(1 for r in table if r["diet_color"] == "yellow")

    scores = [r["total_score"] for r in table]
    return {
        "total": total,
        "avg_score": round(statistics.mean(scores), 1) if scores else 0,
        "max_score": max(scores) if scores else 0,
        "min_score": min(scores) if scores else 0,
        "scale_distribution": {"green": scale_green, "red": scale_red, "yellow": scale_yellow},
        "exercise_distribution": {"green": ex_green, "red": ex_red, "yellow": ex_yellow},
        "diet_distribution": {"green": diet_green, "red": diet_red, "yellow": diet_yellow},
    }


# =========================================================================
# DASHBOARD (Original functions, preserved and extended)
# =========================================================================

def get_dashboard_summary():
    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(DISTINCT student_id) FROM submissions')
    survey_students = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM submissions')
    total_submissions = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT student_id) FROM submissions WHERE student_id LIKE '5%' OR student_id LIKE '1%'")
    planned_students = cur.fetchone()[0]
    conn.close()

    conn_d = get_diet_conn()
    cur_d = conn_d.cursor()
    cur_d.execute('SELECT COUNT(DISTINCT student_id) FROM submissions')
    diet_students = cur_d.fetchone()[0]
    cur_d.execute('SELECT COUNT(*) FROM submissions')
    diet_total = cur_d.fetchone()[0]
    cur_d.execute('SELECT MIN(submitted_at), MAX(submitted_at) FROM submissions')
    diet_dr = cur_d.fetchone()
    conn_d.close()

    conn_e = get_exercise_conn()
    cur_e = conn_e.cursor()
    cur_e.execute('SELECT COUNT(DISTINCT student_id) FROM submissions')
    exercise_students = cur_e.fetchone()[0]
    cur_e.execute('SELECT COUNT(*) FROM submissions')
    exercise_total = cur_e.fetchone()[0]
    cur_e.execute('SELECT MIN(submitted_at), MAX(submitted_at) FROM submissions')
    ex_dr = cur_e.fetchone()
    conn_e.close()

    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute('SELECT MIN(submitted_at), MAX(submitted_at) FROM submissions')
    survey_dr = cur.fetchone()
    cur.execute('SELECT student_id, COUNT(*) as cnt FROM submissions GROUP BY student_id')
    sub_dist = {row[0]: row[1] for row in cur.fetchall()}
    conn.close()

    roster = load_roster()
    total_planned = len(roster) if roster else 248

    dates = []
    if survey_dr[0]: dates.append(str(survey_dr[0])[:10])
    if survey_dr[1]: dates.append(str(survey_dr[1])[:10])
    if diet_dr and diet_dr[0]: dates.append(str(diet_dr[0])[:10])
    if diet_dr and diet_dr[1]: dates.append(str(diet_dr[1])[:10])
    if ex_dr and ex_dr[0]: dates.append(str(ex_dr[0])[:10])
    if ex_dr and ex_dr[1]: dates.append(str(ex_dr[1])[:10])

    return {
        'survey_students': survey_students,
        'total_submissions': total_submissions,
        'planned_students': planned_students,
        'total_planned': total_planned,
        'completion_rate': round(total_submissions / max(total_planned, 1) * 100, 1),
        'diet_students': diet_students,
        'diet_total': diet_total,
        'exercise_students': exercise_students,
        'exercise_total': exercise_total,
        'submission_distribution': sub_dist,
        'data_start': min(dates) if dates else '',
        'data_end': max(dates) if dates else '',
    }


def get_recent_activity(limit=10):
    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute('SELECT student_id, name, submitted_at FROM submissions ORDER BY submitted_at DESC LIMIT ?', (limit,))
    survey_rows = [(r[0], r[1], r[2][:19], '量表提交') for r in cur.fetchall()]
    conn.close()

    conn = get_diet_conn()
    cur = conn.cursor()
    cur.execute('SELECT student_id, name, submitted_at FROM submissions ORDER BY submitted_at DESC LIMIT ?', (limit,))
    diet_rows = [(r[0], r[1], r[2][:19], '饮食记录') for r in cur.fetchall()]
    conn.close()

    conn = get_exercise_conn()
    cur = conn.cursor()
    cur.execute('SELECT student_id, name, submitted_at FROM submissions ORDER BY submitted_at DESC LIMIT ?', (limit,))
    ex_rows = [(r[0], r[1], r[2][:19], '运动记录') for r in cur.fetchall()]
    conn.close()

    combined = survey_rows + diet_rows + ex_rows
    combined.sort(key=lambda x: x[2], reverse=True)
    return combined[:limit]


def get_warnings():
    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute('SELECT student_id, name, email, COUNT(*) as cnt FROM submissions GROUP BY student_id HAVING cnt < 3 ORDER BY cnt')
    rows = [{'student_id': r[0], 'name': r[1], 'email': r[2], 'count': r[3]} for r in cur.fetchall()]
    conn.close()
    return rows


# =========================================================================
# SCALES (preserved from original)
# =========================================================================

def get_all_scales_table():
    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute('''
        SELECT s1.* FROM submissions s1
        INNER JOIN (
            SELECT student_id, MAX(id) as max_id FROM submissions GROUP BY student_id
        ) s2 ON s1.id = s2.max_id
    ''')
    cols = [d[0] for d in cur.description]
    rows = []
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        cur2 = conn.cursor()
        cur2.execute('SELECT COUNT(*) FROM submissions WHERE student_id = ?', (d['student_id'],))
        sub_count = cur2.fetchone()[0]
        rows.append({
            'student_id': d['student_id'],
            'name': d['name'],
            'sub_count': sub_count,
            'debq_emotional_score': d.get('debq_emotional_mean'),
            'debq_external_score': d.get('debq_external_mean'),
            'debq_restrained_score': d.get('debq_restrained_mean'),
            'gad7_score': d.get('gad7_score'),
            'phq9_score': d.get('phq9_score'),
            'psqi_score': d.get('psqi_score'),
            'pss14_score': d.get('pss14_score'),
            'gsrs_score': d.get('gsrs_score'),
            'ipaq_score': d.get('ipaq_met_min_week'),
            'vsi_score': d.get('vsi_score'),
            'whoqol_score': d.get('whoqol_score'),
            'bmi_score': d.get('bmi_score'),
            'last_submit': d.get('submitted_at'),
        })
    conn.close()
    return rows


def get_scale_data(scale_name):
    sd = SCALE_DEFS[scale_name]
    score_col = sd['score_col']

    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute(f'''
        SELECT s1.student_id, s1.name, s1.submitted_at, s1.{score_col}
        FROM submissions s1
        INNER JOIN (
            SELECT student_id, MAX(id) as max_id FROM submissions GROUP BY student_id
        ) s2 ON s1.id = s2.max_id
        WHERE s1.{score_col} IS NOT NULL
    ''')

    students = []
    scores = []
    level_counts = {'正常': 0, '临界': 0, '异常': 0}
    for row in cur.fetchall():
        score = row[3]
        if score is None:
            continue
        try:
            score = float(score)
        except:
            continue
        level = get_scale_level(scale_name, score)
        students.append({
            'student_id': row[0], 'name': row[1],
            'submitted_at': row[2], 'score': score, 'level': level
        })
        scores.append(score)
        level_counts[level] = level_counts.get(level, 0) + 1
    conn.close()

    if not scores:
        return {'students': [], 'stats': {'mean': 0, 'median': 0, 'std': 0, 'min': 0, 'max': 0, 'abnormal_rate': 0, 'total': 0}, 'distribution': level_counts, 'scores': []}

    mean_val = statistics.mean(scores)
    median_val = statistics.median(scores)
    try:
        std_val = statistics.stdev(scores)
    except:
        std_val = 0
    abnormal_rate = round(level_counts.get('异常', 0) / len(scores) * 100, 1) if scores else 0

    students.sort(key=lambda x: x['score'])
    return {
        'students': students,
        'stats': {'mean': round(mean_val, 2), 'median': round(median_val, 2), 'std': round(std_val, 2),
                   'min': round(min(scores), 2), 'max': round(max(scores), 2),
                   'abnormal_rate': abnormal_rate, 'total': len(scores)},
        'distribution': level_counts, 'scores': scores
    }


def get_cross_scale_data(scale1, scale2):
    sd1 = SCALE_DEFS[scale1]
    sd2 = SCALE_DEFS[scale2]
    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute(f'''
        SELECT s1.student_id, s1.name, s1.{sd1['score_col']} as sc1, s1.{sd2['score_col']} as sc2
        FROM submissions s1
        INNER JOIN (SELECT student_id, MAX(id) as max_id FROM submissions GROUP BY student_id) s2 ON s1.id = s2.max_id
        WHERE s1.{sd1['score_col']} IS NOT NULL AND s1.{sd2['score_col']} IS NOT NULL
    ''')
    data = []
    for row in cur.fetchall():
        try:
            v1 = float(row[2]); v2 = float(row[3])
            data.append({'student_id': row[0], 'name': row[1], 'x': v1, 'y': v2})
        except:
            continue
    conn.close()
    n = len(data)
    if n < 2:
        return {'data': data, 'correlation': 0, 'n': n}
    x_vals = [d['x'] for d in data]; y_vals = [d['y'] for d in data]
    try:
        r = statistics.correlation(x_vals, y_vals)
    except:
        r = 0
    return {'data': data, 'correlation': round(r, 4), 'n': n, 'scale1': scale1, 'scale2': scale2}


# =========================================================================
# DIET (preserved from original)
# =========================================================================

def get_diet_summary():
    conn = get_diet_conn()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM submissions'); total = cur.fetchone()[0]
    cur.execute('SELECT COUNT(DISTINCT student_id) FROM submissions'); students = cur.fetchone()[0]
    cur.execute('SELECT MIN(submitted_at), MAX(submitted_at) FROM submissions'); dr = cur.fetchone()
    cur.execute('SELECT AVG(meal_count) FROM submissions WHERE meal_count IS NOT NULL'); avg_meals = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM submissions WHERE diet_description IS NOT NULL AND diet_description != ''"); with_desc = cur.fetchone()[0]
    conn.close()
    return {
        'total': total, 'students': students, 'with_description': with_desc,
        'avg_meals': round(float(avg_meals), 1) if avg_meals else 0,
        'date_start': str(dr[0])[:10] if dr[0] else '', 'date_end': str(dr[1])[:10] if dr[1] else ''
    }


def get_diet_trend():
    conn = get_diet_conn()
    cur = conn.cursor()
    cur.execute("SELECT DATE(submitted_at) as d, COUNT(*) as cnt FROM submissions GROUP BY d ORDER BY d")
    return [{'date': r[0], 'count': r[1]} for r in cur.fetchall()]


def get_diet_student_distribution():
    conn = get_diet_conn()
    cur = conn.cursor()
    cur.execute('SELECT student_id, name, COUNT(*) as cnt FROM submissions GROUP BY student_id ORDER BY cnt DESC')
    return [{'student_id': r[0], 'name': r[1], 'count': r[2]} for r in cur.fetchall()]


def get_diet_list(student_id=None, date_from=None, date_to=None, keyword=None, page=1, per_page=50):
    conn = get_diet_conn()
    cur = conn.cursor()
    conditions = []; params = []
    if student_id: conditions.append('student_id = ?'); params.append(student_id)
    if date_from: conditions.append('DATE(submitted_at) >= ?'); params.append(date_from)
    if date_to: conditions.append('DATE(submitted_at) <= ?'); params.append(date_to)
    if keyword: conditions.append("(diet_description LIKE ? OR dietary_advice LIKE ?)"); params.extend(['%'+keyword+'%', '%'+keyword+'%'])
    where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
    cur.execute(f'SELECT COUNT(*) FROM submissions {where}', params); total = cur.fetchone()[0]
    offset = (page-1)*per_page
    cur.execute(f'SELECT id, student_id, name, submitted_at, record_date, diet_description, meal_count, dietary_advice FROM submissions {where} ORDER BY submitted_at DESC LIMIT ? OFFSET ?', params+[per_page, offset])
    rows = []
    for r in cur.fetchall():
        rows.append({
            'id': r[0], 'student_id': r[1], 'name': r[2],
            'submitted_at': str(r[3])[:19] if r[3] else '',
            'record_date': str(r[4])[:10] if r[4] else '',
            'diet_description': r[5] or '', 'meal_count': r[6] or 0, 'dietary_advice': r[7] or ''
        })
    conn.close()
    return {'rows': rows, 'total': total, 'page': page, 'per_page': per_page}


# =========================================================================
# EXERCISE (new)
# =========================================================================

def get_exercise_summary():
    conn = get_exercise_conn()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM submissions'); total = cur.fetchone()[0]
    cur.execute('SELECT COUNT(DISTINCT student_id) FROM submissions'); students = cur.fetchone()[0]
    cur.execute('SELECT MIN(submitted_at), MAX(submitted_at) FROM submissions'); dr = cur.fetchone()
    cur.execute('SELECT AVG(walk_days) FROM submissions WHERE walk_days IS NOT NULL'); avg_walk = cur.fetchone()[0]
    conn.close()
    return {
        'total': total, 'students': students,
        'avg_walk_days': round(float(avg_walk), 1) if avg_walk else 0,
        'date_start': str(dr[0])[:10] if dr[0] else '', 'date_end': str(dr[1])[:10] if dr[1] else ''
    }


def get_exercise_trend():
    conn = get_exercise_conn()
    cur = conn.cursor()
    cur.execute("SELECT DATE(submitted_at) as d, COUNT(*) as cnt FROM submissions GROUP BY d ORDER BY d")
    return [{'date': r[0], 'count': r[1]} for r in cur.fetchall()]


def get_exercise_list(student_id=None, date_from=None, date_to=None, keyword=None, page=1, per_page=50):
    conn = get_exercise_conn()
    cur = conn.cursor()
    conditions = []; params = []
    if student_id: conditions.append('student_id = ?'); params.append(student_id)
    if date_from: conditions.append('DATE(submitted_at) >= ?'); params.append(date_from)
    if date_to: conditions.append('DATE(submitted_at) <= ?'); params.append(date_to)
    if keyword: conditions.append("(exercises LIKE ? OR name LIKE ?)"); params.extend(['%'+keyword+'%', '%'+keyword+'%'])
    where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
    cur.execute(f'SELECT COUNT(*) FROM submissions {where}', params); total = cur.fetchone()[0]
    offset = (page-1)*per_page
    cur.execute(f'''SELECT id, student_id, name, submitted_at, walk_days, walk_minutes,
                    sedentary_hours, exercises, organization
                    FROM submissions {where} ORDER BY submitted_at DESC LIMIT ? OFFSET ?''', params+[per_page, offset])
    rows = []
    for r in cur.fetchall():
        try: ex_list = json.loads(r[7]) if r[7] else []
        except: ex_list = []
        rows.append({
            'id': r[0], 'student_id': r[1], 'name': r[2],
            'submitted_at': str(r[3])[:19] if r[3] else '',
            'walk_days': r[4] or 0, 'walk_minutes': r[5] or 0,
            'sedentary_hours': r[6] or 0, 'exercises': ex_list,
            'organization': r[8] or '',
        })
    conn.close()
    return {'rows': rows, 'total': total, 'page': page, 'per_page': per_page}


def get_exercise_student_distribution():
    conn = get_exercise_conn()
    cur = conn.cursor()
    cur.execute('SELECT student_id, name, COUNT(*) as cnt FROM submissions GROUP BY student_id ORDER BY cnt DESC')
    return [{'student_id': r[0], 'name': r[1], 'count': r[2]} for r in cur.fetchall()]


# =========================================================================
# STUDENTS (preserved, extended with exercise)
# =========================================================================

def get_student_status(student_id):
    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM submissions WHERE student_id = ?', (student_id,))
    survey_cnt = cur.fetchone()[0]
    cur.execute('SELECT MIN(submitted_at), MAX(submitted_at) FROM submissions WHERE student_id = ?', (student_id,))
    survey_dates = cur.fetchone()
    conn.close()

    conn = get_diet_conn()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM submissions WHERE student_id = ?', (student_id,))
    diet_cnt = cur.fetchone()[0]
    cur.execute('SELECT MAX(submitted_at) FROM submissions WHERE student_id = ?', (student_id,))
    last_diet = cur.fetchone()[0]
    conn.close()

    conn = get_exercise_conn()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM submissions WHERE student_id = ?', (student_id,))
    exercise_cnt = cur.fetchone()[0]
    conn.close()

    is_planned = str(student_id).startswith('5') or str(student_id).startswith('1')
    if survey_cnt == 0:
        status = '未开始'
    elif survey_cnt >= 4:
        status = '已完成'
    else:
        status = '进行中'
    if not is_planned:
        status = '计划外'

    return {
        'survey_count': survey_cnt, 'diet_count': diet_cnt, 'exercise_count': exercise_cnt,
        'first_submit': str(survey_dates[0])[:19] if survey_dates and survey_dates[0] else '',
        'last_submit': str(survey_dates[1])[:19] if survey_dates and survey_dates[1] else '',
        'last_active': str(max(survey_dates[1] if survey_dates and survey_dates[1] else '',
                               last_diet if last_diet else ''))[:19],
        'is_planned': is_planned, 'status': status, 'status_badge': status
    }


def get_students_list(search=None, status_filter=None, page=1, per_page=50):
    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute('''
        SELECT s1.student_id, s1.name, s1.email FROM submissions s1
        INNER JOIN (SELECT student_id, MAX(id) as max_id FROM submissions GROUP BY student_id) s2 ON s1.id = s2.max_id
    ''')
    survey_students = {r[0]: {'name': r[1], 'email': r[2] or ''} for r in cur.fetchall()}
    conn.close()

    conn = get_diet_conn()
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT student_id, name, email FROM submissions')
    for r in cur.fetchall():
        if r[0] not in survey_students:
            survey_students[r[0]] = {'name': r[1], 'email': r[2] or ''}
    conn.close()

    all_students = []
    for sid, info in survey_students.items():
        status_info = get_student_status(sid)
        if search:
            if search.lower() not in str(sid).lower() and search.lower() not in str(info['name']).lower():
                continue
        if status_filter and status_filter != '全部':
            if status_filter == '计划内' and not status_info['is_planned']: continue
            if status_filter == '计划外' and status_info['is_planned']: continue
            if status_filter not in ['全部','计划内','计划外'] and status_info['status_badge'] != status_filter: continue
        all_students.append({
            'student_id': sid, 'name': info['name'], 'email': info['email'],
            'survey_count': status_info['survey_count'], 'diet_count': status_info['diet_count'],
            'exercise_count': status_info['exercise_count'],
            'last_active': status_info['last_active'],
            'status': status_info['status_badge'], 'is_planned': status_info['is_planned']
        })

    status_order = {'已完成': 0, '进行中': 1, '未开始': 2, '计划外': 3}
    all_students.sort(key=lambda x: (status_order.get(x['status'], 9), x['student_id']))
    total = len(all_students)
    paginated = all_students[(page-1)*per_page: (page-1)*per_page + per_page]
    return {'students': paginated, 'total': total, 'page': page, 'per_page': per_page}


def get_student_overview(student_id):
    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute('SELECT * FROM submissions WHERE student_id = ? ORDER BY id', (student_id,))
    cols = [d[0] for d in cur.description]
    submissions = []
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        try: d['responses_dict'] = json.loads(d['responses']) if d['responses'] else {}
        except: d['responses_dict'] = {}
        try: d['analysis_dict'] = json.loads(d['analysis']) if d['analysis'] else {}
        except: d['analysis_dict'] = {}
        submissions.append(d)
    conn.close()

    if not submissions:
        return None

    latest = submissions[-1]
    scale_keys = [
        ('debq_emotional', 'DEBQ情绪性饮食', 'debq_emotional_mean', 'debq_emotional_level'),
        ('debq_external', 'DEBQ外部性饮食', 'debq_external_mean', 'debq_external_level'),
        ('debq_restrained', 'DEBQ限制性饮食', 'debq_restrained_mean', 'debq_restrained_level'),
        ('gad7', 'GAD-7焦虑', 'gad7_score', None),
        ('phq9', 'PHQ-9抑郁', 'phq9_score', None),
        ('psqi', 'PSQI睡眠', 'psqi_score', None),
        ('pss14', 'PSS-14压力', 'pss14_score', None),
        ('gsrs', 'GSRS胃肠道', 'gsrs_score', None),
        ('ipaq', 'IPAQ体力活动', 'ipaq_met_min_week', None),
        ('vsi', 'VSI内脏焦虑', 'vsi_score', None),
        ('whoqol', 'WHOQOL生活质量', 'whoqol_score', None),
        ('bmi', 'BMI', 'bmi_score', None),
    ]

    scale_cards = []
    for key, label, score_col, level_col in scale_keys:
        score = latest.get(score_col)
        level = latest.get(level_col) if level_col else None
        if score is not None:
            try:
                score = float(score)
                if level is None: level = get_scale_level(key, score)
            except: score = 0; level = '未知'
        else: score = 0; level = '未知'

        prev_score = None
        if len(submissions) >= 2:
            prev = submissions[-2].get(score_col)
            if prev is not None:
                try: prev_score = float(prev)
                except: pass

        trend = 'none'
        if prev_score is not None:
            if score > prev_score: trend = 'up'
            elif score < prev_score: trend = 'down'
            else: trend = 'same'

        scale_cards.append({
            'key': key, 'label': label, 'score': round(score, 1) if score else 0,
            'level': level, 'trend': trend,
            'prev_score': round(prev_score, 1) if prev_score is not None else None
        })

    info = {
        'student_id': latest['student_id'], 'name': latest['name'], 'email': latest['email'],
        'submission_count': len(submissions),
        'first_submit': submissions[0]['submitted_at'][:19] if submissions[0]['submitted_at'] else '',
        'last_submit': latest['submitted_at'][:19] if latest['submitted_at'] else '',
    }

    conn = get_diet_conn()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM submissions WHERE student_id = ?', (student_id,))
    info['diet_count'] = cur.fetchone()[0]
    conn.close()

    conn = get_exercise_conn()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM submissions WHERE student_id = ?', (student_id,))
    info['exercise_count'] = cur.fetchone()[0]
    conn.close()

    info['is_planned'] = str(student_id).startswith('5') or str(student_id).startswith('1')
    if len(submissions) >= 4: info['status'] = '已完成'
    elif len(submissions) > 0: info['status'] = '进行中'
    else: info['status'] = '未开始'
    if not info['is_planned']: info['status'] = '计划外'

    return {'info': info, 'scale_cards': scale_cards, 'submissions': submissions}


# (Remaining student functions preserved as-is from original)
def get_student_responses(student_id):
    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute('SELECT id, student_id, name, submission_id, submitted_at, responses, analysis FROM submissions WHERE student_id = ? ORDER BY id', (student_id,))
    submissions = []
    for row in cur.fetchall():
        try: responses = json.loads(row[5]) if row[5] else {}
        except: responses = {}
        try: analysis = json.loads(row[6]) if row[6] else {}
        except: analysis = {}
        submissions.append({
            'id': row[0], 'student_id': row[1], 'name': row[2], 'submission_id': row[3],
            'submitted_at': str(row[4])[:19] if row[4] else '',
            'responses': responses, 'analysis': analysis
        })
    conn.close()
    return submissions


def get_student_diet(student_id, date_from=None, date_to=None):
    conn = get_diet_conn()
    cur = conn.cursor()
    conditions = ['student_id = ?']; params = [student_id]
    if date_from: conditions.append('DATE(submitted_at) >= ?'); params.append(date_from)
    if date_to: conditions.append('DATE(submitted_at) <= ?'); params.append(date_to)
    where = 'WHERE ' + ' AND '.join(conditions)
    cur.execute(f'SELECT id, student_id, name, submitted_at, record_date, diet_description, meal_count, dietary_advice FROM submissions {where} ORDER BY submitted_at DESC', params)
    rows = []
    for r in cur.fetchall():
        rows.append({
            'id': r[0], 'student_id': r[1], 'name': r[2],
            'submitted_at': str(r[3])[:19] if r[3] else '',
            'record_date': str(r[4])[:10] if r[4] else '',
            'diet_description': r[5] or '', 'meal_count': r[6] or 0, 'dietary_advice': r[7] or ''
        })
    conn.close()
    return rows


def get_student_raw(student_id):
    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute('SELECT id, submission_id, submitted_at, raw_data, analysis, item_scores, responses FROM submissions WHERE student_id = ? ORDER BY id', (student_id,))
    rows = []
    for r in cur.fetchall():
        def safe_parse(x):
            try: return json.loads(x) if x else {}
            except: return {'raw': str(x)}
        rows.append({
            'id': r[0], 'submission_id': r[1], 'submitted_at': str(r[2])[:19] if r[2] else '',
            'raw_data': safe_parse(r[3]), 'analysis': safe_parse(r[4]),
            'item_scores': safe_parse(r[5]), 'responses': safe_parse(r[6]),
        })
    conn.close()
    return rows


def get_student_trends(student_id):
    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute('''
        SELECT id, submitted_at, debq_emotional_mean, debq_external_mean, debq_restrained_mean,
               gad7_score, phq9_score, psqi_score, pss14_score, gsrs_score,
               ipaq_met_min_week, vsi_score, whoqol_score, bmi_score
        FROM submissions WHERE student_id = ? ORDER BY id
    ''', (student_id,))
    rows = []
    for r in cur.fetchall():
        rows.append({
            'id': r[0], 'date': str(r[1])[:10] if r[1] else '',
            'DEBQ-情绪性饮食': float(r[2]) if r[2] is not None else None,
            'DEBQ-外部性饮食': float(r[3]) if r[3] is not None else None,
            'DEBQ-限制性饮食': float(r[4]) if r[4] is not None else None,
            'GAD-7': float(r[5]) if r[5] is not None else None,
            'PHQ-9': float(r[6]) if r[6] is not None else None,
            'PSQI': float(r[7]) if r[7] is not None else None,
            'PSS-14': float(r[8]) if r[8] is not None else None,
            'GSRS': float(r[9]) if r[9] is not None else None,
            'IPAQ': float(r[10]) if r[10] is not None else None,
            'VSI': float(r[11]) if r[11] is not None else None,
            'WHOQOL': float(r[12]) if r[12] is not None else None,
            'BMI': float(r[13]) if r[13] is not None else None,
        })
    conn.close()
    conn = get_diet_conn()
    cur = conn.cursor()
    cur.execute("SELECT DATE(submitted_at) as d, COUNT(*) FROM submissions WHERE student_id = ? GROUP BY d ORDER BY d", (student_id,))
    diet_trend = [{'date': r[0], 'count': r[1]} for r in cur.fetchall()]
    conn.close()
    return {'scale_trends': rows, 'diet_trend': diet_trend}


# =========================================================================
# STATISTICS
# =========================================================================

def get_stats_overview():
    scale_avgs = {}
    for key, sd in SCALE_DEFS.items():
        data = get_scale_data(key)
        scale_avgs[key] = {
            'name': sd['name'], 'short': sd['short'],
            'mean': data['stats'].get('mean', 0),
            'distribution': data['distribution'], 'total': data['stats'].get('total', 0)
        }
    return scale_avgs


def get_correlation_matrix():
    conn = get_survey_conn()
    cur = conn.cursor()
    cur.execute('''
        SELECT s1.debq_emotional_mean, s1.debq_external_mean, s1.debq_restrained_mean,
               s1.gad7_score, s1.phq9_score, s1.psqi_score, s1.pss14_score,
               s1.gsrs_score, s1.ipaq_met_min_week, s1.vsi_score, s1.whoqol_score, s1.bmi_score
        FROM submissions s1
        INNER JOIN (SELECT student_id, MAX(id) as max_id FROM submissions GROUP BY student_id) s2 ON s1.id = s2.max_id
    ''')
    data = []
    for r in cur.fetchall():
        try:
            vals = [float(x) if x is not None else None for x in r]
            if all(v is not None for v in vals): data.append(vals)
        except: continue
    conn.close()

    labels = ['DEBQ情绪','DEBQ外部','DEBQ限制','GAD7','PHQ9','PSQI','PSS14','GSRS','IPAQ','VSI','WHOQOL','BMI']
    n = len(labels)
    matrix = [[0]*n for _ in range(n)]
    if len(data) < 3: return {'labels': labels, 'matrix': matrix}
    for i in range(n):
        for j in range(n):
            if i == j: matrix[i][j] = 1.0
            else:
                xi = [d[i] for d in data]; xj = [d[j] for d in data]
                try: matrix[i][j] = round(statistics.correlation(xi, xj), 4)
                except: matrix[i][j] = 0
    return {'labels': labels, 'matrix': matrix}


# =========================================================================
# EXPORT
# =========================================================================

def get_export_scales(scale_name=None, student_id=None):
    conn = get_survey_conn()
    cur = conn.cursor()
    if student_id: cur.execute('SELECT * FROM submissions WHERE student_id = ? ORDER BY id', (student_id,))
    else: cur.execute('SELECT s1.* FROM submissions s1 INNER JOIN (SELECT student_id, MAX(id) as max_id FROM submissions GROUP BY student_id) s2 ON s1.id = s2.max_id')
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


def get_export_responses(student_id=None):
    conn = get_survey_conn()
    cur = conn.cursor()
    if student_id: cur.execute('SELECT id, student_id, name, submitted_at, responses FROM submissions WHERE student_id = ? ORDER BY id', (student_id,))
    else: cur.execute('SELECT id, student_id, name, submitted_at, responses FROM submissions ORDER BY id')
    rows = []
    for r in cur.fetchall():
        try: resp = json.loads(r[4]) if r[4] else {}
        except: resp = {}
        rows.append({'id': r[0], 'student_id': r[1], 'name': r[2], 'submitted_at': str(r[3])[:19] if r[3] else '', 'responses': resp})
    conn.close()
    return rows


def get_export_diet(student_id=None, date_from=None, date_to=None):
    conn = get_diet_conn()
    cur = conn.cursor()
    conditions = []; params = []
    if student_id: conditions.append('student_id = ?'); params.append(student_id)
    if date_from: conditions.append('DATE(submitted_at) >= ?'); params.append(date_from)
    if date_to: conditions.append('DATE(submitted_at) <= ?'); params.append(date_to)
    where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
    cur.execute(f'SELECT id, student_id, name, submitted_at, record_date, diet_description, meal_count, dietary_advice FROM submissions {where} ORDER BY submitted_at', params)
    rows = []
    for r in cur.fetchall():
        rows.append({'id': r[0], 'student_id': r[1], 'name': r[2], 'submitted_at': str(r[3])[:19] if r[3] else '', 'record_date': str(r[4])[:10] if r[4] else '', 'diet_description': r[5] or '', 'meal_count': r[6] or 0, 'dietary_advice': r[7] or ''})
    conn.close()
    return rows


def get_export_exercise(student_id=None, date_from=None, date_to=None):
    conn = get_exercise_conn()
    cur = conn.cursor()
    conditions = []; params = []
    if student_id: conditions.append('student_id = ?'); params.append(student_id)
    if date_from: conditions.append('DATE(submitted_at) >= ?'); params.append(date_from)
    if date_to: conditions.append('DATE(submitted_at) <= ?'); params.append(date_to)
    where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
    cur.execute(f'SELECT id, student_id, name, submitted_at, walk_days, walk_minutes, sedentary_hours, exercises FROM submissions {where} ORDER BY submitted_at', params)
    rows = []
    for r in cur.fetchall():
        rows.append({'id': r[0], 'student_id': r[1], 'name': r[2], 'submitted_at': str(r[3])[:19] if r[3] else '', 'walk_days': r[4] or 0, 'walk_minutes': r[5] or 0, 'sedentary_hours': r[6] or 0, 'exercises': r[7] or '[]'})
    conn.close()
    return rows
