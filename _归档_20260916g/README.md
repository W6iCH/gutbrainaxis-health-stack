# _归档_20260916g — 本轮（补做：计分一致性验证 + 可配置项总清单）归档说明

## 结论：本轮**零删除**

本轮全部改动均为**新增或就地修改**，**未删除任何文件**（含 `_归档_*`、`config/` 等范围外内容）。
因此本目录不存放「被删除文件」，只存放**改动说明**与**可回退线索**。

## 新增文件
| 文件 | 说明 |
|---|---|
| `tools/verify_scoring_parity.py` | 计分一致性验证（程序 ⟷ 权威条目库，逐条断言；L1–L4 四层）※本目录创建时已由同轮工作落地 |
| `tools/audit_config.py` | 部署个性化项穷尽审计扫描器（本次新增） |
| `tools/gen_config_doc.py` | 《可配置项总清单》生成器（配置→文档，`--check` 防漂移） |
| `docs/可配置项总清单.md` | 180 项配置总清单（自动生成） |
| `docs/分析口径一致性核对.md` | 部署包 ⟷ 科研处理的口径差异清单与结论 |
| `tests/test_parity_and_config.py` | 计分一致性与配置一致性的回归测试 |
| `config/study_calendar.example.yaml` | 研究日历样例 |

## 就地修改（仅追加/替换实现，不改变对外接口）
`tools/appconfig.py`、`tools/selfcheck.py`、`tools/gen_config_doc.py`、
`services/common/lib_study.py`（注释修正）、
`services/diet_survey/run_all.sh`、`services/sjtu_survey_pro/run_all.sh`（端口改环境变量回退）、
`services/*/{survey,diet,exercise}_sync_cron.py`（API base 外提）、
`services/{sjtu_survey_pro/email_feedback.py,diet_survey/diet_email_feedback.py}`（邮件域名外提）、
`config/app.schema.json`、`config/app.yaml.example`、`install.sh`、`docs/时间项配置总表.md`。

## 回退线索
本轮改动**未 commit**（按规则由主 Agent 统一提交）。改动前的服务器端原始版本可从
`/Users/which/Downloads/服务器数据/` 与上一轮归档 `_归档_20260916f/` 取回；
`git diff` 亦可逐文件比对。
