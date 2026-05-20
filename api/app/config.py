from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator


class AppConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    request_timeout_sec: float = 120.0
    docs_enabled: bool = True


class PollinationsConfig(BaseModel):
    base_url: str = "https://gen.pollinations.ai"
    api_keys: List[str] = Field(default_factory=list)
    default_image_model: str = "flux"
    use_proxy_2080: bool = False
    proxy_2080_url: str = "socks5://127.0.0.1:2080"
    trust_env_proxy: bool = False
    max_attempts_per_request: int = 6
    retry_status_codes: List[int] = Field(default_factory=lambda: [402, 429, 500, 502, 503, 504])
    retry_backoff_sec: float = 0.35
    cooldown_sec: float = 20.0

    @field_validator("api_keys")
    @classmethod
    def validate_keys(cls, value: List[str]) -> List[str]:
        cleaned = [v.strip() for v in value if str(v).strip()]
        if not cleaned:
            raise ValueError("At least one Pollinations API key is required")
        return cleaned


class ImageDefaults(BaseModel):
    n: int = 1
    size: str = "1024x1024"
    quality: str = "medium"
    response_format: str = "b64_json"


class AdminConfig(BaseModel):
    enabled: bool = True
    require_token: bool = False
    token: str = ""
    header_name: str = "x-admin-token"
    models_cache_ttl_sec: float = 180.0


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    pollinations: PollinationsConfig
    image_defaults: ImageDefaults = Field(default_factory=ImageDefaults)
    admin: AdminConfig = Field(default_factory=AdminConfig)


BOOL_TRUE = {"1", "true", "yes", "on"}


def _to_bool(raw: str) -> bool:
    return str(raw).strip().lower() in BOOL_TRUE


def _to_csv(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _set_nested(data: Dict[str, Any], path: str, value: Any) -> None:
    keys = path.split(".")
    cursor = data
    for key in keys[:-1]:
        if key not in cursor or not isinstance(cursor[key], dict):
            cursor[key] = {}
        cursor = cursor[key]
    cursor[keys[-1]] = value


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config file must be a mapping: {path}")
    return loaded


def _env_overrides(data: Dict[str, Any]) -> Dict[str, Any]:
    mapping: Dict[str, tuple[str, Any]] = {
        "APP_HOST": ("app.host", str),
        "APP_PORT": ("app.port", int),
        "APP_LOG_LEVEL": ("app.log_level", str),
        "APP_REQUEST_TIMEOUT_SEC": ("app.request_timeout_sec", float),
        "APP_DOCS_ENABLED": ("app.docs_enabled", _to_bool),
        "POLLINATIONS_BASE_URL": ("pollinations.base_url", str),
        "POLLINATIONS_DEFAULT_IMAGE_MODEL": ("pollinations.default_image_model", str),
        "POLLINATIONS_USE_PROXY_2080": ("pollinations.use_proxy_2080", _to_bool),
        "POLLINATIONS_PROXY_2080_URL": ("pollinations.proxy_2080_url", str),
        "POLLINATIONS_TRUST_ENV_PROXY": ("pollinations.trust_env_proxy", _to_bool),
        "POLLINATIONS_MAX_ATTEMPTS_PER_REQUEST": ("pollinations.max_attempts_per_request", int),
        "POLLINATIONS_RETRY_STATUS_CODES": (
            "pollinations.retry_status_codes",
            lambda x: [int(v) for v in _to_csv(x)],
        ),
        "POLLINATIONS_RETRY_BACKOFF_SEC": ("pollinations.retry_backoff_sec", float),
        "POLLINATIONS_KEY_COOLDOWN_SEC": ("pollinations.cooldown_sec", float),
        "IMAGE_DEFAULT_N": ("image_defaults.n", int),
        "IMAGE_DEFAULT_SIZE": ("image_defaults.size", str),
        "IMAGE_DEFAULT_QUALITY": ("image_defaults.quality", str),
        "IMAGE_DEFAULT_RESPONSE_FORMAT": ("image_defaults.response_format", str),
        "ADMIN_ENABLED": ("admin.enabled", _to_bool),
        "ADMIN_REQUIRE_TOKEN": ("admin.require_token", _to_bool),
        "ADMIN_TOKEN": ("admin.token", str),
        "ADMIN_HEADER_NAME": ("admin.header_name", str),
        "ADMIN_MODELS_CACHE_TTL_SEC": ("admin.models_cache_ttl_sec", float),
    }

    out = dict(data)
    for env_name, (path, caster) in mapping.items():
        raw = os.getenv(env_name)
        if raw is None or raw == "":
            continue
        _set_nested(out, path, caster(raw))

    keys_csv = os.getenv("POLLINATIONS_API_KEYS", "")
    single_key = os.getenv("POLLINATIONS_API_KEY", "")
    keys = _to_csv(keys_csv)
    if not keys and single_key.strip():
        keys = [single_key.strip()]
    if keys:
        _set_nested(out, "pollinations.api_keys", keys)

    proxy_url = os.getenv("POLLINATIONS_PROXY_URL", "").strip()
    if proxy_url:
        _set_nested(out, "pollinations.proxy_2080_url", proxy_url)

    return out


def load_settings(config_file: str | None = None) -> Settings:
    load_dotenv(override=False)

    config_path = Path(config_file or os.getenv("APP_CONFIG_FILE", "config/config.yml"))
    defaults = {
        "app": AppConfig().model_dump(),
        "pollinations": {
            "base_url": PollinationsConfig.model_fields["base_url"].default,
            "api_keys": [],
            "default_image_model": PollinationsConfig.model_fields["default_image_model"].default,
            "use_proxy_2080": PollinationsConfig.model_fields["use_proxy_2080"].default,
            "proxy_2080_url": PollinationsConfig.model_fields["proxy_2080_url"].default,
            "trust_env_proxy": PollinationsConfig.model_fields["trust_env_proxy"].default,
            "max_attempts_per_request": PollinationsConfig.model_fields["max_attempts_per_request"].default,
            "retry_status_codes": PollinationsConfig.model_fields["retry_status_codes"].default_factory(),
            "retry_backoff_sec": PollinationsConfig.model_fields["retry_backoff_sec"].default,
            "cooldown_sec": PollinationsConfig.model_fields["cooldown_sec"].default,
        },
        "image_defaults": ImageDefaults().model_dump(),
        "admin": AdminConfig().model_dump(),
    }

    file_data = _load_yaml(config_path)
    merged = _deep_merge(defaults, file_data)
    merged = _env_overrides(merged)

    try:
        return Settings.model_validate(merged)
    except ValidationError as exc:
        raise ValueError(f"Invalid config: {exc}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
