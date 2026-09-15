#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_microbiome.py — 二期菌群数据导入接口
=============================================================================
投递方式（两种都支持）
----------------------
A. 监控目录（推荐）：把 CSV 放进 `MICROBIOME_IMPORT_DIR`，由
   `research-microbiome-import.timer` 每小时调用本脚本扫描导入。
   文件名约定：`<omics_type>_<batch>_<yyyymmdd>.csv`
      例：metagenome_batch01_20261001.csv / scfa_batch01_20261001.csv
      例：inflammation_batch02_20261008.csv
   未按约定命名的文件按表头自动判别组学类型（`omics_type` 列或文件名关键字）。

B. 指定文件：`python3 import_microbiome.py --file path/to/x.csv [--omics-type scfa]`

CSV 格式
--------
**长表（推荐）**
    sample_id,student_id,timepoint,sample_date,sample_type,omics_type,feature,value,unit,method,qc_flag
    S001,524072910079,T2,2026-10-01,stool,metagenome,Faecalibacterium_prausnitzii,12.4,rel_abundance,shotgun,pass

**宽表**（feature 为列名）
    sample_id,student_id,timepoint,sample_type,omics_type,Shannon,Chao1,SCFA_acetate,...
额外列自动进 `extras`（`microbiome.allow_extra_columns`）。

幂等与留痕
----------
* 以文件 sha256 去重：同一文件重复投递不会重复入库（status=ok 即跳过）。
* 以 (sample_id, omics_type) / (sample_id, feature, method) 为业务主键 upsert。
* 每次导入写入 `omics_import_log`（文件名/sha256/行数/状态/消息）。

导入后原文件移动到 `<import_dir>/processed/`；失败移动到 `<import_dir>/failed/`。

零 PII：`student_id`（学工号）仅作连接键；不导出、不落匿名化以外的产物。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import microbiome_database as MDB                     # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(BASE_DIR), "..", "tools"))
try:
    import appconfig as AC                            # noqa: E402
except Exception:                                     # noqa: BLE001
    AC = None

DEFAULT_IMPORT_DIR = os.environ.get(
    "MICROBIOME_IMPORT_DIR",
    os.path.join(os.environ.get("APP_BASE", "/opt/gutbrainaxis"),
                 "microbiome", "import"))

# 文件名关键字 → 组学类型
NAME_HINTS = [
    ("metagenome", "metagenome"), ("meta", "metagenome"), ("mgx", "metagenome"),
    ("scfa", "scfa"), ("shortchain", "scfa"),
    ("inflam", "inflammation"), ("cyto", "inflammation"),
    ("meta_metabolome", "metabolome"), ("metabolome", "metabolome"),
]


def _cfg():
    if AC is None:
        return {}
    cfg, _ = AC.try_load()
    return cfg or {}


def guess_omics_type(filename: str, header: list, explicit: str = None) -> str:
    if explicit:
        return explicit
    for col in ("omics_type", "OMICS_TYPE", "组学类型"):
        if col in header:
            return "from_column"
    low = os.path.basename(filename).lower()
    for key, val in NAME_HINTS:
        if key in low:
            return val
    return "other"


def _num(v):
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def import_csv(path: str, omics_type: str = None, cfg: dict = None,
               dry_run: bool = False) -> dict:
    """导入单个 CSV。返回结果摘要 dict。"""
    cfg = cfg if cfg is not None else _cfg()
    allow_extra = bool(cfg.get("microbiome.allow_extra_columns", True))
    sha = MDB.file_sha256(path)

    conn = MDB.get_conn()
    result = {"file": os.path.basename(path), "sha256": sha, "rows_ok": 0,
              "rows_skipped": 0, "status": "ok", "message": "", "samples": 0,
              "measurements": 0, "omics_type": omics_type or "?"}
    try:
        if MDB.already_imported(conn, sha):
            result.update(status="skipped", message="同 sha256 文件已成功导入过")
            return result

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            sample = f.read(64 * 1024)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(f, dialect=dialect)
            header = [h.strip() for h in (reader.fieldnames or [])]
            if not header:
                result.update(status="failed", message="空文件或无表头")
                return result
            lower = {h.lower(): h for h in header}
            if "sample_id" not in lower:
                result.update(status="failed", message="缺少必填列 sample_id")
                return result

            ot = guess_omics_type(path, header, omics_type)
            result["omics_type"] = ot

            known_sample = {c.lower() for c in MDB.SAMPLE_FIELDS}
            known_meas = {c.lower() for c in MDB.MEASUREMENT_FIELDS}
            long_format = ("feature" in lower and "value" in lower)
            feature_cols = []
            if not long_format:
                feature_cols = [h for h in header
                                if h.lower() not in known_sample
                                and h.lower() not in known_meas]

            for raw_row in reader:
                row = {(k or "").strip(): (v.strip() if isinstance(v, str) else v)
                       for k, v in raw_row.items()}
                sid = row.get(lower["sample_id"]) or ""
                if not sid:
                    result["rows_skipped"] += 1
                    continue
                row_ot = ot
                if row_ot == "from_column":
                    row_ot = (row.get(lower.get("omics_type", "omics_type")) or "other").strip()
                    row_ot = row_ot if row_ot in MDB.OMICS_TYPES else "other"

                extras = {}
                if allow_extra:
                    for h in header:
                        if h.lower() not in known_sample and h.lower() not in known_meas \
                                and h not in feature_cols:
                            extras[h] = row.get(h)

                sample_rec = {
                    "sample_id": sid,
                    "student_id": row.get(lower.get("student_id")) or None,
                    "sid_hash": row.get(lower.get("sid_hash")) or None,
                    "timepoint": row.get(lower.get("timepoint")) or None,
                    "sample_date": row.get(lower.get("sample_date")) or None,
                    "sample_type": (row.get(lower.get("sample_type")) or None),
                    "omics_type": row_ot,
                    "batch": row.get(lower.get("batch")) or None,
                    "platform": row.get(lower.get("platform")) or None,
                    "notes": row.get(lower.get("notes")) or None,
                    "_extras": extras,
                }
                if not dry_run:
                    MDB.upsert_sample(conn, sample_rec, os.path.basename(path))
                result["samples"] += 1

                if long_format:
                    feature = row.get(lower["feature"]) or ""
                    val = _num(row.get(lower["value"]))
                    if not feature:
                        result["rows_skipped"] += 1
                        continue
                    meas = {
                        "sample_id": sid, "feature": feature,
                        "feature_type": row.get(lower.get("feature_type")) or None,
                        "value": val,
                        "unit": row.get(lower.get("unit")) or None,
                        "method": row.get(lower.get("method")) or None,
                        "qc_flag": row.get(lower.get("qc_flag")) or None,
                    }
                    if not dry_run:
                        MDB.upsert_measurement(conn, meas)
                    result["measurements"] += 1
                    result["rows_ok"] += 1
                else:
                    for fc in feature_cols:
                        val = _num(row.get(fc))
                        if val is None:
                            continue
                        meas = {"sample_id": sid, "feature": fc, "feature_type": "wide",
                                "value": val,
                                "unit": row.get(lower.get("unit")) or None,
                                "method": row.get(lower.get("method")) or None,
                                "qc_flag": None}
                        if not dry_run:
                            MDB.upsert_measurement(conn, meas)
                        result["measurements"] += 1
                        result["rows_ok"] += 1

        if not dry_run:
            MDB.log_import(conn, os.path.basename(path), sha, result["omics_type"],
                           result["rows_ok"], result["rows_skipped"], "ok",
                           f"samples={result['samples']} meas={result['measurements']}")
            conn.commit()
        return result
    except Exception as e:                            # noqa: BLE001
        result.update(status="failed", message=f"{type(e).__name__}: {e}")
        try:
            MDB.log_import(conn, os.path.basename(path), sha, result["omics_type"],
                           result["rows_ok"], result["rows_skipped"], "failed",
                           result["message"])
            conn.commit()
        except Exception:                             # noqa: BLE001
            pass
        return result
    finally:
        conn.close()


def scan_dir(import_dir: str, omics_type: str = None, dry_run: bool = False) -> list:
    """扫描投递目录里的 CSV 并导入；导入后归档到 processed/ 或 failed/。"""
    cfg = _cfg()
    out = []
    if not os.path.isdir(import_dir):
        return out
    files = sorted(f for f in os.listdir(import_dir)
                   if f.lower().endswith(".csv") and
                   os.path.isfile(os.path.join(import_dir, f)))
    for name in files:
        path = os.path.join(import_dir, name)
        res = import_csv(path, omics_type, cfg, dry_run)
        if not dry_run:
            dest_dir = os.path.join(import_dir,
                                    "processed" if res["status"] in ("ok", "skipped")
                                    else "failed")
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir,
                                datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + name)
            try:
                shutil.move(path, dest)
                res["archived_to"] = dest
            except OSError as e:
                res["archived_to"] = f"移动失败: {e}"
        out.append(res)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="二期菌群数据导入（宏基因组/SCFA/炎症因子）")
    ap.add_argument("--dir", default=None, help="投递目录（默认取配置 microbiome.import_dir）")
    ap.add_argument("--file", default=None, help="只导入单个 CSV")
    ap.add_argument("--omics-type", default=None,
                    help=f"指定组学类型 {list(MDB.OMICS_TYPES)}")
    ap.add_argument("--init-db", action="store_true", help="仅初始化 schema 后退出")
    ap.add_argument("--dry-run", action="store_true", help="只解析不入库、不移动文件")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.init_db:
        MDB.init_db()
        print(f"✅ microbiome schema 已就绪: {MDB.DB_PATH}")
        return 0

    cfg = _cfg()
    import_dir = args.dir or cfg.get("microbiome.import_dir") or DEFAULT_IMPORT_DIR
    MDB.init_db()

    if args.file:
        results = [import_csv(args.file, args.omics_type, cfg, args.dry_run)]
    else:
        results = scan_dir(import_dir, args.omics_type, args.dry_run)

    if args.json:
        print(json.dumps({"import_dir": import_dir, "results": results},
                         ensure_ascii=False, indent=2))
    else:
        if not results:
            print(f"ℹ️  {import_dir} 下没有待导入的 CSV")
        for r in results:
            icon = {"ok": "✅", "skipped": "⏭️", "failed": "❌"}.get(r["status"], "?")
            print(f"{icon} {r['file']}  [{r['omics_type']}]  样本 {r['samples']} / "
                  f"指标 {r['measurements']} / 跳过 {r['rows_skipped']}"
                  + (f"  — {r['message']}" if r["message"] else ""))
    return 0 if all(r["status"] in ("ok", "skipped") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
