#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_time_config_doc.py — 生成《时间项配置总表》
=============================================================================
时间项的「键名 / 默认值 / 单位 / 作用 / 生效方式」由**代码单一真源**生成，
避免文档与配置漂移：

  * 键集合与分组  ← `services/admin_console/schedule_manager.GROUPS`
  * 默认值/类型   ← `config/app.yaml.example` + `config/app.schema.json`
  * timer 渲染    ← `tools/appconfig.timer_oncalendar`

用法::

    python3 tools/gen_time_config_doc.py            # 写入 docs/时间项配置总表.md
    python3 tools/gen_time_config_doc.py --stdout   # 只打印
    python3 tools/gen_time_config_doc.py --check    # 只校验文档是否已是最新（CI 用）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(PKG, "services", "admin_console"))

import appconfig as AC                                       # noqa: E402

try:
    import schedule_manager as SM
    GROUPS = SM.GROUPS
except Exception as e:                                       # noqa: BLE001
    print(f"⚠ 无法导入 schedule_manager.GROUPS：{e}", file=sys.stderr)
    GROUPS = []

OUT = os.path.join(PKG, "docs", "时间项配置总表.md")


def build() -> str:
    with open(AC.DEFAULT_SCHEMA, encoding="utf-8") as f:
        fields = json.load(f)["fields"]
    example = AC.flatten(AC._load_yaml(AC.EXAMPLE_CONFIG))

    total = sum(len(items) for _, items in GROUPS)
    lines = []
    lines.append("# 时间项配置总表")
    lines.append("")
    lines.append("> 本文件由 `tools/gen_time_config_doc.py` **自动生成**（勿手改）。")
    lines.append("> 单一真源：`config/app.yaml`（模板 `config/app.yaml.example`）"
                 "＋ `config/app.schema.json`。")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                 f"｜ 时间项共 **{total}** 个")
    lines.append("")
    lines.append("## 一、总览")
    lines.append("")
    lines.append(f"本系统**所有**影响「周期 / 时点 / 窗口 / 超时 / 冷却 / 退避 / 节流」"
                 f"的配置项，共 **{total}** 个，全部落在 "
                 "`schedule.*` / `analysis.*` / `webhook.*` / `alert.*` / "
                 "`selfcheck.*` / `llm.*` / `smtp.*` / `backup.*` / "
                 "`survey_platforms.*.fetch_*` 命名空间内。")
    lines.append("")
    lines.append("**「改配置即改行为」的三条落地路径**：")
    lines.append("")
    lines.append("1. `tools/appconfig.py --render-env /etc/research-app/env` "
                 "→ 端口/窗口/静默等写入 systemd 环境文件；")
    lines.append("2. `tools/appconfig.py --render-timers /etc/systemd/system` "
                 "→ `schedule.*` 渲染为 `<unit>.timer.d/10-schedule.conf`"
                 "（`OnCalendar` / `RandomizedDelaySec` / `AccuracySec` / `Persistent`）；"
                 "改后 `systemctl daemon-reload` 生效；")
    lines.append("3. 运行期服务（LLM worker、各 sync、告警、看板）用 "
                 "`services/common/lib_schedule.py` **每 30 秒重读配置**，"
                 "窗口/静默判定即时生效。")
    lines.append("")
    lines.append("控制台「**调度**」页（`admin_console`，默认 :9000）可视化查看与修改"
                 "本表全部条目，写入前自动备份 `app.yaml.bak.<时间戳>`。")
    lines.append("")

    for title, items in GROUPS:
        lines.append(f"## {title}（{len(items)} 项）")
        lines.append("")
        lines.append("| 键名 | 默认值 | 单位 | 作用 | 生效方式 |")
        lines.append("|---|---|---|---|---|")
        for key, label, unit, effect, apply_how in items:
            val = example.get(key, "—")
            if isinstance(val, bool):
                val = "true" if val else "false"
            if val == "":
                val = "（空）"
            val = str(val).replace("|", "\\|")
            lines.append(f"| `{key}` | `{val}` | {unit} | {label}：{effect} | {apply_how} |")
        lines.append("")

    # timer 渲染对照
    lines.append("## 附：timer 渲染对照（由 schedule.* 计算）")
    lines.append("")
    lines.append("| systemd timer | OnCalendar（当前配置） | 来源键 |")
    lines.append("|---|---|---|")
    cfg = AC.flatten(AC._load_yaml(AC.EXAMPLE_CONFIG))
    for unit, (kind, key, default) in AC.TIMER_SPEC.items():
        lines.append(f"| `{unit}` | `{AC.timer_oncalendar(cfg, unit)}` | `{key}` |")
    lines.append("")
    lines.append("> 注意：`*:0/N` 表示「每小时的第 0、N、2N… 分钟触发」。"
                 "`N` 必须能整除 60 才均匀（如 15/30/60）；"
                 "其余值（如 7）会被 systemd 解释为整点后的第 0、7、14… 分钟，"
                 "跨小时部分由 `RandomizedDelaySec` 抖动补偿。")
    lines.append("")
    lines.append("## 附：校验与自检")
    lines.append("")
    lines.append("```bash")
    lines.append("# 1) schema 与 example 键集合一致（无悬空键）")
    lines.append("python3 tools/appconfig.py --check-example-keys")
    lines.append("")
    lines.append("# 2) 当前窗口 / 静默时段状态")
    lines.append("python3 tools/appconfig.py --show-window")
    lines.append("")
    lines.append("# 3) 时间项格式（HH:MM / workdays）在自检 config 组中校验")
    lines.append("python3 tools/selfcheck.py --only config")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成《时间项配置总表》")
    ap.add_argument("--stdout", action="store_true", help="只打印")
    ap.add_argument("--check", action="store_true", help="校验文档是否最新（CI）")
    args = ap.parse_args()

    content = build()
    if args.stdout:
        print(content)
        return 0
    if args.check:
        try:
            with open(OUT, encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            print("❌ 文档不存在：" + OUT, file=sys.stderr)
            return 1
        # 忽略生成时间行的差异
        norm = lambda s: "\n".join(l for l in s.splitlines()
                                   if not l.startswith("> 生成时间："))
        if norm(existing) != norm(content):
            print("❌ docs/时间项配置总表.md 与配置不一致，请运行 "
                  "python3 tools/gen_time_config_doc.py", file=sys.stderr)
            return 1
        print("✅ 时间项文档与配置一致")
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(content)
    print("✅ 已写入 " + OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
