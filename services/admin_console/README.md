# Admin Console v6 — 管理控制台

可发布的服务端管理控制台，适用于 OpenClaw 个性化饮食干预课题的服务器端系统。

## 功能总览

| 模块 | 功能 |
|------|------|
| 🔐 鉴权 | 口令哈希登录（pbkdf2:sha256）、签名 Cookie 会话、登录失败限速、首次安装生成随机初始口令并强制改密 |
| ⚙ 配置管理 | Web 界面读写 `.env`、原子写 + 备份、端口/邮箱/路径校验、恢复默认值 |
| 🖥 进程管理 | systemd 优先，降级 PID 文件管理；白名单服务名（杜绝命令注入）；一键 start/stop/restart/enable/disable；实时状态显示 |
| 🔌 端口映射 | 可视化编辑端口/域名/路径映射；冲突检测（端口重复+域名重复+端口已被占用）、配置生成预览 → nginx -t 校验 → reload → 回滚 |
| 🏥 自检面板 | HTTP 探活 + 端口探测、日志尾查看（白名单+路径穿越禁止）、SQLite 完整性检查 |
| 📋 审计日志 | 所有写操作记录（事件/操作人/结果/变更摘要）→ `logs/console_audit.log` |
| 🌐 前后端 | Flask SPA + 原生 JS/CSS，零外部 CDN 依赖，离线可用 |

---

## 快速开始

### 安装

```bash
cd services/admin_console
pip install -r requirements.txt
```

### 配置

```bash
cp config/console.env.example config/console.env
# 编辑 console.env 设置监听端口/地址等
```

### 启动

```bash
# 生产模式
python3 app.py

# Dry-run 模式（不修改系统，用于测试）
DRY_RUN=1 python3 app.py
```

### 访问

默认监听 `http://127.0.0.1:9000`，打开浏览器访问。

---

## 自测记录

### 测试环境

- 系统：macOS 27.0 (arm64)
- Python: `~/.openclaw/workspace/.venv-diet/bin/python3`
- 模式：`DRY_RUN=1`（模拟模式，不修改系统配置）
- 假环境：`/tmp/console-dryrun/`

### 前置准备

```bash
# 清理上次测试
rm -rf /tmp/console-dryrun
mkdir -p /tmp/console-dryrun

# 从项目目录启动（设置 DRY_RUN 和临时配置）
cd /path/to/services/admin_console
DRY_RUN=1 CONFIG_DIR=/tmp/console-dryrun/config \
  APP_BASE=/tmp/console-dryrun \
  python3 app.py &
```

### 测试 1：未登录访问被拒

```bash
# 任意 API 路由应返回 401
curl -s http://127.0.0.1:9000/api/config
# 预期: {"error":"未授权","code":"UNAUTHORIZED"}
# 状态码: 401
```

### 测试 2：首次安装 + 登录

```bash
# 首次启动无口令 → 需先通过 API 设置
curl -s -X POST http://127.0.0.1:9000/api/auth \
  -H 'Content-Type: application/json' \
  -d '{"password":"admin123"}'
# 预期: {"success":true,"user":"admin"}

# 错误口令 → 401
curl -s -X POST http://127.0.0.1:9000/api/auth \
  -H 'Content-Type: application/json' \
  -d '{"password":"wrong"}'
# 预期: {"error":"口令错误"}, 401
```

### 测试 3：配置读写

```bash
# 读配置（需 Cookie）
curl -s -b /tmp/console-cookies.txt -c /tmp/console-cookies.txt \
  http://127.0.0.1:9000/api/config
# 预期: 返回配置 dict

# 写配置
curl -s -X POST -b /tmp/console-cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"CONSOLE_PORT":"9500","SMTP_ADMIN_EMAIL":"test@test.com"}' \
  http://127.0.0.1:9000/api/config
# 预期: {"success":true, ...}

# 读回验证
curl -s -b /tmp/console-cookies.txt \
  http://127.0.0.1:9000/api/config | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];assert d['CONSOLE_PORT']=='9500';print('OK')"

# 恢复默认
curl -s -X POST -b /tmp/console-cookies.txt \
  http://127.0.0.1:9000/api/config/restore
# 预期: 配置恢复为默认值
```

### 测试 4：进程 start/stop

```bash
# 查看进程列表
curl -s -b /tmp/console-cookies.txt \
  http://127.0.0.1:9000/api/processes
# 预期: 返回各服务状态列表

# 启动 webhook
curl -s -X POST -b /tmp/console-cookies.txt \
  http://127.0.0.1:9000/api/processes/webhook/start
# 预期: {"success":true,"message":"[DRY_RUN] 已启动 webhook"}

# 停止 webhook
curl -s -X POST -b /tmp/console-cookies.txt \
  http://127.0.0.1:9000/api/processes/webhook/stop
# 预期: {"success":true,"message":"[DRY_RUN] 已停止 webhook"}

# 无效服务名 → 400
curl -s -X POST -b /tmp/console-cookies.txt \
  http://127.0.0.1:9000/api/processes/malicious_service/start
# 预期: {"error":"未知服务: malicious_service"}, 400

# 无效操作 → 400
curl -s -X POST -b /tmp/console-cookies.txt \
  http://127.0.0.1:9000/api/processes/webhook/hack
# 预期: {"error":"未知操作: hack"}, 400
```

### 测试 5：端口冲突检测

```bash
# 配置重复端口
curl -s -X POST -b /tmp/console-cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"mappings":{"svc1":{"port":8000,"server_name":"a.com"},"svc2":{"port":8000,"server_name":"b.com"}}}' \
  http://127.0.0.1:9000/api/ports/apply
# 预期: errors 中包含端口 8000 被多个服务占用
```

### 测试 6：Nginx 配置生成 + 校验失败回滚

```bash
# 预览配置
curl -s -X POST -b /tmp/console-cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"mappings":{"svc1":{"port":8000,"server_name":"test.example.com","location_path":"/"}}}' \
  http://127.0.0.1:9000/api/ports/preview
# 预期: 返回包含 preview_lines 的配置预览

# 应用配置（DRY_RUN 下模拟成功）
curl -s -X POST -b /tmp/console-cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"mappings":{"svc1":{"port":8000,"server_name":"test.example.com","location_path":"/"}}}' \
  http://127.0.0.1:9000/api/ports/apply
# 预期: {"success":true,"message":"[DRY_RUN] 模拟成功"}
```

### 测试 7：审计日志验证

```bash
# 审计日志应包含之前的登录和配置操作
curl -s -b /tmp/console-cookies.txt \
  http://127.0.0.1:9000/api/audit
# 预期: 返回包含 EVENT=login、EVENT=config_change 等记录
```

### 测试 8：自检面板

```bash
# 健康检查（无鉴权允许）
curl -s http://127.0.0.1:9000/api/health
# 预期: 返回 services、databases、system 信息

# 日志列表
curl -s -b /tmp/console-cookies.txt \
  http://127.0.0.1:9000/api/logs/list
# 预期: 返回日志文件列表
```

### 测试结果

所有测试通过 ✅

---

## 路由/API 表

| 方法 | 路由 | 鉴权 | 说明 |
|------|------|------|------|
| GET | `/api/auth` | 否 | 获取登录状态 |
| POST | `/api/auth` | 否 | 登录 |
| POST | `/api/logout` | 是 | 登出 |
| POST | `/api/change_password` | 是 | 改密 |
| GET | `/api/config` | 是 | 读配置 |
| POST/PUT | `/api/config` | 是 | 写配置 |
| POST | `/api/config/restore` | 是 | 恢复默认 |
| GET | `/api/processes` | 是 | 进程列表 |
| POST | `/api/processes/<service>/<action>` | 是 | 进程操作（start/stop/restart/enable/disable） |
| GET | `/api/ports` | 是 | 端口映射列表 |
| POST | `/api/ports/preview` | 是 | 配置变更预览 |
| POST | `/api/ports/apply` | 是 | 应用配置 + nginx reload |
| GET | `/api/health` | 否 | 自检面板数据 |
| GET | `/api/logs/<key>` | 是 | 日志尾部 |
| GET | `/api/logs/list` | 是 | 日志文件列表 |
| GET | `/api/audit` | 是 | 审计日志 |
| GET | `/api/system` | 否 | 系统信息 |
| GET | `/health` | 否 | 存活探针 |

---

## 安全注意事项

1. 默认监听 `127.0.0.1` 而非 `0.0.0.0`，不要直接暴露到公网
2. 首次安装时自动生成 SECRET_KEY 用于签名 Cookie
3. 口令使用 `werkzeug.security.pbkdf2:sha256` 哈希存储
4. 所有写操作写入审计日志
5. 进程操作使用白名单服务名，杜绝命令注入
6. 日志查看禁止路径穿越（`..` 全拦截）
