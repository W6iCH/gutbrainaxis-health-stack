# CONTRIBUTING · 贡献与开发约定

> 适用对象：本部署包（`gutbrainaxis-health-stack`）的后续维护者。
> 目标：**改动可复现、行为可解释、发布可审计**。

---

## 1. 环境

```bash
# 本机开发（macOS/Linux 均可，无需 root）
git clone <repo> gutbrainaxis-health-stack
cd gutbrainaxis-health-stack
PY=~/.openclaw/workspace/.venv-diet/bin/python3   # 本课题约定；或任意 python3.10+
$PY -m pip install pyyaml flask flask-cors requests    # 最小依赖
```

> 生产只跑 Ubuntu 22.04/24.04（aarch64/x86_64），但**本地开发不需要 systemd/nginx**：
> 相关检查会显示为 `skip` 而非 `fail`。

---

## 2. 铁律（Red Lines）

1. **零 PII、零真实 Token**：任何提交不得含真实学号/姓名/邮箱、真实问卷 Token、
   SMTP 口令、LLM Key。一律用占位符（`__CHANGE_ME__`、`${VAR}`、`example.com`）。
2. **只写本仓库**：不得修改课题其他目录（原始数据、评分标准、分析产出等）。
3. **改文件前先归档**：把改动前副本放到 `_归档_YYYYMMDDx/<原相对路径>`，
   **不删除**非本次范围的文件。
4. **密钥只从 `secrets.env`（0600）** 读取；`app.yaml` 只写 `${VAR}` 引用。
5. **不 `git push`**（同步由维护者统一执行）。

---

## 3. 改配置 = 改代码

**所有可配置项必须落在 `config/app.schema.json` + `config/app.yaml.example`**，
二者键集合必须**完全一致**（无缺键、无悬空键）：

```bash
$PY tools/appconfig.py --check-example-keys   # 必须 0 错误
```

新增配置项时：

1. 在两个文件里都加（schema 声明 `type`/`required`/`min`/`max`/`enum`/`secret`）；
2. 若属于**时间项**（周期/时点/窗口/超时/冷却/退避/节流），
   还要在 `services/admin_console/schedule_manager.py` 的 `GROUPS` 中登记
   （中文名 / 单位 / 作用 / 生效方式），并重新生成文档：
   ```bash
   $PY tools/gen_time_config_doc.py
   ```
3. 若需渲染进 systemd：在 `tools/appconfig.py` 的 `ENV_MAP` / `TIMER_SPEC` 中登记；
4. 运行时读取：`services/common/lib_schedule.load_config()`（不要自己 `yaml.safe_load`）。

> 时间窗口逻辑**唯一实现**在 `services/common/lib_schedule.py`。
> 任何地方需要「是否在窗口内」都调它，禁止复制第二份。

---

## 4. 代码约定

| 主题 | 约定 |
|---|---|
| 依赖 | 服务端只用**标准库 + Flask/flask-cors/requests/PyYAML**；前端零 CDN（内网离线可用） |
| 日志 | 结构化 JSON 行（`lib_runtime.JsonLog`）；**不打印密钥/PII**；不吞异常 |
| 错误处理 | HTTP 层用 `lib_errors.guard` / `AppError`；对外响应**不含堆栈与内部路径** |
| 队列 | 用 `lib_queue.FileQueue`（原子领取 + 幂等恢复），不要自己写 `os.listdir` 轮询 |
| 限流 | 用 `lib_ratelimit.SlidingWindowLimiter`；登录锁定用 `LoginLockout`（持久化） |
| 校验 | 外部输入一律设长度/深度/大小上限（`lib_validate`） |
| 失败隔离 | **单条失败不得影响整批**：逐条 try/except 且记录到日志/队列 |
| 幂等 | 写库按业务唯一键去重；重试不得产生重复行；定时/回调双通道必须幂等 |
| 时区 | 一律显式时区（默认 `Asia/Shanghai`）；禁止裸 `datetime.now()` 做窗口判定 |
| 路径 | 不得写死 `/Users/...`；一律 `APP_BASE` / 环境变量 / 相对包路径 |

---

## 5. 测试

```bash
$PY -m unittest discover -s tests -t . -v     # 单元 + 集成
bash ci.sh                                     # 全量门禁
bash ci.sh --with-install                      # 追加 install.sh --dry-run 幂等校验
```

* 新增功能**必须**补测试；涉及时间窗口/幂等的功能必须覆盖「窗口外」「重复投递」
  「崩溃恢复」三类边界。
* 涉及 HTTP 的改动，优先写**真实起服务**的集成测试（参考 `tests/test_webhook.py`：
  注入 stub 模块 + 选空闲端口 + 断言状态码）。

---

## 6. 发布检查单

发布前**必须**全绿：

```bash
bash ci.sh --with-install
$PY tools/scan_sensitive.py            # 0 阻断项
$PY tools/selfcheck.py                 # 沙箱环境无 fail
$PY tools/appconfig.py --check-example-keys
$PY tools/gen_time_config_doc.py --check
# 上期已移除模块的残留关键词检查（词表见 ci.sh 第 8 步，期望 0 命中）
bash ci.sh
```

并更新：

* `CHANGELOG.md`（本次改动 + 为什么）
* `变更清单.md`（逐条改动 + 商用级必要性）
* `功能与进程清单.md`（功能/单元计数必须与**实际文件**逐一一致）

> **模拟数据测试**：用 `tools/seed_mock_data.py` 在 `/tmp` 沙箱跑通端到端，
> 测完必须 `tools/clear_mock_data.py` 清除，并在 `_测试证据_*/` 留下前后对照证据。

---

## 7. 提交信息建议

```
<类型>: <一句话>

类型：feat | fix | docs | test | refactor | chore | security | ops

正文说明：
- 改了什么
- 为什么（商用级必要性 / 缺陷后果）
- 如何验证（命令 + 期望输出）
```

示例：
```
security: 登录失败锁定改为持久化

改了什么：AuthManager 的失败计数从内存字典改为 lib_ratelimit.LoginLockout（JSON 0600）
为什么：旧实现重启即清零，攻击者可用 `systemctl restart` 绕过锁定
如何验证：bash ci.sh；手工连错 5 次 → 429；重启服务后仍 429
```
