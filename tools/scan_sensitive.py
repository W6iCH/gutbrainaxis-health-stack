#!/usr/bin/env python3
"""
脱敏扫描器 — 发布前强制自检
============================================================
在把本目录推送到 GitHub 之前运行，检查是否存在：

  A. 真实数据（数据库 / 日志 / 队列 / 数据导出）
  B. 真实凭据（API key / token / SMTP 密码 / 私钥）
  C. 个人隐私信息（PII：邮箱 / 手机 / 身份证 / 姓名 / 学号）
  D. 不可移植的绝对路径（本地个人目录 / 硬编码 /opt）

用法：
    python3 tools/scan_sensitive.py            # 扫描仓库根
    python3 tools/scan_sensitive.py --path .   # 指定目录
    python3 tools/scan_sensitive.py --json     # 机器可读输出
    python3 tools/scan_sensitive.py --report tools/脱敏扫描报告.txt

退出码：
    0 = 无 BLOCKER（可以发布）
    1 = 存在 BLOCKER（禁止发布）

设计原则：占位符与示例值（example.com / __CHANGE_ME__ / <...>）不算命中。
"""

import argparse
import fnmatch
import json
import os
import re
import sys
from collections import Counter, defaultdict

# ── 扫描目标 ───────────────────────────────────────────────────────────────

TEXT_EXT = {
    ".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts",
    ".html", ".htm", ".css", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".env", ".example", ".md", ".txt",
    ".service", ".timer", ".sql", ".csv", ".j2", ".tmpl", ".template",
}
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".trash", "dist", "build", ".idea",
    ".vscode", "site-packages",
}
MAX_BYTES = 4 * 1024 * 1024  # 单个文件最多读 4MB

# ── 规则表 ─────────────────────────────────────────────────────────────────
# level: BLOCKER（禁止发布） / WARN（需人工确认） / INFO

RULES = [
    # ── B. 凭据 ────────────────────────────────────────────────────────────
    ("B", "BLOCKER", "OpenAI/兼容 API Key",
     re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("B", "BLOCKER", "GitHub Token",
     re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("B", "BLOCKER", "AWS Access Key",
     re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("B", "BLOCKER", "Google API Key",
     re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("B", "BLOCKER", "Slack Token",
     re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    ("B", "BLOCKER", "私钥块",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----")),
    ("B", "BLOCKER", "JWT",
     re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("B", "BLOCKER", "SJTU/门户 appkey 赋值",
     re.compile(r"""(?i)\b(?:app_?secret|app_?key|api_?key|access_?token|"""
                r"""client_?secret|cloudflared?_?token|rclone_?token)\b\s*[:=]\s*"""
                r"""["'][^"'\s]{12,}["']""")),
    ("B", "WARN", "疑似硬编码口令赋值",
     re.compile(r"""(?i)\b(?:password|passwd|pwd|smtp_pass|db_pass)\b\s*[:=]\s*"""
                r"""["'](?![A-Z_<{$\s]{0,3}["'])[^"'\s]{6,}["']""")),
    ("B", "WARN", "Bearer 字面量",
     re.compile(r"""(?i)["']Bearer\s+[A-Za-z0-9_\-\.]{20,}["']""")),

    # ── C. PII ────────────────────────────────────────────────────────────
    ("C", "WARN", "邮箱地址",
     re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("C", "BLOCKER", "中国大陆手机号",
     re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("C", "BLOCKER", "身份证号",
     re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("C", "WARN", "疑似学号（独立 8-12 位数字）",
     re.compile(r"(?<![\w./\-_])[12]\d{7,11}(?![\w./\-_])")),

    # ── D. 不可移植路径 ───────────────────────────────────────────────────
    ("D", "BLOCKER", "个人本地绝对路径",
     re.compile(r"/Users/[A-Za-z0-9._\-]+/")),
    ("D", "WARN", "硬编码 /opt 路径",
     re.compile(r"""(?:^|["'\s=(])/opt/[A-Za-z0-9_\-]+""")),

    # ── A. 数据痕迹（内容层） ─────────────────────────────────────────────
    ("A", "WARN", "疑似真实数据库路径引用",
     re.compile(r"(?:survey|diet|exercise)_data\.db")),
]

# 视为“已正确占位/示例”而豁免的值
ALLOW_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "example.edu",
                       "localhost", "test.local", "invalid")
ALLOW_EMAIL_LOCAL = ("operator", "admin", "noreply", "no-reply", "user", "mailer",
                     "your", "you", "someone", "test", "example", "sender", "recipient")
ALLOW_TEXT_HINTS = (
    "__CHANGE_ME__", "CHANGE_ME", "PLACEHOLDER", "YOUR_", "<your", "xxxx",
    "TODO", "占位", "示例", "请填写", "填写", "your_", "example",
)

# 强制忽略的文件/路径（本目录内的说明文档本身会提到这些词）
IGNORE_REL = {
    "tools/scan_sensitive.py",   # 本扫描器自身含规则文本
    "tools/脱敏扫描报告.txt",
    ".gitignore",
}
# 内部工作文档：保留在磁盘上供追溯，但不进入发布集（.gitignore 已排除）
IGNORE_GLOBS = ("00_*.md", "*.log", "*_工作记录.md")

# ── 文件系统级检查 ─────────────────────────────────────────────────────────

FS_BLOCKERS = [
    (re.compile(r"\.db$|\.db-wal$|\.db-shm$|\.sqlite3?$"), "真实数据库文件"),
    (re.compile(r"\.log$|\.log\.\d+$"), "日志文件"),
    (re.compile(r"\.bak\d*$|\.bak_.*$|\.orig$"), "备份文件"),
    (re.compile(r"\.zip$|\.tar\.gz$|\.tgz$|\.7z$"), "压缩包（可能含数据）"),
]
FS_DIR_BLOCKERS = {
    ".llm_queue": "LLM 队列（含真实饮食记录）",
    ".email_queue": "邮件队列（含真实收件地址）",
    "logs": "日志目录",
    "log": "日志目录",
    "__pycache__": "Python 字节码缓存",
    "venv": "虚拟环境",
    ".venv": "虚拟环境",
    ".git": "Git 内部目录",
}

# 允许保留的参考数据（营养表等非个人数据）
FS_ALLOW = {
    "food_nutrition.csv",
    "dish_nutrition_calculated.csv",
    "population_stats.json",   # 人群基线统计（聚合值），需人工确认
}


class Finding:
    __slots__ = ("category", "level", "rule", "path", "line", "text")

    def __init__(self, category, level, rule, path, line, text):
        self.category = category
        self.level = level
        self.rule = rule
        self.path = path
        self.line = line
        self.text = text

    def as_dict(self):
        return {"category": self.category, "level": self.level, "rule": self.rule,
                "path": self.path, "line": self.line, "text": self.text}


def _allowed_email(match_text):
    try:
        local, _, domain = match_text.partition("@")
    except ValueError:
        return False
    if domain.lower() in ALLOW_EMAIL_DOMAINS:
        return True
    if any(local.lower().startswith(p) for p in ALLOW_EMAIL_LOCAL):
        return True
    return False


def _is_allowed_line(line):
    up = line.upper()
    if any(h.upper() in up for h in ALLOW_TEXT_HINTS):
        return True
    # 常量赋值行（如 WEEKLY_TOKEN_LIMIT = 1000000000）不是学号
    if re.match(r"\s*[A-Z][A-Z0-9_]{3,}\s*[:=]", line):
        return True
    return False


_ALLOW_OPT_IN_EXAMPLE = re.compile(r"\.(?:example|tmpl|template|sample)$")


def _allow_opt_path(rel, line):
    """`.example` / 模板文件里的 /opt 是文档化的默认值，属预期。"""
    if _ALLOW_OPT_IN_EXAMPLE.search(rel):
        return True
    return _is_allowed_line(line)


def scan_content(root, findings):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            if rel.replace(os.sep, "/") in IGNORE_REL:
                continue
            if any(fnmatch.fnmatch(fn, g) for g in IGNORE_GLOBS):
                continue

            # A. 文件系统级
            for rx, desc in FS_BLOCKERS:
                if rx.search(fn) and fn not in FS_ALLOW:
                    findings.append(Finding("A", "BLOCKER", desc, rel, 0, fn))
                    break

            ext = os.path.splitext(fn)[1].lower()
            if ext not in TEXT_EXT and fn not in ("Dockerfile", "Makefile"):
                continue
            try:
                if os.path.getsize(full) > MAX_BYTES:
                    findings.append(Finding("A", "WARN", "文件过大，已跳过内容扫描",
                                            rel, 0, f">{MAX_BYTES} bytes"))
                    continue
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError as exc:
                findings.append(Finding("A", "WARN", f"无法读取: {exc}", rel, 0, ""))
                continue

            for i, line in enumerate(lines, 1):
                for cat, level, rule, rx in RULES:
                    m = rx.search(line)
                    if not m:
                        continue
                    hit = m.group(0)
                    if rule == "邮箱地址":
                        if _allowed_email(hit) or _is_allowed_line(line):
                            continue
                    if rule in ("疑似学号（独立 8-12 位数字）",
                                "疑似真实数据库路径引用", "疑似硬编码口令赋值",
                                "Bearer 字面量"):
                        if _is_allowed_line(line):
                            continue
                    if rule == "硬编码 /opt 路径":
                        if _allow_opt_path(rel, line):
                            continue
                    findings.append(Finding(cat, level, rule, rel, i,
                                            line.strip()[:200]))
    return findings


def scan_dirs(root, findings):
    """目录级阻断项。

    ⚠️ 范围修正（设计复查）：
      旧实现不剪枝 SKIP_DIRS，导致在**普通 git checkout 内**扫描时把仓库自身的
      `.git/` 判为阻断项 → 该门禁在任何 checkout 都必然失败（无法使用）。
      现改为：
        · 与 SKIP_DIRS 重叠的目录（`.git` / `__pycache__` / `venv` 等）→ **WARN**，
          并剪枝不再深入（它们已被 .gitignore 排除，不属于发布集）；
        · 真正的数据/队列/日志目录（`logs` / `.llm_queue` / `.email_queue`）
          → 仍为 **BLOCKER**。
      这样既保留门禁信号，又让门禁可用。
    """
    for dirpath, dirnames, _ in os.walk(root):
        keep = []
        for d in dirnames:
            if d in FS_DIR_BLOCKERS:
                rel = os.path.relpath(os.path.join(dirpath, d), root)
                level = "WARN" if d in SKIP_DIRS else "BLOCKER"
                findings.append(Finding("A", level, FS_DIR_BLOCKERS[d], rel, 0, d))
            if d in SKIP_DIRS:
                continue          # 剪枝：非发布集，不深入
            keep.append(d)
        dirnames[:] = keep
    return findings


def main():
    ap = argparse.ArgumentParser(description="发布前脱敏扫描")
    ap.add_argument("--path", default=".", help="要扫描的目录（默认当前目录）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--report", help="同时写出文本报告到该路径")
    ap.add_argument("--quiet", action="store_true", help="只输出汇总")
    args = ap.parse_args()

    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(f"错误：目录不存在 {root}", file=sys.stderr)
        return 2

    findings = []
    scan_content(root, findings)
    scan_dirs(root, findings)

    blockers = [f for f in findings if f.level == "BLOCKER"]
    warns = [f for f in findings if f.level == "WARN"]

    ok = not blockers

    if args.json:
        print(json.dumps({
            "root": root,
            "verdict": "PASS" if ok else "FAIL",
            "blockers": len(blockers),
            "warnings": len(warns),
            "findings": [f.as_dict() for f in findings],
        }, ensure_ascii=False, indent=2))
    else:
        lines = []
        lines.append("=" * 72)
        lines.append("  脱敏扫描报告  (publish-safety scan)")
        lines.append("=" * 72)
        lines.append(f"扫描目录 : {root}")
        lines.append(f"结论     : {'✅ PASS — 未发现阻断项' if ok else '❌ FAIL — 存在阻断项，禁止发布'}")
        lines.append(f"阻断项   : {len(blockers)}")
        lines.append(f"警告项   : {len(warns)}")
        lines.append("")

        by_rule = defaultdict(list)
        for f in findings:
            by_rule[(f.level, f.category, f.rule)].append(f)

        for (level, cat, rule) in sorted(by_rule, key=lambda k: (k[0] != "BLOCKER", k[1], k[2])):
            group = by_rule[(level, cat, rule)]
            icon = "🛑" if level == "BLOCKER" else "⚠️ "
            lines.append(f"{icon} [{cat}] {rule}  ×{len(group)}")
            if not args.quiet:
                for f in group[:25]:
                    loc = f"{f.path}:{f.line}" if f.line else f.path
                    snippet = f"  → {f.text}" if f.text else ""
                    lines.append(f"      {loc}{snippet}")
                if len(group) > 25:
                    lines.append(f"      … 另有 {len(group) - 25} 处")
            lines.append("")

        file_groups = Counter(os.path.dirname(f.path) or "." for f in findings)
        lines.append("-" * 72)
        lines.append("按目录汇总（前 20）")
        for d, c in file_groups.most_common(20):
            lines.append(f"  {c:5d}  {d}")

        text = "\n".join(lines)
        print(text)

        if args.report:
            os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
            with open(args.report, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
