from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class WebDavSettings:
    url: str
    username: str
    password: str
    remote_path: str
    interval_seconds: int = 300
    encryption_key: Optional[str] = None


@dataclass
class WebConfig:
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    proxy: Optional[str] = None
    admin_username: str = "admin"
    admin_password: Optional[str] = None
    session_secret: str = ""
    cookie_secure: bool = False
    webdav: Optional[WebDavSettings] = None


@dataclass(frozen=True)
class WebSettings:
    data_dir: Path
    workdir: Path
    sessions_dir: Path
    runs_dir: Path
    config_path: Path


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8") or "{}")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_settings() -> WebSettings:
    data_dir = Path(os.environ.get("TG_SIGNER_DATA_DIR", "./.tg-signer-web")).resolve()
    workdir = data_dir / "workdir"
    sessions_dir = data_dir / "sessions"
    runs_dir = data_dir / "runs"
    config_path = data_dir / "web.config.json"

    return WebSettings(
        data_dir=data_dir,
        workdir=workdir,
        sessions_dir=sessions_dir,
        runs_dir=runs_dir,
        config_path=config_path,
    )


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return int(value)


def _coerce_int(value: Optional[object]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def load_config(settings: WebSettings) -> WebConfig:
    data = _read_json(settings.config_path)

    api_id = _parse_int(_env("TG_API_ID")) or _coerce_int(data.get("api_id"))
    api_hash = _env("TG_API_HASH") or data.get("api_hash")
    proxy = _env("TG_PROXY") or data.get("proxy")

    admin_username = data.get("admin_username") or "admin"
    admin_password = _env("TG_SIGNER_WEB_ADMIN_PASSWORD") or data.get("admin_password")
    session_secret = _env("TG_SIGNER_WEB_SESSION_SECRET") or data.get(
        "session_secret", ""
    )
    cookie_secure = (
        os.environ.get("TG_SIGNER_WEB_COOKIE_SECURE", "").strip() != "0"
        if os.environ.get("TG_SIGNER_WEB_COOKIE_SECURE") is not None
        else bool(data.get("cookie_secure", False))
    )

    webdav = None
    webdav_data = data.get("webdav") or {}
    webdav_url = _env("TG_SIGNER_BACKUP_WEBDAV_URL") or webdav_data.get("url")
    webdav_username = _env("TG_SIGNER_BACKUP_WEBDAV_USERNAME") or webdav_data.get(
        "username"
    )
    webdav_password = _env("TG_SIGNER_BACKUP_WEBDAV_PASSWORD") or webdav_data.get(
        "password"
    )
    webdav_remote_path = _env("TG_SIGNER_BACKUP_REMOTE_PATH") or webdav_data.get(
        "remote_path"
    )
    webdav_interval_seconds = _env("TG_SIGNER_BACKUP_INTERVAL_SECONDS") or str(
        webdav_data.get("interval_seconds", "")
    )
    webdav_encryption_key = _env("TG_SIGNER_BACKUP_ENCRYPTION_KEY") or webdav_data.get(
        "encryption_key"
    )

    if any([webdav_url, webdav_username, webdav_password, webdav_remote_path]):
        interval = 300
        if webdav_interval_seconds:
            interval = int(webdav_interval_seconds)
        webdav = WebDavSettings(
            url=str(webdav_url),
            username=str(webdav_username),
            password=str(webdav_password),
            remote_path=str(webdav_remote_path),
            interval_seconds=interval,
            encryption_key=webdav_encryption_key or None,
        )

    if not session_secret:
        session_secret = secrets.token_urlsafe(48)
        data["session_secret"] = session_secret
        data.setdefault("cookie_secure", cookie_secure)
        _write_json(settings.config_path, data)

    return WebConfig(
        api_id=api_id,
        api_hash=api_hash,
        proxy=proxy,
        admin_username=admin_username,
        admin_password=admin_password,
        session_secret=session_secret,
        cookie_secure=cookie_secure,
        webdav=webdav,
    )


def save_config(settings: WebSettings, config: WebConfig) -> None:
    data = {
        "api_id": config.api_id,
        "api_hash": config.api_hash,
        "proxy": config.proxy,
        "admin_username": config.admin_username,
        "admin_password": config.admin_password,
        "session_secret": config.session_secret,
        "cookie_secure": config.cookie_secure,
        "webdav": None,
    }
    if config.webdav:
        data["webdav"] = {
            "url": config.webdav.url,
            "username": config.webdav.username,
            "password": config.webdav.password,
            "remote_path": config.webdav.remote_path,
            "interval_seconds": config.webdav.interval_seconds,
            "encryption_key": config.webdav.encryption_key,
        }
    _write_json(settings.config_path, data)
