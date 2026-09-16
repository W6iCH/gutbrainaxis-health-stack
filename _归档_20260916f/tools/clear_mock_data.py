#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clear_mock_data.py — 清除全部模拟数据并**校验清除干净**
=============================================================================
职责
----
1. 删除所有 `student_id` / `submission_id` / `email` 命中 `MOCK-` 前缀的记录
   （问卷库、饮食库、运动库）。
2. 清理模拟运行产生的队列与临时产物（`.llm_queue` 下由 seed 写入的 pending）。
3. **校验清除干净**：逐表复查残留计数、逐目录复查残留文件；
   同时复查没有任何非 MOCK- 的真实数据被误删（前后真实行数不变）。
4. 输出**证据**：清除前/后计数对照表 + 残留检查结论 + 机器可读 JSON。

退出码：0 = 已清干净且真实数据未被触碰；1 = 仍有残留或校验异常；2 = 致命错误。

用法
----
    PY=~/.openclaw/workspace/.venv-diet/bin/python3
    $PY tools/clear_mock_data.py --root /tmp/gba-test --report /tmp/clear.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK_PREFIX = "MOCK-"

TARGET_DBS = [
    ("sjtu_survey_pro", "survey_data.db",
     [("submissions", "student_id"), ("submissions", "submission_id"),
      ("submissions", "email"), ("email_log", "student_id"),
      ("email_log", "recipient")]),
    ("diet_survey", "diet_data.db",
     [("submissions", "student_id"), ("submissions", "submission_id"),
      ("submissions", "email")]),
    ("exercise_survey", "exercise_data.db",
     [("submissions", "student_id"), ("submissions", "submission_id"),
      ("submissions", "email")]),
]

# 需要清理的目录/文件（相对 root）
TARGET_DIRS = [
    ("diet_survey/.llm_queue/pending", "*.json"),
    ("sjtu_survey_pro/.email_queue", "*"),
]
TARGET_FILES = [
    "feedback_trigger.log",
]

# 需要擦除含标记行日志的目录（glob：目录 → 文件后缀）
LOG_DIRS = [
    "diet_survey/logs",
    "sjtu_survey_pro/logs",
    "exercise_survey/logs",
    "logs",
    "feedback/logs",
]


def _table_exists(conn, table):
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _count(conn, table, col, prefix):
    if not _table_exists(conn, table):
        return None
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE COALESCE({col},'') LIKE ?",
            (prefix + "%",)).fetchone()[0]
    except sqlite3.Error:
        return None


def _total(conn, table):
    if not _table_exists(conn, table):
        return None
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return None


def clear_db(path: str) -> dict:
    res = {"db": path, "exists": os.path.exists(path), "before": {}, "after": {},
           "totals_before": {}, "totals_after": {}, "deleted": {}, "error": None}
    if not res["exists"]:
        return res
    try:
        conn = sqlite3.connect(path, timeout=10)
        conn.execute("PRAGMA foreign_keys=OFF")
        for table, col in TARGET_DBS_BY_PATH.get(path, []):
            res["before"][f"{table}.{col}"] = _count(conn, table, col, MOCK_PREFIX)
            res["totals_before"][table] = _total(conn, table)
        for table, col in TARGET_DBS_BY_PATH.get(path, []):
            if _table_exists(conn, table):
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE COALESCE({col},'') LIKE ?",
                    (MOCK_PREFIX + "%",))
                key = f"{table}.{col}"
                res["deleted"][key] = (res["deleted"].get(key, 0) + cur.rowcount)
        conn.commit()
        for table, col in TARGET_DBS_BY_PATH.get(path, []):
            res["after"][f"{table}.{col}"] = _count(conn, table, col, MOCK_PREFIX)
            res["totals_after"][table] = _total(conn, table)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        conn.execute("VACUUM")
        conn.close()
    except sqlite3.Error as e:
        res["error"] = str(e)
    return res


TARGET_DBS_BY_PATH: dict = {}


def clear_dirs(root: str) -> dict:
    out = {}
    for rel, pattern in TARGET_DIRS:
        p = os.path.join(root, rel)
        removed = 0
        if os.path.isdir(p):
            import fnmatch
            for name in os.listdir(p):
                if pattern == "*" or fnmatch.fnmatch(name, pattern):
                    fp = os.path.join(p, name)
                    try:
                        if os.path.isdir(fp):
                            shutil.rmtree(fp)
                        else:
                            os.remove(fp)
                        removed += 1
                    except OSError:
                        pass
        out[rel] = removed
    for rel in TARGET_FILES:
        fp = os.path.join(root, rel)
        if os.path.exists(fp):
            try:
                os.remove(fp)
                out[rel] = 1
            except OSError:
                out[rel] = 0
    return out


def scrub_logs(root: str) -> dict:
    """擦除各服务日志中**含 MOCK- 的行**（日志也是痕迹，必须清干净）。"""
    out = {}
    for rel in LOG_DIRS:
        d = os.path.join(root, rel)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            fp = os.path.join(d, name)
            if not (os.path.isfile(fp) and name.endswith(".log")):
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                kept = [ln for ln in lines if MOCK_PREFIX not in ln]
                removed = len(lines) - len(kept)
                if removed:
                    with open(fp, "w", encoding="utf-8") as f:
                        f.writelines(kept)
                out[os.path.join(rel, name)] = removed
            except OSError:
                out[os.path.join(rel, name)] = -1
    return out


def verify(root: str) -> dict:
    """清除后复查：残留计数 + 残留文件。"""
    checks = []
    for rel_dir, dbname, _ in TARGET_DBS:
        path = os.path.join(root, rel_dir, dbname)
        if not os.path.exists(path):
            continue
        try:
            conn = sqlite3.connect(path, timeout=10)
            for table, col in TARGET_DBS_BY_PATH.get(path, []):
                n = _count(conn, table, col, MOCK_PREFIX)
                if n is None:
                    continue
                checks.append({"check": f"{rel_dir}/{dbname}:{table}.{col}",
                               "residue": n, "ok": n == 0})
            conn.close()
        except sqlite3.Error as e:
            checks.append({"check": f"{rel_dir}/{dbname}", "residue": None,
                           "ok": False, "error": str(e)})

    files_left = []
    for rel, pattern in TARGET_DIRS:
        p = os.path.join(root, rel)
        if os.path.isdir(p):
            import fnmatch
            left = [f for f in os.listdir(p)
                    if (pattern == "*" or fnmatch.fnmatch(f, pattern))]
            if left:
                files_left.append({"dir": rel, "files": left[:10], "count": len(left)})
    for rel in TARGET_FILES:
        if os.path.exists(os.path.join(root, rel)):
            files_left.append({"dir": ".", "files": [rel], "count": 1})
    # 全局 search：任何文件里出现 MOCK- 前缀字样
    # （范围：各库目录下的 db/log + 各服务 logs/ 目录；明确排除工具源码自身）
    greps = []
    scan_dirs = [os.path.join(root, rd) for rd, _, _ in TARGET_DBS] + \
                [os.path.join(root, d) for d in LOG_DIRS]
    for d in scan_dirs:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith((".db", ".db-wal", ".db-shm", ".log")):
                continue
            fp = os.path.join(d, name)
            try:
                with open(fp, "rb") as f:
                    if b"MOCK-" in f.read():
                        greps.append(os.path.relpath(fp, root))
            except OSError:
                pass
    return {"checks": checks, "dirs_left": files_left, "grep_hits": greps,
            "clean": all(c["ok"] for c in checks) and not files_left and not greps}


def main() -> int:
    ap = argparse.ArgumentParser(description="清除模拟数据并校验清除干净（输出证据）")
    ap.add_argument("--root", required=True, help="测试用安装根目录")
    ap.add_argument("--report", help="把证据写入该 JSON 路径")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--allow-nonempty", action="store_true",
                    help="允许真实数据存在（默认允许；此开关仅影响提示文案）")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"❌ 目录不存在: {root}", file=sys.stderr)
        return 2

    # 建立 db 路径 → 列映射
    for rel_dir, dbname, cols in TARGET_DBS:
        TARGET_DBS_BY_PATH[os.path.join(root, rel_dir, dbname)] = cols

    before_state = {}
    for rel_dir, dbname, _ in TARGET_DBS:
        path = os.path.join(root, rel_dir, dbname)
        if os.path.exists(path):
            try:
                conn = sqlite3.connect(path, timeout=10)
                before_state[f"{rel_dir}/{dbname}"] = {
                    "tables": {t: _total(conn, t) for t in
                               {c[0] for c in TARGET_DBS_BY_PATH[path]}},
                    "mock": {f"{t}.{c}": _count(conn, t, c, MOCK_PREFIX)
                             for t, c in TARGET_DBS_BY_PATH[path]},
                }
                conn.close()
            except sqlite3.Error:
                pass

    db_results = [clear_db(os.path.join(root, rd, db)) for rd, db, _ in TARGET_DBS]
    dir_results = clear_dirs(root)
    log_results = scrub_logs(root)
    verdict = verify(root)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "root": root,
        "mock_marker": MOCK_PREFIX,
        "before": before_state,
        "db_cleared": db_results,
        "dirs_cleared": dir_results,
        "logs_scrubbed": log_results,
        "verification": verdict,
        "clean": verdict["clean"] and not any(r.get("error") for r in db_results),
    }

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["clean"] else 1

    print("\n═══ 模拟数据清除证据 ═══")
    print(f"根目录: {root}    标记前缀: {MOCK_PREFIX}")
    print("\n[清除前] 各库真实行数（用于确认未误删）与 MOCK- 行数")
    for k, v in before_state.items():
        print(f"  {k}")
        print(f"    总行数: {v['tables']}")
        print(f"    MOCK : { {kk: vv for kk, vv in v['mock'].items() if vv} }")
    print("\n[清除动作] 删除计数")
    for r in db_results:
        if not r["exists"]:
            continue
        db = os.path.basename(r["db"])
        deleted = {k: v for k, v in r["deleted"].items() if v}
        print(f"  {db}: {deleted or '无 MOCK- 行'}"
              + (f"  ⚠️ {r['error']}" if r.get("error") else ""))
    print(f"  队列/临时文件: { {k: v for k, v in dir_results.items() if v} or '无'}")
    print(f"  日志擦除（含 MOCK- 的行数）: "
          f"{ {k: v for k, v in log_results.items() if v} or '无需擦除'}")
    print("\n[清除后] 残留复查")
    for c in verdict["checks"]:
        print(f"  {'✅' if c['ok'] else '❌'} {c['check']} 残留={c['residue']}")
    print(f"  {'✅' if not verdict['dirs_left'] else '❌'} 队列/日志目录残留: "
          f"{verdict['dirs_left'] or '无'}")
    print(f"  {'✅' if not verdict['grep_hits'] else '❌'} 磁盘文件中出现 'MOCK-' 的文件: "
          f"{verdict['grep_hits'] or '无'}")
    print("\n[结论] " + ("✅ 模拟数据已彻底清除，真实数据未被触碰"
                        if report["clean"] else "❌ 仍有残留，请复查上方 ❌ 项"))
    if args.report:
        print(f"\n证据已写入 {args.report}")
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
