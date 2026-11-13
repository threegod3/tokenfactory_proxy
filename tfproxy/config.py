from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    api_keys: List[str] = Field(default_factory=list)
    allow_anonymous: bool = False
    log_level: str = "info"
    request_timeout_seconds: int = 300


class UpstreamConfig(BaseModel):
    base_url: str = "https://tokenfactory.kt.com"
    proxy: Optional[str] = "socks5h://127.0.0.1:7890"
    cookies_file: Optional[str] = "./data/cookies.json"
    cookies_save_file: Optional[str] = "./data/cookies.json"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
    tenant_id: str = ""
    workspace_id: str = ""
    chat_mode: str = "auto"  # auto|turns|openai
    reuse_conversation: bool = False
    verify_tls: bool = True
    connect_timeout_seconds: int = 20
    read_timeout_seconds: int = 300
    extra_headers: Dict[str, str] = Field(default_factory=dict)
    # 上游每账号每日对话配额（FREE_MEMBER 实测 10）
    daily_quota_per_account: int = 10


class AccountConfig(BaseModel):
    email: str
    password: str
    enabled: bool = True
    tenant_id: str = ""
    workspace_id: str = ""


class AuthConfig(BaseModel):
    recaptcha_mode: str = "playwright"  # playwright|manual|disabled
    recaptcha_site_keys: List[str] = Field(default_factory=lambda: [
        "6Ld6CAEuAAAAABMcfRzFiXfSsiPjCgehCytp1laq",
        "6LeZEBcuAAAAAMQOk0zy4wrzZPzUbBSeHRnD22eM",
    ])
    cloudmail_base_url: str = "https://mail.qjn.ccwu.cc"
    cloudmail_admin_email: str = ""
    cloudmail_admin_password: str = ""


class ModelsConfig(BaseModel):
    refresh_interval_seconds: int = 3600
    aliases: Dict[str, str] = Field(default_factory=lambda: {"*": "auto"})
    expose_all_upstream: bool = True


class ToolsConfig(BaseModel):
    enabled: bool = True
    compact_schemas: bool = True
    retry_on_invalid: bool = True


class MaintenanceConfig(BaseModel):
    enabled: bool = True
    # REFRESH_TOKEN 剩余少于该天数时，自动起浏览器重新登录换新 token
    renew_refresh_days: float = 3.0
    # 同时最多几个浏览器实例
    max_browser_concurrency: int = 2
    # 巡检间隔（秒）
    interval_seconds: int = 1800
    # 每轮最多续期多少个账号，剩余的留给下一轮
    max_renewals_per_cycle: int = 4


class StorageConfig(BaseModel):
    state_file: str = "./data/state.json"
    accounts_file: str = "./data/accounts.json"
    quota_file: str = "./data/quota.json"


class AdminConfig(BaseModel):
    enabled: bool = True
    token: str = ""


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    upstream: UpstreamConfig = Field(default_factory=UpstreamConfig)
    accounts: List[AccountConfig] = Field(default_factory=list)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)

    base_dir: Path = Field(default_factory=lambda: Path.cwd(), exclude=True)
    config_path: Optional[Path] = Field(default=None, exclude=True)

    def resolve_path(self, p: Optional[str]) -> Optional[Path]:
        if not p:
            return None
        path = Path(p)
        if not path.is_absolute():
            path = (self.base_dir / path).resolve()
        return path


def _deep_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Optional[str | os.PathLike] = None) -> AppConfig:
    data: Dict[str, Any] = {}
    cfg_path: Optional[Path] = None
    if path:
        cfg_path = Path(path).resolve()
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            if not isinstance(loaded, dict):
                raise ValueError("Config file must be a YAML mapping/object")
            data = loaded
    base_dir = cfg_path.parent if cfg_path else Path.cwd()

    # env overrides (minimal)
    env_port = os.getenv("TFPROXY_PORT")
    env_host = os.getenv("TFPROXY_HOST")
    env_key = os.getenv("TFPROXY_API_KEY")
    env_proxy = os.getenv("TFPROXY_UPSTREAM_PROXY")
    env_cookies = os.getenv("TFPROXY_COOKIES_FILE")
    env_log = os.getenv("TFPROXY_LOG_LEVEL")
    overrides: Dict[str, Any] = {"server": {}, "upstream": {}}
    if env_port:
        overrides["server"]["port"] = int(env_port)
    if env_host:
        overrides["server"]["host"] = env_host
    if env_key:
        overrides["server"]["api_keys"] = [env_key]
    if env_proxy is not None:
        overrides["upstream"]["proxy"] = env_proxy
    if env_cookies:
        overrides["upstream"]["cookies_file"] = env_cookies
    if env_log:
        overrides["server"]["log_level"] = env_log
    data = _deep_merge(data, {k: v for k, v in overrides.items() if v})

    cfg = AppConfig.model_validate(data)
    cfg.base_dir = base_dir
    cfg.config_path = cfg_path
    return cfg
