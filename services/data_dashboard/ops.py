#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ops.py — 数据看板：运行状况 / 依从性时序 / 数据质量 / 系统健康
=============================================================================
本模块补齐用户提出的 4 个「看不到」的缺口（D），全部与既有 `/completion`、
`/stats` 同风格（Flask 路由 + JSON API + CSV/JSON 导出）：

1. `queue_overview()`    —— **队列运行状况**：LLM 队列（pending/done/failed/
                             等待窗口）、量表/饮食邮件队列（pending/sent/failed）、
                             webhook 回调健康与重试积压。
2. `adherence_series()`  —— **依从性时间序列**：每人逐日提交矩阵（热力图）+ 各链路
                             （量表/饮食/运动）整体完成率趋势。
3. `data_quality()`      —— **数据质量**：缺失项、异常值、重复提交、未映射条目。
4. `system_health()`     —— **系统健康**：聚合各服务 `/healthz` + 端口监听 + DB 状态。

设计约束
--------
* **只读**：所有 SQL 打开只读连接（`mode=ro`），绝不写业务库；
* **隔离失败**：单个库/目录异常不影响其它检查项（逐项 try/except）；
* **无 PII 外泄**：仅返回学号（= 研究标识），不含姓名/邮箱；
* **可导出**：每个数据集都有 `to_rows()` 形式供 CSV/JSON 导出复用。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import urllib.request
from datetime import datetime, timedelta

import data as _d

BASE = os.environ.get("APP_BASE") or _d._BASE
ROSTER_PATH = os.environ.get("ROSTER_PATH", _d.ROSTER_PATH)

SURVEY_DIR = os.path.dirname(_d.SURVEY_DB)
DIET_DIR = os.path.dirname(_d.DIET_DB)
EXERCISE_DIR = os.path.dirname(_d.EXERCISE_DB)


# ── 基础 ──────────────────────────────────────────────────────────────────

def _ro(path) -> sqlite3.Connection | None:
    """只读连接（URI mode=ro）；不存在/打不开返回 None。"""
    if not path or not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _count_dir(path: str) -> int:
    try:
        return len([f for f in os.listdir(path) if f.endswith(".json")])
    except OSError:
        return 0


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _roster_ids() -> list:
    try:
        roster = _d.load_roster()
        out = []
        for s in roster or []:
            if isinstance(s, dict):
                sid = s.get("student_id") or s.get("id") or s.get("学号")
                if sid:
                    out.append(str(sid))
            elif s:
                out.append(str(s))
        return out
    except Exception:                                        # noqa: BLE001
        return []


# ══════════════════════════════════════════════════════════════════════════
# 1) 队列运行状况
# ══════════════════════════════════════════════════════════════════════════

def queue_overview() -> dict:
    """LLM 队列 + 邮件队列 + webhook 的实时运行状况。"""
    out = {"llm": {}, "email": {}, "webhook": {}, "generated_at":
           datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # ── LLM 队列（含窗口调度状态）────────────────────────────────────
    llm_dir = os.path.join(DIET_DIR, ".llm_queue")
    llm = {
        "pending": _count_dir(os.path.join(llm_dir, "pending")),
        "processing": _count_dir(os.path.join(llm_dir, "processing")),
        "done": _count_dir(os.path.join(llm_dir, "done")),
        "failed": _count_dir(os.path.join(llm_dir, "failed")),
        "exists": os.path.isdir(llm_dir),
    }
    st = _read_json(os.path.join(llm_dir, "state.json"))
    try:
        from lib_schedule import load_config, window_state
        ws = window_state(load_config())
    except Exception:                                        # noqa: BLE001
        ws = {}
    llm.update({
        "mode": ws.get("mode") or st.get("mode") or "realtime",
        "in_window": ws.get("in_window", True),
        "waiting_window": bool(ws.get("waiting") or st.get("suspended")),
        "window": f"{ws.get('window_start')}–{ws.get('window_end')}"
                   if ws.get("window_start") else "",
        "timezone": ws.get("timezone") or st.get("timezone"),
        "reason": st.get("reason", ""),
        "day_consumed": st.get("day_consumed", 0),
        "window_consumed": st.get("window_consumed", 0),
        "last_processed_at": st.get("last_processed_at"),
        "state_updated_at": st.get("updated_at"),
    })
    out["llm"] = llm

    # ── 邮件队列（量表 / 饮食）───────────────────────────────────────
    for key, qdir in (("survey", os.path.join(SURVEY_DIR, ".email_queue")),
                      ("diet", os.path.join(DIET_DIR, ".email_queue"))):
        out["email"][key] = {
            "pending": _count_dir(os.path.join(qdir, "pending")),
            "sent": _count_dir(os.path.join(qdir, "sent")),
            "failed": _count_dir(os.path.join(qdir, "failed")),
            "exists": os.path.isdir(qdir),
        }

    # ── Webhook 健康（量表有状态文件；饮食仅记录事件）────────────────
    ws_state = _read_json(os.path.join(SURVEY_DIR, ".webhook_queue", "state.json"))
    retry_dir = os.path.join(SURVEY_DIR, ".webhook_queue", "retry")
    last = ws_state.get("last_received_at")
    idle_min = None
    if last:
        try:
            idle_min = round((datetime.now() - datetime.fromisoformat(last))
                             .total_seconds() / 60, 1)
        except ValueError:
            idle_min = None
    try:
        from lib_schedule import load_config
        threshold = int(load_config().get("webhook.timer_only_after_minutes") or 180)
    except Exception:                                        # noqa: BLE001
        threshold = 180
    out["webhook"] = {
        "survey_received_total": ws_state.get("received_total", 0),
        "survey_new_rows_total": ws_state.get("new_rows_total", 0),
        "survey_failed_total": ws_state.get("failed_total", 0),
        "survey_last_received_at": last,
        "survey_idle_minutes": idle_min,
        "survey_retry_pending": _count_dir(retry_dir),
        "survey_mode": ("timer_only" if not last else
                        ("degraded_timer_fallback" if (idle_min or 0) > threshold
                         else "webhook")),
        "timer_only_after_minutes": threshold,
    }
    return out


# ══════════════════════════════════════════════════════════════════════════
# 2) 依从性时间序列
# ══════════════════════════════════════════════════════════════════════════

CHANNEL_SQL = {
    "scale": ("submissions", "student_id", "date(COALESCE(submitted_at, created_at))"),
    "diet": ("submissions", "student_id", "date(COALESCE(record_date, submitted_at, created_at))"),
    "exercise": ("submissions", "student_id", "date(COALESCE(submitted_at, created_at))"),
}


def _channel_daily(db_path: str, channel: str) -> dict:
    """返回 {date: {student_id, ...}}。"""
    conn = _ro(db_path)
    if conn is None:
        return {}
    table, sid, datex = CHANNEL_SQL[channel]
    try:
        rows = conn.execute(
            f"SELECT {datex} AS d, {sid} AS s, COUNT(*) AS n "
            f"FROM {table} WHERE s IS NOT NULL OR 1=1 GROUP BY d, s"
        ).fetchall()
        out: dict = {}
        for r in rows:
            d, s = r["d"], r["s"]
            if not d or not s:
                continue
            out.setdefault(str(d), {})[str(s)] = int(r["n"] or 0)
        return out
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def adherence_series(days: int = 30, end: str = None) -> dict:
    """
   每人逐日提交矩阵 + 各链路整体完成率趋势。
   days: 观察窗口天数（默认 30）；end: 结束日期 YYYY-MM-DD（默认今天）。
    """
    try:
        end_dt = datetime.strptime(end, "%Y-%m-%d") if end else datetime.now()
    except ValueError:
        end_dt = datetime.now()
    dates = [(end_dt - timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range(days - 1, -1, -1)]
    dset = set(dates)

    roster = _roster_ids()
    channels = {
        "scale": _channel_daily(_d.SURVEY_DB, "scale"),
        "diet": _channel_daily(_d.DIET_DB, "diet"),
        "exercise": _channel_daily(_d.EXERCISE_DB, "exercise"),
    }

    # 学生全集：roster ∪ 各链路出现过的学号
    students = set(roster)
    for ch in channels.values():
        for d, m in ch.items():
            if d in dset:
                students.update(m.keys())
    students = sorted(students)

    # 逐人逐日矩阵（任一链路提交记 1）
    matrix = []
    for sid in students:
        cells, total = [], 0
        for d in dates:
            hit = 0
            for ch in channels.values():
                if ch.get(d, {}).get(sid):
                    hit = 1
                    break
            cells.append(hit)
            total += hit
        matrix.append({"student_id": sid, "cells": cells, "active_days": total,
                       "rate": round(total / len(dates), 3) if dates else 0.0})

    # 各链路趋势：当日在册学生中提交过的比例
    denom = max(1, len(students))
    trend = {}
    for name, ch in channels.items():
        trend[name] = [round(len(ch.get(d, {})) / denom, 4) for d in dates]
    overall = []
    for i, d in enumerate(dates):
        hit = 0
        for sid in students:
            if any(ch.get(d, {}).get(sid) for ch in channels.values()):
                hit += 1
        overall.append(round(hit / denom, 4))

    # 按链路汇总
    summary = {}
    for name, ch in channels.items():
        submitters = set()
        rows_total = 0
        for d in dates:
            for s, n in ch.get(d, {}).items():
                submitters.add(s)
                rows_total += n
        summary[name] = {
            "students_submitted": len(submitters),
            "roster": len(roster),
            "coverage": round(len(submitters) / denom, 4),
            "records": rows_total,
        }

    return {
        "dates": dates,
        "students": matrix,
        "n_students": len(students),
        "n_roster": len(roster),
        "trend": trend,
        "overall_trend": overall,
        "summary": summary,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ══════════════════════════════════════════════════════════════════════════
# 3) 数据质量
# ══════════════════════════════════════════════════════════════════════════

# 量表分值的合理区间（超出即视为异常值；区间取自量表量程）
SCORE_RANGES = {
    "gad7_score": (0, 21), "phq9_score": (0, 27), "psqi_score": (0, 21),
    "pss14_score": (0, 56), "gsrs_score": (0, 105), "vsi_score": (0, 90),
    "whoqol_score": (0, 200), "ipaq_met_min_week": (0, 20000),
    "ipaq_sedentary_min": (0, 1440),
    "debq_emotional_mean": (1, 5), "debq_external_mean": (1, 5),
    "debq_restrained_mean": (1, 5), "bmi_score": (10, 60),
}


def data_quality() -> dict:
    """缺失项 / 异常值 / 重复提交 / 未映射条目。"""
    out = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "missing": {}, "outliers": {}, "duplicates": {}, "unmapped": {}}

    # ── 量表库 ────────────────────────────────────────────────────────
    conn = _ro(_d.SURVEY_DB)
    if conn:
        try:
            total = conn.execute("SELECT COUNT(*) n FROM submissions").fetchone()["n"]
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(submissions)").fetchall()}
            # 缺失：分析未完成 / 关键标识缺失 / 邮箱缺失
            def _null(col):
                if col not in cols:
                    return 0
                try:
                    return conn.execute(
                        f"SELECT COUNT(*) n FROM submissions WHERE {col} IS NULL "
                        f"OR TRIM(COALESCE({col},''))=''").fetchone()["n"]
                except sqlite3.Error:
                    return 0
            out["missing"]["survey"] = {
                "total": total,
                "analysis_null": _null("analysis"),
                "email_null": _null("email"),
                "name_null": _null("name"),
                "submission_id_null": _null("submission_id"),
                "score_cols_all_null": _count_all_null(conn, cols),
            }
            # 重复：同一 submission_id 出现多次（UNIQUE 约束下应为 0）
            dup = conn.execute(
                "SELECT submission_id, COUNT(*) n FROM submissions "
                "WHERE submission_id IS NOT NULL AND submission_id<>'' "
                "GROUP BY submission_id HAVING n>1").fetchall()
            rows_no_redirect = conn.execute(
                "SELECT COUNT(*) n FROM submissions WHERE redirect_answer IS NULL"
            ).fetchone()["n"]
            out["duplicates"]["survey"] = {
                "by_submission_id": [{"submission_id": r["submission_id"], "n": r["n"]}
                                     for r in dup],
                "duplicate_groups": len(dup),
                "missing_redirect_answer": rows_no_redirect,
            }
            # 异常值
            out["outliers"]["survey"] = _outliers(conn, cols, SCORE_RANGES)
            # 未映射条目（原生严格引擎：未识别即报错；此处统计仍有记录的）
            unmapped = 0
            try:
                rows = conn.execute(
                    "SELECT analysis FROM submissions WHERE analysis LIKE '%_UNKNOWN%' "
                    "OR analysis LIKE '%unknown_items%'").fetchall()
                for r in rows:
                    try:
                        meta = (json.loads(r["analysis"]) or {}).get("_meta", {})
                        unmapped += len(meta.get("unknown_items", []) or [])
                    except (json.JSONDecodeError, TypeError):
                        unmapped += 1
            except sqlite3.Error:
                pass
            out["unmapped"]["survey"] = {"count": unmapped}
        except sqlite3.Error as e:                            # noqa: BLE001
            out["missing"]["survey"] = {"error": str(e)}
        finally:
            conn.close()
    else:
        out["missing"]["survey"] = {"error": "数据库不可读或不存在"}

    # ── 饮食库 ────────────────────────────────────────────────────────
    conn = _ro(_d.DIET_DB)
    if conn:
        try:
            total = conn.execute("SELECT COUNT(*) n FROM submissions").fetchone()["n"]
            conn_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(submissions)").fetchall()}
            miss = {"total": total}
            for col in ("diet_description", "record_date", "meal_count",
                        "dietary_advice", "email"):
                if col in conn_cols:
                    miss[col + "_null"] = conn.execute(
                        f"SELECT COUNT(*) n FROM submissions WHERE {col} IS NULL "
                        f"OR TRIM(COALESCE({col},''))=''").fetchone()["n"]
            out["missing"]["diet"] = miss
            dup = conn.execute(
                "SELECT submission_id, COUNT(*) n FROM submissions "
                "WHERE submission_id IS NOT NULL AND submission_id<>'' "
                "GROUP BY submission_id HAVING n>1").fetchall()
            out["duplicates"]["diet"] = {"duplicate_groups": len(dup)}
            # 异常：meal_count 超范围 / sedentary 超范围
            outliers = []
            if "meal_count" in conn_cols:
                bad = conn.execute(
                    "SELECT COUNT(*) n FROM submissions WHERE meal_count IS NOT NULL "
                    "AND (meal_count<0 OR meal_count>10)").fetchone()["n"]
                if bad:
                    outliers.append({"field": "meal_count", "count": bad,
                                     "expected": "0-10"})
            out["outliers"]["diet"] = outliers
        except sqlite3.Error as e:                            # noqa: BLE001
            out["missing"]["diet"] = {"error": str(e)}
        finally:
            conn.close()
    else:
        out["missing"]["diet"] = {"error": "数据库不可读或不存在"}

    # ── 运动库 ────────────────────────────────────────────────────────
    conn = _ro(_d.EXERCISE_DB)
    if conn:
        try:
            total = conn.execute("SELECT COUNT(*) n FROM submissions").fetchone()["n"]
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(submissions)").fetchall()}
            miss = {"total": total}
            for col in ("walk_days", "walk_minutes", "sedentary_hours", "email"):
                if col in cols:
                    miss[col + "_null"] = conn.execute(
                        f"SELECT COUNT(*) n FROM submissions WHERE {col} IS NULL"
                    ).fetchone()["n"]
            out["missing"]["exercise"] = miss
            outliers = []
            for field, lo, hi in (("walk_days", 0, 7), ("walk_minutes", 0, 10080),
                                  ("sedentary_hours", 0, 24)):
                if field in cols:
                    bad = conn.execute(
                        f"SELECT COUNT(*) n FROM submissions WHERE {field} IS NOT NULL "
                        f"AND ({field}<{lo} OR {field}>{hi})").fetchone()["n"]
                    if bad:
                        outliers.append({"field": field, "count": bad,
                                         "expected": f"{lo}-{hi}"})
            out["outliers"]["exercise"] = outliers
            dup = conn.execute(
                "SELECT submission_id, COUNT(*) n FROM submissions "
                "WHERE submission_id IS NOT NULL AND submission_id<>'' "
                "GROUP BY submission_id HAVING n>1").fetchall()
            out["duplicates"]["exercise"] = {"duplicate_groups": len(dup)}
        except sqlite3.Error as e:                            # noqa: BLE001
            out["missing"]["exercise"] = {"error": str(e)}
        finally:
            conn.close()
    else:
        out["missing"]["exercise"] = {"error": "数据库不可读或不存在"}

    # 模拟数据残留（交给 seed/clear 工具的标记）
    out["mock_residue"] = _mock_residue()
    return out


def _count_all_null(conn, cols) -> int:
    score_cols = [c for c in SCORE_RANGES if c in cols]
    if not score_cols:
        return 0
    cond = " AND ".join(f"{c} IS NULL" for c in score_cols)
    try:
        return conn.execute(
            f"SELECT COUNT(*) n FROM submissions WHERE {cond}").fetchone()["n"]
    except sqlite3.Error:
        return 0


def _outliers(conn, cols, ranges) -> list:
    out = []
    for field, (lo, hi) in ranges.items():
        if field not in cols:
            continue
        try:
            n = conn.execute(
                f"SELECT COUNT(*) n FROM submissions WHERE {field} IS NOT NULL "
                f"AND ({field} < ? OR {field} > ?)", (lo, hi)).fetchone()["n"]
        except sqlite3.Error:
            continue
        if n:
            out.append({"field": field, "count": n, "expected": f"{lo}-{hi}"})
    return out


def _mock_residue() -> dict:
    """检查三个库中是否残留 MOCK- 标记（与 tools/clear_mock_data.py 同一判据）。"""
    res = {}
    for name, path in (("survey", _d.SURVEY_DB), ("diet", _d.DIET_DB),
                       ("exercise", _d.EXERCISE_DB)):
        conn = _ro(path)
        if conn is None:
            res[name] = None
            continue
        try:
            n = conn.execute(
                "SELECT COUNT(*) n FROM submissions "
                "WHERE student_id LIKE 'MOCK-%' OR submission_id LIKE 'MOCK-%'"
            ).fetchone()["n"]
            res[name] = int(n)
        except sqlite3.Error:
            res[name] = None
        finally:
            conn.close()
    res["clean"] = all(v in (0, None) for v in res.values() if isinstance(v, int))
    return res


# ══════════════════════════════════════════════════════════════════════════
# 4) 系统健康
# ══════════════════════════════════════════════════════════════════════════

def _probe(url: str, timeout: float = 3.0) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {"raw": body[:200]}
            return {"reachable": True, "http": resp.status,
                    "status": data.get("status", "?"),
                    "checks": data.get("checks", {}), "body_keys": sorted(data)[:8]}
    except Exception as e:                                    # noqa: BLE001
        return {"reachable": False, "error": str(e)[:200]}


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def _cfg(key, default=None):
    try:
        from lib_schedule import load_config
        return load_config().get(key, default)
    except Exception:                                        # noqa: BLE001
        return default


def system_health(timeout: float = 3.0) -> dict:
    """聚合各服务 /healthz + 端口监听 + DB 可读性。"""
    services = [
        ("survey_feedback", _cfg("services.survey_feedback.port", 8000), "/healthz"),
        ("diet_feedback", _cfg("services.diet_feedback.port", 8001), "/healthz"),
        ("diet_webhook", _cfg("services.diet_webhook.port", 9876), "/healthz"),
        ("survey_webhook", _cfg("services.survey_webhook.port", 9877), "/healthz"),
        ("admin_console", _cfg("services.admin_console.port", 9000), "/healthz"),
        ("data_dashboard", _cfg("services.data_dashboard.port", 8090), "/healthz"),
    ]
    rows, n_ok, n_fail = [], 0, 0
    for name, port, path in services:
        port = int(port or 0)
        listening = _port_listening(port) if port else False
        probe = _probe(f"http://127.0.0.1:{port}{path}", timeout) if listening else \
            {"reachable": False, "error": "port not listening"}
        ok = bool(listening and probe.get("reachable"))
        n_ok += 1 if ok else 0
        n_fail += 0 if ok else 1
        rows.append({"service": name, "port": port, "listening": listening,
                     "healthy": ok, "probe": probe})

    dbs = []
    for name, path in (("survey", _d.SURVEY_DB), ("diet", _d.DIET_DB),
                       ("exercise", _d.EXERCISE_DB)):
        conn = _ro(path)
        entry = {"name": name, "path": path, "exists": os.path.exists(path)}
        if conn is None:
            entry.update({"readable": False})
        else:
            try:
                conn.execute("SELECT 1").fetchone()
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                entry.update({"readable": True, "tables": tables})
            except sqlite3.Error as e:                        # noqa: BLE001
                entry.update({"readable": False, "error": str(e)})
            finally:
                conn.close()
        dbs.append(entry)

    return {
        "status": "ok" if n_fail == 0 else "degraded",
        "counts": {"services_ok": n_ok, "services_fail": n_fail,
                   "databases": len(dbs), "databases_readable":
                   len([d for d in dbs if d.get("readable")])},
        "services": rows,
        "databases": dbs,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ══════════════════════════════════════════════════════════════════════════
# 导出：统一把嵌套结构摊平成行（CSV 友好）
# ══════════════════════════════════════════════════════════════════════════

def queue_rows(dat: dict = None) -> list:
    dat = dat or queue_overview()
    rows = []
    for k, v in (dat.get("llm") or {}).items():
        if isinstance(v, (int, str, bool)) or v is None:
            rows.append({"scope": "llm", "key": k, "value": v})
    for scope, blk in (dat.get("email") or {}).items():
        for k, v in blk.items():
            rows.append({"scope": f"email.{scope}", "key": k, "value": v})
    for k, v in (dat.get("webhook") or {}).items():
        rows.append({"scope": "webhook", "key": k, "value": v})
    return rows


def adherence_rows(dat: dict = None) -> list:
    dat = dat or adherence_series()
    dates = dat.get("dates", [])
    rows = []
    for s in dat.get("students", []):
        row = {"student_id": s["student_id"], "active_days": s["active_days"],
               "rate": s["rate"]}
        for d, c in zip(dates, s.get("cells", [])):
            row[d] = c
        rows.append(row)
    return rows


def quality_rows(dat: dict = None) -> list:
    dat = dat or data_quality()
    rows = []
    for kind in ("missing", "outliers", "duplicates", "unmapped"):
        blk = dat.get(kind) or {}
        for scope, val in blk.items():
            if isinstance(val, dict):
                for k, v in val.items():
                    if isinstance(v, list):
                        rows.append({"kind": kind, "scope": scope, "key": k,
                                     "value": json.dumps(v, ensure_ascii=False)})
                    else:
                        rows.append({"kind": kind, "scope": scope, "key": k, "value": v})
            elif isinstance(val, list):
                for it in val:
                    rows.append({"kind": kind, "scope": "survey" if kind == "outliers"
                                 else scope, "key": it.get("field", ""),
                                 "value": it.get("count", "")})
    for k, v in (dat.get("mock_residue") or {}).items():
        rows.append({"kind": "mock_residue", "scope": "all", "key": k, "value": v})
    return rows


def health_rows(dat: dict = None) -> list:
    dat = dat or system_health()
    rows = []
    for s in dat.get("services", []):
        rows.append({"kind": "service", "name": s["service"], "port": s["port"],
                     "listening": s["listening"], "healthy": s["healthy"],
                     "detail": json.dumps(s.get("probe", {}), ensure_ascii=False)[:200]})
    for d in dat.get("databases", []):
        rows.append({"kind": "database", "name": d["name"], "port": "",
                     "listening": d.get("exists"), "healthy": d.get("readable"),
                     "detail": ",".join(d.get("tables", []) or [])[:200]})
    return rows
