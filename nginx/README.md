# Nginx 配置说明

## 快速开始

安装脚本 `install.sh` 会自动将本目录的 `research-app.conf` 安装到
`/etc/nginx/sites-available/` 并链接到 `sites-enabled/`。

## 自定义设置

### 1. 更改域名

编辑 `research-app.conf` 中的 `server_name` 字段，将 `gutbrainaxis.online`
替换为你的实际域名。

### 2. 启用 HTTPS（推荐）

使用 Let's Encrypt 免费证书：

```bash
# 安装 certbot
sudo apt-get install -y certbot python3-certbot-nginx

# 获取证书（自动修改 Nginx 配置）
sudo certbot --nginx -d gutbrainaxis.online \
  -d www.gutbrainaxis.online \
  -d admin.gutbrainaxis.online \
  -d api.gutbrainaxis.online \
  -d svc.gutbrainaxis.online \
  -d tools.gutbrainaxis.online \
  -d data.gutbrainaxis.online
```

或手动配置 SSL：编辑配置文件中的 HTTPS 段（已被注释），填入证书路径。

### 3. 子域名映射表

| 子域名 | 后端端口 | 服务说明 |
|--------|----------|----------|
| `gutbrainaxis.online` | 301 → www | 裸域跳转 |
| `www.gutbrainaxis.online` | 8080 | Web 前端 |
| `api.gutbrainaxis.online` | 8000 | 问卷反馈 API |
| `svc.gutbrainaxis.online` | 8001 | 饮食反馈 |
| `admin.gutbrainaxis.online` | 9000 | 管理控制台 |
| `tools.gutbrainaxis.online` | 9876 | Webhook 工具 |
| `data.gutbrainaxis.online` | 8090 | 数据看板 |

### 4. DNS 配置

所有子域名 A 记录指向服务器 IP 地址。

### 5. 验证与重载

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## 日志

日志写入 `/var/log/nginx/` 目录：
- `admin.access.log` / `admin.error.log`
- `api.access.log` / `api.error.log`
- `svc.access.log` / `svc.error.log`
- `tools.access.log` / `tools.error.log`
- `data.access.log` / `data.error.log`
- `www.access.log` / `www.error.log`
