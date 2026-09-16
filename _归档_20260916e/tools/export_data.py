#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_data.py — 一键数据导出（CSV / JSON）
=============================================================================
把三个业务库 + 菌群库导出为 CSV 与 JSON，供研究者离线分析。

要点
----
* **零 PII 可开关**：`--anonymize` 时把 `学工号(student_id)` 换成稳定匿名号
  `P###` + `sid_hash`（HMAC-SHA256，盐来自 `EXPORT_SALT` 或默认项目盐）；
  邮箱、姓名一律哈希/剔除。默认导出**原样**（研究者内部使用），
  但**只要输出目录不是本机受限目录就应加 `--anonymize`**。
* **一键全量**：`--all` 导出全部表；也可 `--table submissions` 指定。
* **可复现**：导出清单（manifest）记录每张表的行数、列名、sha256、导出时间。
* **不破坏源库**：全程只读（`mode=ro`）。

用法
----
    PY=~/.openclaw/workspace/.venv-diet/bin/python3
    $PY tools/export_data.py --out /tmp/export --all --anonymize
    $PY tools/export_data.py --out /tmp/export --table submissions --format csv
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import appconfig as AC                                   # noqa: E402

DEFAULT_SALT = os.environ.get("EXPORT_SALT", "sjtu-brain-gut-2026")

# 需要匿名化的列（命中即替换）
PII_COLUMNS = {"student_id", "email", "name", "recipient", "ip_address"}
# 需要整列剔除的列
DROP_COLUMNS = {"raw_data", "analysis", "responses"}

DB_KEYS = [("survey_db", "问卷", "sjtu_survey_pro"),
           ("diet_db", "饮食", "diet_survey"),
           ("exercise_db", "运动", "exercise_survey"),
           ("microbiome_db", "菌群", "microbiome")]


def _anon(value, salt: str) -> str:
    if value in (None, ""):
        return value
    h = hmac.new(salt.encode(), str(value).encode(), hashlib.sha256).hexdigest()
    return "H-" + h[:12]


def _pid(value, salt: str) -> str:
    if value in (None, ""):
        return value
    h = hmac.new(salt.encode(), ("pid:" + str(value)).encode(), hashlib.sha256).hexdigest()
    return "P" + str(int(h[:8], 16) % 100000).zfill(3)


def list_tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]


def export_table(conn, table: str, out_dir: str, fmt: str, anonymize: bool,
                 salt: str, drop_heavy: bool = True) -> dict:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if not cols:
        return {"table": table, "rows": 0, "skipped": "无列定义"}
    select_cols = [c for c in cols
                   if not (drop_heavy and c in DROP_COLUMNS)]
    if anonymize:
        select_cols = [c for c in select_cols if c not in {"name", "email", "ip_address"}]
    sql = f"SELECT {', '.join(select_cols)} FROM {table}"
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as e:
        return {"table": table, "rows": 0, "error": str(e)}

    def conv(col, val):
        if anonymize:
            if col == "student_id":
                return _pid(val, salt)
            if col in PII_COLUMNS:
                return _anon(val, salt)
        if col == "sid_hash" and anonymize:
            return _anon(val, salt)
        return val

    out_rows = [[conv(c, r[c]) for c in select_cols] for r in rows]

    os.makedirs(out_dir, exist_ok=True)
    paths = []
    if fmt in ("csv", "both"):
        p = os.path.join(out_dir, f"{table}.csv")
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(select_cols)
            w.writerows(out_rows)
        paths.append(p)
    if fmt in ("json", "both"):
        p = os.path.join(out_dir, f"{table}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump([dict(zip(select_cols, r)) for r in out_rows], f,
                      ensure_ascii=False, indent=2, default=str)
        paths.append(p)

    digest = hashlib.sha256(
        json.dumps(out_rows, ensure_ascii=False, default=str).encode()).hexdigest()[:16]
    return {"table": table, "rows": len(out_rows), "columns": select_cols,
            "sha256_16": digest, "files": paths,
            "dropped_heavy_columns": [c for c in cols if c not in select_cols]
            if drop_heavy or anonymize else []}


def main() -> int:
    ap = argparse.ArgumentParser(description="一键数据导出（CSV/JSON，支持匿名化）")
    ap.add_argument("--out", default="./export", help="输出目录")
    ap.add_argument("--all", action="store_true", help="导出全部库的全部表")
    ap.add_argument("--table", action="append", default=[], help="指定表名（可多次）")
    ap.add_argument("--format", choices=["csv", "json", "both"], default="both")
    ap.add_argument("--anonymize", action="store_true",
                    help="匿名化：student_id→P###，姓名/邮箱/IP→HMAC 哈希")
    ap.add_argument("--salt", default=DEFAULT_SALT, help="匿名化盐（默认取 EXPORT_SALT）")
    ap.add_argument("--keep-heavy", action="store_true",
                    help="保留 raw_data/analysis/responses 大字段（默认剔除）")
    ap.add_argument("--config", default=AC.DEFAULT_CONFIG)
    args = ap.parse_args()

    cfg, _ = AC.try_load(args.config)
    out_dir = os.path.abspath(args.out)
    manifest = {"generated_at": datetime.now().isoformat(timespec="seconds"),
                "out_dir": out_dir, "format": args.format,
                "anonymized": args.anonymize, "databases": []}

    total_rows, total_files = 0, []
    for key, label, subdir in DB_KEYS:
        path = (cfg or {}).get(f"database.{key}")
        if not path or not os.path.exists(path):
            manifest["databases"].append({"db": key, "label": label,
                                          "path": path, "exists": False})
            continue
        entry = {"db": key, "label": label, "path": path, "exists": True, "tables": []}
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            tables = list_tables(conn)
            want = args.table or (tables if args.all else [])
            if not want and not args.all:
                # 默认导出 submissions / email_log
                want = [t for t in tables if t in ("submissions", "email_log",
                                                   "omics_samples", "omics_measurements",
                                                   "omics_import_log")]
            for t in want:
                if t not in tables:
                    entry["tables"].append({"table": t, "rows": 0,
                                            "error": "表不存在"})
                    continue
                sub_out = os.path.join(out_dir, subdir)
                res = export_table(conn, t, sub_out, args.format, args.anonymize,
                                   args.salt, drop_heavy=not args.keep_heavy)
                entry["tables"].append(res)
                total_rows += res.get("rows", 0)
                total_files += res.get("files", [])
            conn.close()
        except sqlite3.Error as e:
            entry["error"] = str(e)
        manifest["databases"].append(entry)

    manifest["total_rows"] = total_rows
    manifest["total_files"] = len(total_files)
    os.makedirs(out_dir, exist_ok=True)
    mpath = os.path.join(out_dir, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"✅ 导出完成 → {out_dir}")
    print(f"   库 {len(manifest['databases'])} 个 ｜ 表行数合计 {total_rows} ｜ 文件 {len(total_files)}")
    print(f"   匿名化: {'是' if args.anonymize else '否'}")
    for db in manifest["databases"]:
        if not db.get("exists"):
            print(f"   ⏭️  {db['label']}: 库不存在（{db.get('path')}）")
            continue
        for t in db["tables"]:
            print(f"   ✅ {db['label']}/{t['table']}: {t.get('rows', 0)} 行"
                  + (f"  ⚠️ {t['error']}" if t.get("error") else ""))
    print(f"   清单: {mpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
