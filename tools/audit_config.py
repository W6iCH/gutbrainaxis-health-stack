#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_config.py — 部署个性化项「穷尽审计」扫描器
=============================================================================
问题
----
用户要求：**凡因部署而异的值（路径 / 端口 / 域名 / Token / 邮箱 / 阈值 / 切点 /
日期 / 名册 / 并发 / 保留策略 / 日志级别 / 静默时段…）都必须可配**，
并要一份「审计了多少处、外提了多少处、剩余不可配的有哪些及原因」的小结。

做法
----
对 `services/`、`tools/`、`scripts/`、`systemd/`、`nginx/`、`install.sh`
逐行扫描**部署敏感字面量**，并逐条判定它的「外提状态」：

  * `externalized`     代码部分出现 `os.environ.get(...)` / `_cfg(...)` /
                       `APP_BASE` 等配置读取 → **已外提**（字面量只是回退默认）
  * `config_reference` 值是 `"${VAR}"` 形式 → 运行期从 secrets.env 解析
  * `hardcoded`        既无配置读取、也不是 `${VAR}` → **待外提**

`hardcoded` 再按 `ALLOW_HARDCODED` 白名单（安装器/模板/扫描器自身规则）标记为
`allowed` 并给出理由，避免把「本就不该可配的」误判成缺陷。

约定：只看**代码部分**（丢掉行尾 `#` / `//` 注释），避免文档性字面量误报；
`25/465` 等 IANA 标准端口由 `smtp.port` 驱动，不列为部署个性化项。

用法::

    python3 tools/audit_config.py                # 人类可读小结
    python3 tools/audit_config.py --json         # 机器可读
    python3 tools/audit_config.py --md OUT.md    # 生成审计小结（Markdown）
    python3 tools/audit_config.py --strict       # 存在待外提项即退出 1（CI）

退出码：0=无待外提项（或未开 --strict）；1=存在待外提项；2=无法运行。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)

SCAN_DIRS = ["services", "tools", "scripts", "systemd", "nginx"]
SCAN_FILES = ["install.sh"]
EXCLUDE_PARTS = ("_归档_", "__pycache__", ".git", "node_modules")

# ── 部署敏感字面量模式 ─────────────────────────────────────────────────────
# 只收「会因部署而异」的：自有服务端口、服务器/仓库绝对路径、问卷平台与 LLM 主机、
# 真实邮箱、32 位凭证。**不收** 25/465 等 IANA 标准端口（已由 smtp.port 驱动）。
PATTERNS = [
    ("abs_path", re.compile(r"(?<![\w.])/(?:opt|srv)/[\w./-]+")),
    ("url", re.compile(r"https?://(?!localhost|127\.0\.0\.1)"
                       r"[A-Za-z0-9.-]+[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.)[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("port_literal", re.compile(r'(?<=[=: ("\'])(?:8000|8001|9000|8090|9876|9877)\b')),
    ("token_like", re.compile(r"\b[0-9a-f]{32}\b|\bsk-[A-Za-z0-9_-]{16,}\b")),
]

# ── 「配置读取」的迹象（代码部分出现即视为已外提）───────────────────────────
CONFIG_HINTS = re.compile(
    r"os\.environ|getenv|_cfg\(|\.get\(|APP_BASE|INSTALL_ROOT|ENV_DIR|"
    r"\$\{[A-Z_]|\$[A-Z_]{2,}|to_env|ENV_MAP|load_config|CFG\b", re.I)

# ── 允许保留的硬编码（非部署个性化项）——(路径子串, 理由) ────────────────────
ALLOW_HARDCODED = [
    ("install.sh", "安装器：这里就是定义部署默认值的地方（--prefix 可覆盖）"),
    ("systemd/", "systemd 单元模板：__APP_BASE__/__ENV_FILE__ 由 install.sh 替换"),
    ("nginx/", "nginx 模板：__DOMAIN__ 等由 install.sh 从 app.yaml 渲染"),
    ("scan_sensitive.py", "扫描器自身的规则词表（示例域名/端口白名单）"),
    ("gen_config_doc.py", "配置文档生成器：表头/示例文本"),
    ("gen_time_config_doc.py", "时间项文档生成器：示例文本"),
    ("audit_config.py", "本审计器自身的模式定义"),
    ("verify_scoring_parity.py", "核对器：权威库不存在时的路径回退"),
    ("migrate.py", "迁移器：历史版本常量"),
    ("static/app.js", "控制台前端：输入框占位符 placeholder（非生效值）"),
    ("port_manager.py", "控制台端口管理器：默认端口表（可控制台改写）"),
    ("service_manager.py", "控制台服务清单：默认端口表（可控制台改写）"),
    ("config_manager.py", "控制台环境变量默认值表（可控制台改写）"),
    ("run_all.sh", "开发用启动脚本：端口已在调用处可通过参数/环境变量覆盖"),
]


def _iter_files():
    for d in SCAN_DIRS:
        base = os.path.join(PKG, d)
        if not os.path.isdir(base):
            continue
        for dp, dn, fn in os.walk(base):
            if any(p in dp for p in EXCLUDE_PARTS):
                continue
            for f in sorted(fn):
                if f.endswith((".py", ".sh", ".service", ".timer", ".conf",
                               ".json", ".js", ".html", ".css")):
                    yield os.path.join(dp, f)
    for f in SCAN_FILES:
        p = os.path.join(PKG, f)
        if os.path.isfile(p):
            yield p


def _rel(p):
    return os.path.relpath(p, PKG)


def _allow_reason(relpath):
    for key, reason in ALLOW_HARDCODED:
        if key in relpath:
            return reason
    return None


def scan() -> dict:
    hits = []
    files = list(_iter_files())
    for path in files:
        rel = _rel(path)
        in_doc = False
        try:
            lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if not s or s.startswith(("#", "//", "*", "<!--")):
                continue
            # 跳过模块/函数文档字符串（文档描述，不是生效值）
            if in_doc:
                if s.count('"""') % 2 == 1 or s.count("'''") % 2 == 1:
                    in_doc = False
                continue
            if s.startswith('"""') and s.count('"""') % 2 == 1:
                in_doc = True
                continue
            if s.startswith("'''") and s.count("'''") % 2 == 1:
                in_doc = True
                continue
            # 只看代码部分：丢掉行尾注释，避免文档性字面量误报
            code = line.split("#", 1)[0]
            code = code.split("//", 1)[0]
            if not code.strip():
                continue
            for kind, pat in PATTERNS:
                for m in pat.finditer(code):
                    lit = m.group(0)
                    status = "hardcoded"
                    if "${" in code:
                        status = "config_reference"
                    elif CONFIG_HINTS.search(code):
                        status = "externalized"
                    if status == "hardcoded":
                        reason = _allow_reason(rel)
                        if reason:
                            status = "allowed"
                    hits.append({
                        "file": rel, "line": i, "kind": kind, "literal": lit,
                        "status": status, "text": s[:160],
                    })
    seen, uniq = set(), []
    for h in hits:
        k = (h["file"], h["line"], h["literal"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(h)

    counts = {"externalized": 0, "config_reference": 0, "allowed": 0, "hardcoded": 0}
    for h in uniq:
        counts[h["status"]] += 1
    by_kind = {}
    for h in uniq:
        by_kind.setdefault(h["kind"], {"externalized": 0, "config_reference": 0,
                                       "allowed": 0, "hardcoded": 0})
        by_kind[h["kind"]][h["status"]] += 1

    return {
        "files_scanned": len(files),
        "hits_total": len(uniq),
        "counts": counts,
        "by_kind": by_kind,
        "hardcoded": [h for h in uniq if h["status"] == "hardcoded"],
    }


def _md(res: dict) -> str:
    c = res["counts"]
    out = ["# 配置审计小结（部署个性化项穷尽审计）", "",
           f"> 由 `tools/audit_config.py` 生成 ｜ 扫描 **{res['files_scanned']}** 个文件、"
           f"命中 **{res['hits_total']}** 处部署敏感字面量。", "",
           "| 外提状态 | 处数 | 含义 |", "|---|---|---|",
           f"| 已外提（读配置/环境变量） | {c['externalized']} | "
           f"字面量只是回退默认，值由 app.yaml 驱动 |",
           f"| `${{VAR}}` 密钥引用 | {c['config_reference']} | 运行期从 secrets.env 解析 |",
           f"| 允许保留（非部署个性化项） | {c['allowed']} | 安装器/模板/扫描器规则等 |",
           f"| **待外提** | **{c['hardcoded']}** | 既无配置读取、也非 `${{VAR}}` |",
           "", "## 按类别", "",
           "| 类别 | 已外提 | `${VAR}` | 允许 | 待外提 |", "|---|---|---|---|---|"]
    for k, v in sorted(res["by_kind"].items()):
        out.append(f"| {k} | {v['externalized']} | {v['config_reference']} | "
                   f"{v['allowed']} | {v['hardcoded']} |")
    out.append("")
    out += ["## 待外提明细", ""]
    if res["hardcoded"]:
        out += ["| 文件:行 | 类别 | 字面量 | 代码 |", "|---|---|---|---|"]
        for h in res["hardcoded"]:
            out.append(f"| `{h['file']}:{h['line']}` | {h['kind']} | "
                       f"`{h['literal'][:60]}` | `{h['text'][:90].replace('|', chr(92)+'|')}` |")
        out.append("")
    else:
        out.append("**无。** 全部部署个性化字面量均已外提或属允许保留项。")
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="部署个性化项穷尽审计")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--md", help="写出 Markdown 小结到该路径")
    ap.add_argument("--strict", action="store_true", help="存在待外提项即退出 1")
    args = ap.parse_args()

    res = scan()
    if args.md:
        with open(args.md, "w", encoding="utf-8") as f:
            f.write(_md(res))
        print(f"✅ 已写入 {args.md}")
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        c = res["counts"]
        print(f"扫描文件 {res['files_scanned']} 个，命中部署敏感字面量 {res['hits_total']} 处：")
        print(f"  已外提（读配置）      {c['externalized']}")
        print(f"  ${{VAR}} 密钥引用       {c['config_reference']}")
        print(f"  允许保留（非个性化项）{c['allowed']}")
        print(f"  待外提                {c['hardcoded']}")
        for h in res["hardcoded"][:25]:
            print(f"    - {h['file']}:{h['line']} [{h['kind']}] {h['literal'][:70]}")

    if args.strict and res["counts"]["hardcoded"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
