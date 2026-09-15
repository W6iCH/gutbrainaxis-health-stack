"""
config_manager.py — .env 读写/校验/恢复默认
"""
import os, re, shutil, tempfile, json
from datetime import datetime
from pathlib import Path
from typing import Optional


class ConfigValidationError(ValueError):
    def __init__(self, field: str, msg: str):
        self.field = field
        super().__init__(f"{field}: {msg}")


class ConfigManager:
    """管理控制台所有配置项（.env 格式）"""

    # ── 配置 item 定义 ──────────────────────────────────────────────
    FIELDS = {
        # (key, label, default, validator)
        "CONSOLE_PORT": ("CONSOLE_PORT", "控制台端口", "9000", None),
        "CONSOLE_HOST": ("CONSOLE_HOST", "监听地址", "127.0.0.1", None),
        "APP_BASE": ("APP_BASE", "应用基准目录", "/opt", None),
        "NGINX_SITES_DIR": ("NGINX_SITES_DIR", "Nginx 站点配置目录", "/etc/nginx/sites-available", None),
        "NGINX_ENABLED_DIR": ("NGINX_ENABLED_DIR", "Nginx 启用目录", "/etc/nginx/sites-enabled", None),
        "SMTP_HOST": ("SMTP_HOST", "SMTP 服务器", "mail.sjtu.edu.cn", None),
        "SMTP_PORT": ("SMTP_PORT", "SMTP 端口", "465", None),
        "SMTP_USERNAME": ("SMTP_USERNAME", "SMTP 用户名", "", None),
        "SMTP_PASSWORD": ("SMTP_PASSWORD", "SMTP 密码", "", None),
        "SMTP_SENDER_EMAIL": ("SMTP_SENDER_EMAIL", "发件人邮箱", "", "email"),
        "SMTP_ADMIN_EMAIL": ("SMTP_ADMIN_EMAIL", "管理员邮箱（收告警）", "", "email"),
        "ALERT_TO": ("ALERT_TO", "告警收件箱", "", "email"),
        "LLM_API_KEYS": ("LLM_API_KEYS", "LLM API Key 列表（逗号分隔）", "", None),
        "LLM_BASE_URL": ("LLM_BASE_URL", "LLM API 地址", "https://models.sjtu.edu.cn/api/v1", None),
        "SECRET_KEY": ("SECRET_KEY", "Flask 签名密钥", "", None),
    }

    REQUIRED = {
        "CONSOLE_PORT", "CONSOLE_HOST", "APP_BASE",
        "SMTP_HOST", "SMTP_PORT",
        "SMTP_ADMIN_EMAIL",
    }

    def __init__(self, config_dir: str):
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.env_path = self.config_dir / "console.env"
        self.example_path = self.config_dir / "console.env.example"

    # ── 读 ──────────────────────────────────────────────────────────

    def load(self) -> dict:
        """读取全部配置项（返回 dict，无效值返回默认值）"""
        raw = self._read_env_file()
        result = {}
        for key, _label, default, _validator in self.FIELDS.values():
            result[key] = raw.get(key, default)
        return result

    def get(self, key: str, default: str = "") -> str:
        d = self.load()
        return d.get(key, default)

    # ── 写（原子写 + 备份） ─────────────────────────────────────────

    def save(self, updates: dict) -> dict:
        """
        校验并保存配置。
        updates: {KEY: value, ...} 只包含要修改的项。
        返回 {success: bool, errors: {field: msg}, warnings: [...]}
        """
        result = {"success": True, "errors": {}, "warnings": []}

        current = self.load()
        errors = self._validate(updates)
        if errors:
            result["success"] = False
            result["errors"] = errors
            return result

        new_config = {**current, **updates}
        lines = self._format_env_lines(new_config)

        # 原子写: 临时文件 → rename
        tmp = self.env_path.with_suffix(".env.tmp")
        try:
            tmp.write_text("".join(lines), encoding="utf-8")
            tmp.chmod(0o600)

            # 备份旧文件
            if self.env_path.exists():
                bak = self.env_path.with_name(
                    f"console.env.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                )
                shutil.copy2(str(self.env_path), str(bak))

            os.replace(str(tmp), str(self.env_path))
        except OSError as e:
            result["success"] = False
            result["errors"]["_file"] = str(e)
            return result

        return result

    # ── 恢复默认 ────────────────────────────────────────────────────

    def restore_defaults(self) -> dict:
        """从 .example 还原配置"""
        if not self.example_path.exists():
            # 从 FIELDS 生成默认配置
            lines = ["# Admin Console — 默认配置\n",
                     f"# 生成时间: {datetime.now().isoformat()}\n",
                     "# 请编辑本文件后重启控制台\n\n"]
            for key, label, default, _v in self.FIELDS.values():
                lines.append(f"# {label}\n")
                lines.append(f"{key}={default}\n\n")
            self.example_path.write_text("".join(lines), encoding="utf-8")

        shutil.copy2(str(self.example_path), str(self.env_path))
        return self.load()

    # ── 内部 ────────────────────────────────────────────────────────

    def _read_env_file(self) -> dict:
        """解析 .env 文件（支持 # 注释和引号）"""
        path = self.env_path
        if not path.exists():
            return {}
        result = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("\"'")
            result[key] = val
        return result

    def _format_env_lines(self, config: dict) -> list:
        """格式化 .env 行"""
        lines = ["# Admin Console Configuration\n",
                 "# Generated by admin_console\n\n"]
        for key, label, _default, _v in self.FIELDS.values():
            val = config.get(key, "")
            lines.append(f"# {label}\n")
            lines.append(f"{key}={val}\n\n")
        return lines

    def _validate(self, updates: dict) -> dict:
        """校验项，返回 {field: msg}"""
        errors = {}
        cfg = self.load()
        merged = {**cfg, **updates}

        for key, val in updates.items():
            if key not in {f[0] for f in self.FIELDS.values()}:
                errors[key] = f"未知配置项"
                continue

            # 检查必填
            if key in self.REQUIRED and not val.strip():
                errors[key] = "此项为必填"

            # 端口范围
            if key in ("CONSOLE_PORT", "SMTP_PORT"):
                if not val.strip():
                    continue
                try:
                    p = int(val)
                    if p < 1 or p > 65535:
                        errors[key] = f"端口 {p} 不在有效范围 1–65535"
                except ValueError:
                    errors[key] = "端口必须是数字"

            # 邮箱格式
            validator = None
            for _k, _l, _d, v in self.FIELDS.values():
                if _k == key:
                    validator = v
                    break
            if validator == "email" and val.strip():
                if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', val.strip()):
                    errors[key] = "邮箱格式不正确"

            # 路径存在性（APP_BASE 在非 dry-run 下检查）
            if key == "APP_BASE" and val.strip():
                p = Path(val)
                if not os.environ.get("DRY_RUN"):
                    if not p.exists():
                        errors[key] = f"路径 {val} 不存在"
                    elif not os.access(str(p), os.W_OK):
                        errors[key] = f"路径 {val} 不可写"

        return errors
