#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
microbiome_database.py — 二期菌群数据存储（宏基因组 / SCFA / 炎症因子）
=============================================================================
设计目标
--------
* **预留存储**：二期新增的组学数据（宏基因组、短链脂肪酸、炎症因子）有独立
  库文件与稳定 schema，不与一期问卷/饮食库耦合。
* **导入接口**：外部只往 `MICROBIOME_IMPORT_DIR` 投 CSV（长表或宽表），
  `import_microbiome.py` 负责校验、幂等入库、留痕（import_log）。
* **不丢数据**：未在规范列内的额外列进 `extras` JSON（`allow_extra_columns`）。
* **可追溯**：每次导入记录文件名 + sha256 + 行数 + 状态；重复文件不重复入库。

表结构（详见 docs/数据模型与存储.md）
-------------------------------------
1. `omics_samples`      —— 样本级：一条样本一行（学生 × 时点 × 组学类型 × 样本类型）
2. `omics_measurements` —— 指标级：长表，一条指标一行
3. `omics_import_log`   —— 导入留痕

保留策略：数据库文件与一期一致（备份保留 14 份）；原始 CSV 导入后归档到
`<import_dir>/processed/`，保留 24 个月（由 backup.retention 与人工策略共同约束）。
"""

import hashlib
import os
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MICROBIOME_DB", os.path.join(BASE_DIR, "microbiome_data.db"))

# 规范列（长表）
SAMPLE_FIELDS = ["sample_id", "student_id", "sid_hash", "timepoint", "sample_date",
                 "sample_type", "omics_type", "batch", "platform", "notes"]
MEASUREMENT_FIELDS = ["sample_id", "feature", "feature_type", "value", "unit",
                      "method", "qc_flag"]

SAMPLE_TYPES = ("stool", "saliva", "blood", "urine", "other")
OMICS_TYPES = ("metagenome", "scfa", "inflammation", "metabolome", "transcriptome", "other")


def get_conn(path: str = None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str = None):
    """创建空 schema（幂等）。"""
    conn = get_conn(path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS omics_samples (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_id       TEXT NOT NULL,
                student_id      TEXT,
                sid_hash        TEXT,
                timepoint       TEXT,
                sample_date     TEXT,
                sample_type     TEXT,
                omics_type      TEXT NOT NULL,
                batch           TEXT,
                platform        TEXT,
                notes           TEXT,
                extras          TEXT,
                source_file     TEXT,
                created_at      TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE (sample_id, omics_type)
            );

            CREATE TABLE IF NOT EXISTS omics_measurements (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_id       TEXT NOT NULL,
                feature         TEXT NOT NULL,
                feature_type    TEXT,
                value           REAL,
                unit            TEXT,
                method          TEXT,
                qc_flag         TEXT,
                extras          TEXT,
                created_at      TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE (sample_id, feature, method)
            );

            CREATE TABLE IF NOT EXISTS omics_import_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                filename        TEXT NOT NULL,
                sha256          TEXT NOT NULL,
                omics_type      TEXT,
                rows_ok         INTEGER DEFAULT 0,
                rows_skipped    INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'pending',
                message         TEXT,
                imported_at     TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE (sha256)
            );

            CREATE INDEX IF NOT EXISTS idx_omics_samples_student
                ON omics_samples(student_id);
            CREATE INDEX IF NOT EXISTS idx_omics_samples_omics
                ON omics_samples(omics_type, timepoint);
            CREATE INDEX IF NOT EXISTS idx_omics_meas_sample
                ON omics_measurements(sample_id);
            CREATE INDEX IF NOT EXISTS idx_omics_meas_feature
                ON omics_measurements(feature);
        """)
        conn.commit()
    finally:
        conn.close()


def upsert_sample(conn, row: dict, source_file: str = "") -> int:
    """按 (sample_id, omics_type) 幂等写样本；冲突时补全非空字段。"""
    import json
    cols = ["sample_id", "student_id", "sid_hash", "timepoint", "sample_date",
            "sample_type", "omics_type", "batch", "platform", "notes", "extras",
            "source_file"]
    vals = {c: row.get(c) for c in cols}
    vals["omics_type"] = vals.get("omics_type") or "other"
    vals["extras"] = json.dumps(row.get("_extras") or {}, ensure_ascii=False) \
        if row.get("_extras") else None
    vals["source_file"] = source_file
    conn.execute(f"""
        INSERT INTO omics_samples ({','.join(cols)})
        VALUES ({','.join('?' for _ in cols)})
        ON CONFLICT(sample_id, omics_type) DO UPDATE SET
            student_id  = COALESCE(excluded.student_id,  omics_samples.student_id),
            sid_hash    = COALESCE(excluded.sid_hash,    omics_samples.sid_hash),
            timepoint   = COALESCE(excluded.timepoint,   omics_samples.timepoint),
            sample_date = COALESCE(excluded.sample_date, omics_samples.sample_date),
            sample_type = COALESCE(excluded.sample_type, omics_samples.sample_type),
            batch       = COALESCE(excluded.batch,       omics_samples.batch),
            platform    = COALESCE(excluded.platform,    omics_samples.platform),
            notes       = COALESCE(excluded.notes,       omics_samples.notes),
            extras      = COALESCE(excluded.extras,      omics_samples.extras)
    """, [vals[c] for c in cols])
    r = conn.execute("SELECT id FROM omics_samples WHERE sample_id=? AND omics_type=?",
                     (vals["sample_id"], vals["omics_type"])).fetchone()
    return r["id"] if r else None


def upsert_measurement(conn, m: dict) -> bool:
    """按 (sample_id, feature, method) 幂等写指标。"""
    import json
    cols = ["sample_id", "feature", "feature_type", "value", "unit", "method",
            "qc_flag", "extras"]
    vals = {c: m.get(c) for c in cols}
    vals["extras"] = json.dumps(m.get("_extras") or {}, ensure_ascii=False) \
        if m.get("_extras") else None
    try:
        conn.execute(f"""
            INSERT INTO omics_measurements ({','.join(cols)})
            VALUES ({','.join('?' for _ in cols)})
            ON CONFLICT(sample_id, feature, method) DO UPDATE SET
                value = excluded.value, unit = excluded.unit,
                feature_type = COALESCE(excluded.feature_type,
                                        omics_measurements.feature_type),
                qc_flag = COALESCE(excluded.qc_flag, omics_measurements.qc_flag),
                extras = COALESCE(excluded.extras, omics_measurements.extras)
        """, [vals[c] for c in cols])
        return True
    except sqlite3.Error:
        return False


def log_import(conn, filename, sha256, omics_type, rows_ok, rows_skipped,
               status, message=""):
    conn.execute("""
        INSERT INTO omics_import_log (filename, sha256, omics_type, rows_ok,
                                      rows_skipped, status, message)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(sha256) DO UPDATE SET
            rows_ok=excluded.rows_ok, rows_skipped=excluded.rows_skipped,
            status=excluded.status, message=excluded.message,
            imported_at=datetime('now','localtime')
    """, (filename, sha256, omics_type, rows_ok, rows_skipped, status, message))


def already_imported(conn, sha256: str) -> bool:
    r = conn.execute("SELECT status FROM omics_import_log WHERE sha256=? AND status='ok'",
                     (sha256,)).fetchone()
    return r is not None


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── 汇总/导出辅助 ─────────────────────────────────────────────────────────

def summary(path: str = None) -> dict:
    conn = get_conn(path)
    try:
        out = {"samples": 0, "measurements": 0, "imports": 0, "by_omics": {},
               "by_type": {}}
        for tbl, key in (("omics_samples", "samples"),
                         ("omics_measurements", "measurements"),
                         ("omics_import_log", "imports")):
            try:
                out[key] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except sqlite3.Error:
                pass
        try:
            for r in conn.execute("SELECT omics_type, COUNT(*) n FROM omics_samples "
                                  "GROUP BY omics_type"):
                out["by_omics"][r["omics_type"]] = r["n"]
            for r in conn.execute("SELECT sample_type, COUNT(*) n FROM omics_samples "
                                  "GROUP BY sample_type"):
                out["by_type"][r["sample_type"] or "unknown"] = r["n"]
        except sqlite3.Error:
            pass
        return out
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"✅ microbiome schema 已初始化: {DB_PATH}")
    print(summary())
