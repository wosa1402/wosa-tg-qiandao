from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pyrogram import errors

from tg_signer.core import get_client, get_proxy
from tg_signer.webapp.security import (
    issue_csrf_token,
    redirect_to_login,
    verify_csrf_token,
)
from tg_signer.webapp.settings import WebSettings
from tg_signer.webapp.store import AccountsStore, validate_name


@dataclass
class LoginState:
    login_id: str
    account_name: str
    phone_number: str
    phone_code_hash: str
    created_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _quote_segment(value: str) -> str:
    return quote(value, safe="")


def _get_templates(request: Request):
    return request.app.state.templates


def _require_login(request: Request):
    if request.session.get("logged_in") is not True:
        return redirect_to_login(request)
    return None


def _session_paths(sessions_dir: Path, account_name: str) -> list[Path]:
    return [
        sessions_dir / f"{account_name}.session_string",
        sessions_dir / f"{account_name}.session",
        sessions_dir / f"{account_name}.session-journal",
    ]


def _is_logged_in(sessions_dir: Path, account_name: str) -> bool:
    return any(p.exists() for p in _session_paths(sessions_dir, account_name))


def _render_login_start(
    request: Request, *, account_name: str, error: Optional[str] = None
) -> HTMLResponse:
    templates = _get_templates(request)
    return templates.TemplateResponse(
        request,
        "account_login_start.html",
        {
            "request": request,
            "account_name": account_name,
            "csrf_token": issue_csrf_token(request),
            "error": error,
        },
        status_code=400 if error else 200,
    )


def _render_login_verify(
    request: Request,
    *,
    account_name: str,
    login_id: str,
    notice: Optional[str] = None,
    error: Optional[str] = None,
) -> HTMLResponse:
    templates = _get_templates(request)
    return templates.TemplateResponse(
        request,
        "account_login_verify.html",
        {
            "request": request,
            "account_name": account_name,
            "login_id": login_id,
            "csrf_token": issue_csrf_token(request),
            "notice": notice,
            "error": error,
        },
        status_code=400 if error else 200,
    )


def _render_login_password(
    request: Request,
    *,
    account_name: str,
    login_id: str,
    error: Optional[str] = None,
) -> HTMLResponse:
    templates = _get_templates(request)
    return templates.TemplateResponse(
        request,
        "account_login_password.html",
        {
            "request": request,
            "account_name": account_name,
            "login_id": login_id,
            "csrf_token": issue_csrf_token(request),
            "error": error,
        },
        status_code=400 if error else 200,
    )


def _format_login_error(exc: Exception) -> str:
    def _err_cls(name: str):
        cls = getattr(errors, name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            return cls
        return None

    def _is_error(name: str) -> bool:
        cls = _err_cls(name)
        if cls and isinstance(exc, cls):
            return True
        return any(base.__name__ == name for base in exc.__class__.mro())

    if _is_error("FloodWait"):
        seconds = getattr(exc, "value", None)
        if seconds:
            return f"请求过于频繁，请等待 {seconds} 秒后再试"
        return "请求过于频繁，请稍后再试"
    if _is_error("PhoneNumberInvalid"):
        return "手机号格式不正确"
    if _is_error("PhoneNumberBanned"):
        return "该手机号已被 Telegram 封禁"
    if _is_error("PhoneNumberFlood"):
        return "手机号请求过于频繁，请稍后再试"
    if _is_error("PhoneCodeInvalid"):
        return "验证码不正确，请重新输入"
    if _is_error("PhoneCodeExpired"):
        return "验证码已过期，请重新发送验证码"
    if _is_error("PhoneCodeEmpty"):
        return "验证码不能为空"
    if _is_error("PhoneCodeHashEmpty"):
        return "验证码已失效，请重新发送验证码"
    if _is_error("PhoneCodeHashInvalid"):
        return "验证码已失效，请重新发送验证码"
    if _is_error("PasswordHashInvalid"):
        return "二次密码不正确，请重试"
    return str(exc)


router = APIRouter()


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    templates = _get_templates(request)
    settings: WebSettings = request.app.state.settings
    store: AccountsStore = request.app.state.accounts_store

    items = []
    for rec in store.list():
        items.append(
            {
                "account_name": rec.account_name,
                "created_at": rec.created_at,
                "last_login_at": rec.last_login_at,
                "last_error": rec.last_error,
                "logged_in": _is_logged_in(settings.sessions_dir, rec.account_name),
            }
        )
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "request": request,
            "items": items,
            "csrf_token": issue_csrf_token(request),
            "error": None,
            "form": None,
        },
    )


@router.post("/accounts")
async def create_account(
    request: Request, account_name: str = Form(""), csrf_token: str = Form("")
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    await _prune_login_sessions(request)
    templates = _get_templates(request)
    settings: WebSettings = request.app.state.settings
    store: AccountsStore = request.app.state.accounts_store
    try:
        account_name = validate_name(account_name, label="账号名")
    except ValueError as e:
        items = []
        for rec in store.list():
            items.append(
                {
                    "account_name": rec.account_name,
                    "created_at": rec.created_at,
                    "last_login_at": rec.last_login_at,
                    "last_error": rec.last_error,
                    "logged_in": _is_logged_in(settings.sessions_dir, rec.account_name),
                }
            )
        return templates.TemplateResponse(
            request,
            "accounts.html",
            {
                "request": request,
                "items": items,
                "csrf_token": issue_csrf_token(request),
                "error": str(e),
                "form": {"account_name": account_name},
            },
            status_code=400,
        )
    store.ensure(account_name)
    return RedirectResponse(url="/accounts", status_code=303)


@router.get("/accounts/{account_name}/login", response_class=HTMLResponse)
async def login_start_page(request: Request, account_name: str):
    redirect = _require_login(request)
    if redirect:
        return redirect
    account_name = validate_name(account_name, label="账号名")
    return _render_login_start(request, account_name=account_name)


@router.post("/accounts/{account_name}/login/start")
async def login_start(
    request: Request,
    account_name: str,
    phone_number: str = Form(""),
    csrf_token: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)

    account_name = validate_name(account_name, label="账号名")
    phone_number = (phone_number or "").strip()
    if not phone_number:
        return _render_login_start(request, account_name=account_name, error="手机号不能为空")

    settings: WebSettings = request.app.state.settings
    store: AccountsStore = request.app.state.accounts_store
    store.ensure(account_name)

    await _prune_login_sessions(request)
    for existing_id, state in list(request.app.state.login_sessions.items()):
        if state.account_name == account_name:
            await _cleanup_login_session(request, existing_id)

    proxy = get_proxy()
    client = get_client(account_name, proxy, workdir=settings.sessions_dir)

    login_id = str(uuid.uuid4())
    try:
        if not getattr(client, "is_connected", False):
            await client.connect()
        sent = await client.send_code(phone_number)
        phone_code_hash = sent.phone_code_hash
    except Exception as e:
        message = _format_login_error(e)
        store.mark_error(account_name, message)
        try:
            if getattr(client, "is_connected", False):
                await client.disconnect()
        except Exception:
            pass
        return _render_login_start(request, account_name=account_name, error=message)

    login_sessions: dict[str, LoginState] = request.app.state.login_sessions
    login_sessions[login_id] = LoginState(
        login_id=login_id,
        account_name=account_name,
        phone_number=phone_number,
        phone_code_hash=phone_code_hash,
        created_at=_now(),
    )

    login_clients: dict[str, object] = request.app.state.login_clients
    login_clients[login_id] = client

    return RedirectResponse(
        url=f"/accounts/{_quote_segment(account_name)}/login/verify?login_id={login_id}",
        status_code=303,
    )


def _get_login_state(
    request: Request, login_id: str, *, account_name: str
) -> Optional[LoginState]:
    login_sessions: dict[str, LoginState] = request.app.state.login_sessions
    state = login_sessions.get(login_id)
    if not state or state.account_name != account_name:
        return None
    if _now() - state.created_at > timedelta(minutes=10):
        login_sessions.pop(login_id, None)
        return None
    return state


async def _cleanup_login_session(request: Request, login_id: str) -> None:
    request.app.state.login_sessions.pop(login_id, None)
    login_clients = getattr(request.app.state, "login_clients", None)
    if not isinstance(login_clients, dict):
        return
    client = login_clients.pop(login_id, None)
    if client is None:
        return
    try:
        if getattr(client, "is_connected", False):
            await client.disconnect()
    except Exception:
        pass


async def _prune_login_sessions(request: Request) -> None:
    login_sessions: dict[str, LoginState] = request.app.state.login_sessions
    now = _now()
    expired_ids = [
        login_id
        for login_id, state in login_sessions.items()
        if now - state.created_at > timedelta(minutes=10)
    ]
    for login_id in expired_ids:
        await _cleanup_login_session(request, login_id)


@router.get("/accounts/{account_name}/login/verify", response_class=HTMLResponse)
async def login_verify_page(request: Request, account_name: str, login_id: str = ""):
    await _prune_login_sessions(request)
    redirect = _require_login(request)
    if redirect:
        return redirect
    account_name = validate_name(account_name, label="账号名")
    state = _get_login_state(request, login_id, account_name=account_name)
    if not state:
        await _cleanup_login_session(request, login_id)
        return RedirectResponse(
            url=f"/accounts/{_quote_segment(account_name)}/login", status_code=303
        )
    return _render_login_verify(request, account_name=account_name, login_id=login_id)


@router.post("/accounts/{account_name}/login/verify")
async def login_verify(
    request: Request,
    account_name: str,
    login_id: str = Form(""),
    code: str = Form(""),
    csrf_token: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)

    account_name = validate_name(account_name, label="账号名")
    state = _get_login_state(request, login_id, account_name=account_name)
    if not state:
        await _cleanup_login_session(request, login_id)
        return RedirectResponse(
            url=f"/accounts/{_quote_segment(account_name)}/login", status_code=303
        )

    store: AccountsStore = request.app.state.accounts_store

    login_clients = getattr(request.app.state, "login_clients", {})
    client = login_clients.get(login_id) if isinstance(login_clients, dict) else None
    if client is None:
        await _cleanup_login_session(request, login_id)
        return _render_login_start(
            request, account_name=account_name, error="登录会话已失效，请重新发送验证码"
        )

    code_value = re.sub(r"\D", "", (code or ""))
    if not code_value:
        return _render_login_verify(
            request, account_name=account_name, login_id=login_id, error="验证码不能为空"
        )

    try:
        if not getattr(client, "is_connected", False):
            await client.connect()
        try:
            await client.sign_in(state.phone_number, state.phone_code_hash, code_value)
        except errors.SessionPasswordNeeded:
            return RedirectResponse(
                url=f"/accounts/{_quote_segment(account_name)}/login/password?login_id={login_id}",
                status_code=303,
            )
        except Exception as e:
            message = _format_login_error(e)
            store.mark_error(account_name, message)
            return _render_login_verify(
                request, account_name=account_name, login_id=login_id, error=message
            )

        await client.save_session_string()
        store.mark_login_success(account_name)
        backup_manager = getattr(request.app.state, "backup_manager", None)
        if backup_manager:
            await backup_manager.schedule_push("login")
    except Exception as e:
        message = _format_login_error(e)
        store.mark_error(account_name, message)
        return _render_login_verify(
            request, account_name=account_name, login_id=login_id, error=message
        )

    await _cleanup_login_session(request, login_id)
    return RedirectResponse(url="/accounts", status_code=303)


@router.post("/accounts/{account_name}/login/resend")
async def login_resend(
    request: Request,
    account_name: str,
    login_id: str = Form(""),
    csrf_token: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    await _prune_login_sessions(request)

    account_name = validate_name(account_name, label="账号名")
    state = _get_login_state(request, login_id, account_name=account_name)
    if not state:
        await _cleanup_login_session(request, login_id)
        return RedirectResponse(
            url=f"/accounts/{_quote_segment(account_name)}/login", status_code=303
        )

    store: AccountsStore = request.app.state.accounts_store
    login_clients = getattr(request.app.state, "login_clients", {})
    client = login_clients.get(login_id) if isinstance(login_clients, dict) else None
    if client is None:
        await _cleanup_login_session(request, login_id)
        return _render_login_start(
            request, account_name=account_name, error="登录会话已失效，请重新发送验证码"
        )

    try:
        if not getattr(client, "is_connected", False):
            await client.connect()
        sent = await client.send_code(state.phone_number)
        state.phone_code_hash = sent.phone_code_hash
        state.created_at = _now()
    except Exception as e:
        message = _format_login_error(e)
        store.mark_error(account_name, message)
        return _render_login_verify(
            request, account_name=account_name, login_id=login_id, error=message
        )

    return _render_login_verify(
        request,
        account_name=account_name,
        login_id=login_id,
        notice="已重新发送验证码，请使用最新验证码",
    )


@router.get("/accounts/{account_name}/login/password", response_class=HTMLResponse)
async def login_password_page(request: Request, account_name: str, login_id: str = ""):
    await _prune_login_sessions(request)
    redirect = _require_login(request)
    if redirect:
        return redirect
    account_name = validate_name(account_name, label="账号名")
    state = _get_login_state(request, login_id, account_name=account_name)
    if not state:
        await _cleanup_login_session(request, login_id)
        return RedirectResponse(
            url=f"/accounts/{_quote_segment(account_name)}/login", status_code=303
        )
    login_clients = getattr(request.app.state, "login_clients", {})
    client = login_clients.get(login_id) if isinstance(login_clients, dict) else None
    if client is None:
        await _cleanup_login_session(request, login_id)
        return RedirectResponse(
            url=f"/accounts/{_quote_segment(account_name)}/login", status_code=303
        )
    return _render_login_password(request, account_name=account_name, login_id=login_id)


@router.post("/accounts/{account_name}/login/password")
async def login_password(
    request: Request,
    account_name: str,
    login_id: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)

    account_name = validate_name(account_name, label="账号名")
    state = _get_login_state(request, login_id, account_name=account_name)
    if not state:
        await _cleanup_login_session(request, login_id)
        return RedirectResponse(
            url=f"/accounts/{_quote_segment(account_name)}/login", status_code=303
        )

    store: AccountsStore = request.app.state.accounts_store

    login_clients = getattr(request.app.state, "login_clients", {})
    client = login_clients.get(login_id) if isinstance(login_clients, dict) else None
    if client is None:
        await _cleanup_login_session(request, login_id)
        return _render_login_start(
            request, account_name=account_name, error="登录会话已失效，请重新发送验证码"
        )
    try:
        if not getattr(client, "is_connected", False):
            await client.connect()
        password_value = (password or "").strip()
        if not password_value:
            return _render_login_password(
                request, account_name=account_name, login_id=login_id, error="二次密码不能为空"
            )
        await client.check_password(password_value)
        await client.save_session_string()
        store.mark_login_success(account_name)
        backup_manager = getattr(request.app.state, "backup_manager", None)
        if backup_manager:
            await backup_manager.schedule_push("login")
    except Exception as e:
        message = _format_login_error(e)
        store.mark_error(account_name, message)
        return _render_login_password(
            request, account_name=account_name, login_id=login_id, error=message
        )

    await _cleanup_login_session(request, login_id)
    return RedirectResponse(url="/accounts", status_code=303)


@router.post("/accounts/{account_name}/logout")
async def logout_account(request: Request, account_name: str, csrf_token: str = Form("")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)

    account_name = validate_name(account_name, label="账号名")
    settings: WebSettings = request.app.state.settings
    store: AccountsStore = request.app.state.accounts_store

    proxy = get_proxy()
    client = get_client(account_name, proxy, workdir=settings.sessions_dir)
    try:
        is_authorized = await client.connect()
        if is_authorized:
            await client.log_out()
        else:
            for p in _session_paths(settings.sessions_dir, account_name):
                if p.exists():
                    p.unlink()
        store.mark_logout(account_name)
        backup_manager = getattr(request.app.state, "backup_manager", None)
        if backup_manager:
            await backup_manager.schedule_push("logout")
    except Exception as e:
        store.mark_error(account_name, str(e))
        raise
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return RedirectResponse(url="/accounts", status_code=303)
