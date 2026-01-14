from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from tg_signer.webapp.backup import WebDavBackupManager
from tg_signer.webapp.manager import WorkerManager
from tg_signer.webapp.routes.accounts import router as accounts_router
from tg_signer.webapp.routes.backup import router as backup_router
from tg_signer.webapp.routes.runs import router as runs_router
from tg_signer.webapp.routes.tasks import router as tasks_router
from tg_signer.webapp.security import (
    issue_csrf_token,
    login,
    logout,
    redirect_to_login,
    verify_csrf_token,
)
from tg_signer.webapp.settings import (
    WebConfig,
    WebDavSettings,
    WebSettings,
    load_config,
    load_settings,
    save_config,
)
from tg_signer.webapp.store import AccountsStore, RunsStore, TasksStore


def _ensure_dirs(*dirs: Path) -> None:
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def _parse_optional_int(value: str, *, label: str, errors: list[str]) -> Optional[int]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        errors.append(f"{label} 必须是整数")
        return None


def _apply_runtime_env(config: WebConfig) -> None:
    if config.api_id:
        os.environ["TG_API_ID"] = str(config.api_id)
    if config.api_hash:
        os.environ["TG_API_HASH"] = str(config.api_hash)
    if config.proxy:
        os.environ["TG_PROXY"] = config.proxy


def _ensure_admin_config(
    settings: WebSettings, config: WebConfig, logger: logging.Logger
) -> WebConfig:
    changed = False
    if not config.admin_username:
        config.admin_username = "admin"
        changed = True
    if not config.admin_password:
        config.admin_password = secrets.token_urlsafe(16)
        changed = True
        logger.warning(
            "首次启动已生成管理员密码: %s (用户: %s)，请尽快登录后修改。",
            config.admin_password,
            config.admin_username,
        )
    if changed:
        save_config(settings, config)
    return config


def _require_login(request: Request):
    if request.session.get("logged_in") is not True:
        return redirect_to_login(request)
    return None


def _needs_settings_guidance() -> bool:
    return not (os.environ.get("TG_API_ID") and os.environ.get("TG_API_HASH"))


def _sanitize_next_path(next_path: str) -> str:
    next_path = (next_path or "/").strip()
    if not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return quote(next_path, safe="/?&=%")


def create_app(settings: Optional[WebSettings] = None) -> FastAPI:
    settings = settings or load_settings()
    _ensure_dirs(settings.data_dir, settings.workdir, settings.sessions_dir, settings.runs_dir)
    config = load_config(settings)

    base_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(base_dir / "templates"))

    app = FastAPI(title="TG Signer Web")
    app.state.settings = settings
    app.state.templates = templates
    app.state.accounts_store = AccountsStore(settings.data_dir / "accounts.json")
    app.state.login_sessions = {}
    app.state.login_clients = {}
    app.state.tasks_store = TasksStore(settings.workdir)
    app.state.runs_store = RunsStore(settings.data_dir / "runs.json")
    app.state.worker_manager = WorkerManager(
        settings,
        tasks_store=app.state.tasks_store,
        runs_store=app.state.runs_store,
    )
    app.state.backup_manager = None
    app.state.backup_task = None
    app.state.logger = logging.getLogger("uvicorn.error")

    config = _ensure_admin_config(settings, config, app.state.logger)
    app.state.web_config = config
    _apply_runtime_env(config)

    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret,
        same_site="lax",
        https_only=config.cookie_secure,
    )

    app.include_router(accounts_router)
    app.include_router(tasks_router)
    app.include_router(runs_router)
    app.include_router(backup_router)

    async def _auto_start_enabled_tasks() -> None:
        if _needs_settings_guidance():
            app.state.logger.warning("未配置 Telegram API，跳过自动启动启用任务。")
            return

        tasks_store: TasksStore = app.state.tasks_store
        manager: WorkerManager = app.state.worker_manager
        sessions_dir: Path = app.state.settings.sessions_dir

        accounts: set[str] = {
            t.account_name for t in tasks_store.list() if t.enabled and t.account_name
        }
        for account_name in sorted(accounts):
            candidates = [
                sessions_dir / f"{account_name}.session_string",
                sessions_dir / f"{account_name}.session",
            ]
            if not any(p.exists() for p in candidates):
                continue
            try:
                await manager.ensure_account_worker(account_name)
                await manager.reload_account(account_name)
            except Exception as e:
                app.state.logger.warning(
                    "自动启动账号 worker 失败：account=%s err=%s", account_name, e
                )

    @app.exception_handler(PermissionError)
    async def _permission_error_handler(request: Request, exc: PermissionError):
        message = str(exc) or "permission denied"
        if message != "csrf validation failed":
            return HTMLResponse("权限不足", status_code=403)

        cfg: WebConfig = request.app.state.web_config
        hint = "CSRF 校验失败：页面可能已过期，请刷新后重试。"
        if cfg.cookie_secure and request.url.scheme != "https":
            hint = (
                "CSRF 校验失败：当前通过 HTTP 访问，但启用了 Cookie Secure（仅 HTTPS 发送 Cookie），"
                "浏览器不会携带会话 Cookie。请改用 HTTPS 访问，或将配置文件 "
                f"{settings.config_path} 中的 cookie_secure 改为 0 后重启。"
            )

        if request.url.path == "/login":
            csrf = issue_csrf_token(request)
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "request": request,
                    "next": "/",
                    "csrf_token": csrf,
                    "error": hint,
                    "admin_username": cfg.admin_username,
                },
                status_code=403,
            )

        return HTMLResponse(hint, status_code=403)

    @app.exception_handler(ValueError)
    async def _value_error_handler(request: Request, exc: ValueError) -> Response:
        show_nav = bool(request.session.get("logged_in") is True)
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "request": request,
                "csrf_token": issue_csrf_token(request),
                "show_nav": show_nav,
                "message": str(exc) or "参数错误",
                "detail": None,
                "back_url": request.headers.get("referer", "/"),
            },
            status_code=400,
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> Response:
        show_nav = bool(request.session.get("logged_in") is True)
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "request": request,
                "csrf_token": issue_csrf_token(request),
                "show_nav": show_nav,
                "message": "请求参数不合法，请刷新页面后重试",
                "detail": None,
                "back_url": request.headers.get("referer", "/"),
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        app.state.logger.exception("Unhandled error: %s", exc)
        show_nav = bool(request.session.get("logged_in") is True)
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "request": request,
                "csrf_token": issue_csrf_token(request),
                "show_nav": show_nav,
                "message": "发生内部错误，请查看终端日志",
                "detail": None,
                "back_url": request.headers.get("referer", "/"),
            },
            status_code=500,
        )

    @app.middleware("http")
    async def _settings_guard(request: Request, call_next):
        if not _needs_settings_guidance():
            return await call_next(request)

        path = request.url.path
        if path in {"/healthz", "/login", "/logout"} or path.startswith("/settings"):
            return await call_next(request)

        return RedirectResponse(url="/settings?guide=1", status_code=303)

    @app.on_event("startup")
    async def _startup():
        cfg: WebConfig = app.state.web_config
        if cfg.webdav:
            backup_manager = WebDavBackupManager(settings, cfg.webdav)
            app.state.backup_manager = backup_manager
            app.state.worker_manager.set_backup_scheduler(backup_manager)
            await backup_manager.pull_if_exists()
            app.state.backup_task = asyncio.create_task(backup_manager.run_scheduler())
        await _auto_start_enabled_tasks()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if request.session.get("logged_in") is not True:
            return redirect_to_login(request)
        if _needs_settings_guidance():
            return RedirectResponse(url="/settings?guide=1", status_code=303)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "request": request,
                "csrf_token": issue_csrf_token(request),
            },
        )

    @app.get("/login", response_class=HTMLResponse)
    async def show_login(request: Request, next: str = "/"):
        csrf_token = issue_csrf_token(request)
        config: WebConfig = request.app.state.web_config
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "next": next,
                "csrf_token": csrf_token,
                "admin_username": config.admin_username,
            },
        )

    @app.post("/login")
    async def do_login(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        next: str = Form("/"),
        csrf_token: str = Form(""),
    ):
        verify_csrf_token(request, csrf_token)
        config: WebConfig = request.app.state.web_config
        if (username or "").strip() != (config.admin_username or "admin"):
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "request": request,
                    "next": next,
                    "csrf_token": issue_csrf_token(request),
                    "error": "用户名或密码错误",
                    "admin_username": config.admin_username,
                },
                status_code=401,
            )
        if password != (config.admin_password or ""):
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "request": request,
                    "next": next,
                    "csrf_token": issue_csrf_token(request),
                    "error": "用户名或密码错误",
                    "admin_username": config.admin_username,
                },
                status_code=401,
            )
        login(request)
        return RedirectResponse(url=_sanitize_next_path(next), status_code=303)

    @app.post("/logout")
    async def do_logout(request: Request, csrf_token: str = Form("")):
        verify_csrf_token(request, csrf_token)
        logout(request)
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, ok: str = "", guide: str = ""):
        redirect = _require_login(request)
        if redirect:
            return redirect
        config: WebConfig = request.app.state.web_config
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "request": request,
                "csrf_token": issue_csrf_token(request),
                "config": config,
                "ok": ok == "1",
                "guide": guide == "1",
                "form": None,
            },
        )

    @app.post("/settings")
    async def settings_save(
        request: Request,
        csrf_token: str = Form(""),
        api_id: str = Form(""),
        api_hash: str = Form(""),
        proxy: str = Form(""),
        admin_username: str = Form(""),
        admin_password: str = Form(""),
        session_secret: str = Form(""),
        cookie_secure: str = Form(""),
        webdav_url: str = Form(""),
        webdav_username: str = Form(""),
        webdav_password: str = Form(""),
        webdav_remote_path: str = Form(""),
        webdav_interval_seconds: str = Form(""),
        webdav_encryption_key: str = Form(""),
    ):
        redirect = _require_login(request)
        if redirect:
            return redirect
        verify_csrf_token(request, csrf_token)

        current: WebConfig = request.app.state.web_config
        errors: list[str] = []
        api_id_value = _parse_optional_int(api_id, label="API ID", errors=errors)
        api_hash_value = (api_hash or "").strip()
        if api_id_value or api_hash_value:
            if not api_id_value or not api_hash_value:
                errors.append("API ID 和 API HASH 需要同时填写")

        admin_username_value = (admin_username or "").strip() or "admin"
        admin_password_value = (admin_password or "").strip()

        session_secret_value = (session_secret or "").strip()
        cookie_raw = cookie_secure.strip()
        cookie_secure_value = (
            current.cookie_secure if cookie_raw == "" else cookie_raw != "0"
        )

        webdav_url = (webdav_url or "").strip()
        webdav_username = (webdav_username or "").strip()
        webdav_password = (webdav_password or "").strip()
        webdav_remote_path = (webdav_remote_path or "").strip()
        webdav_interval_seconds = (webdav_interval_seconds or "").strip()
        webdav_encryption_key = (webdav_encryption_key or "").strip()

        webdav = None
        if any([webdav_url, webdav_username, webdav_password, webdav_remote_path]):
            if not all([webdav_url, webdav_username, webdav_password, webdav_remote_path]):
                errors.append("WebDAV 配置不完整，请完整填写或留空")
            else:
                interval = 300
                if webdav_interval_seconds:
                    parsed = _parse_optional_int(
                        webdav_interval_seconds, label="WebDAV 间隔", errors=errors
                    )
                    if parsed:
                        interval = parsed
                webdav = WebDavSettings(
                    url=webdav_url,
                    username=webdav_username,
                    password=webdav_password,
                    remote_path=webdav_remote_path,
                    interval_seconds=interval,
                    encryption_key=webdav_encryption_key or None,
                )

        if errors:
            return templates.TemplateResponse(
                request,
                "settings.html",
                {
                    "request": request,
                    "csrf_token": issue_csrf_token(request),
                    "errors": errors,
                    "config": request.app.state.web_config,
                    "form": {
                        "api_id": api_id,
                        "api_hash": api_hash,
                        "proxy": proxy,
                        "admin_username": admin_username_value,
                        "admin_password": admin_password_value,
                        "session_secret": session_secret_value,
                        "cookie_secure": cookie_secure_value,
                        "webdav_url": webdav_url,
                        "webdav_username": webdav_username,
                        "webdav_password": webdav_password,
                        "webdav_remote_path": webdav_remote_path,
                        "webdav_interval_seconds": webdav_interval_seconds,
                        "webdav_encryption_key": webdav_encryption_key,
                    },
                },
                status_code=400,
            )

        new_config = WebConfig(
            api_id=api_id_value or current.api_id,
            api_hash=api_hash_value or current.api_hash,
            proxy=(proxy or "").strip() or None,
            admin_username=admin_username_value,
            admin_password=admin_password_value or current.admin_password,
            session_secret=session_secret_value or current.session_secret,
            cookie_secure=cookie_secure_value,
            webdav=webdav,
        )
        save_config(settings, new_config)
        request.app.state.web_config = new_config
        _apply_runtime_env(new_config)

        if request.app.state.backup_manager:
            request.app.state.backup_manager.stop()
            request.app.state.backup_manager = None
            request.app.state.worker_manager.set_backup_scheduler(None)
            request.app.state.backup_task = None

        if new_config.webdav:
            backup_manager = WebDavBackupManager(settings, new_config.webdav)
            request.app.state.backup_manager = backup_manager
            request.app.state.worker_manager.set_backup_scheduler(backup_manager)
            request.app.state.backup_task = asyncio.create_task(
                backup_manager.run_scheduler()
            )
            await backup_manager.schedule_push("settings")

        return RedirectResponse(url="/settings?ok=1", status_code=303)

    return app


app = create_app()
