from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from tg_signer.config import SignConfigV3
from tg_signer.core import get_client, get_proxy
from tg_signer.webapp.manager import StartRunRequest, WorkerManager
from tg_signer.webapp.security import (
    issue_csrf_token,
    redirect_to_login,
    verify_csrf_token,
)
from tg_signer.webapp.settings import WebSettings
from tg_signer.webapp.store import AccountsStore, TasksStore, validate_name

router = APIRouter()


def _get_templates(request: Request):
    return request.app.state.templates


def _require_login(request: Request):
    if request.session.get("logged_in") is not True:
        return redirect_to_login(request)
    return None


def _quote_segment(value: str) -> str:
    return quote(value, safe="")


def _validate_signer_config(raw: Any) -> dict[str, Any]:
    loaded = SignConfigV3.load(raw)
    if not loaded:
        raise ValueError("配置不合法：无法匹配当前/旧版本配置结构")
    config, _from_old = loaded
    return config.to_jsonable()


def _session_paths(sessions_dir: Path, account_name: str) -> list[Path]:
    return [
        sessions_dir / f"{account_name}.session_string",
        sessions_dir / f"{account_name}.session",
        sessions_dir / f"{account_name}.session-journal",
    ]


def _is_account_logged_in(settings: WebSettings, account_name: str) -> bool:
    return any(p.exists() for p in _session_paths(settings.sessions_dir, account_name))


def _format_chat_label(item: dict[str, Any]) -> str:
    chat_id = item.get("id")
    chat_type = item.get("type") or "-"
    title = (item.get("title") or "").strip()
    username = (item.get("username") or "").strip()
    first_name = (item.get("first_name") or "").strip()
    last_name = (item.get("last_name") or "").strip()

    display = title
    if not display:
        display = " ".join([p for p in [first_name, last_name] if p])
    if not display and username:
        display = f"@{username}"
    if not display:
        display = "(未命名对话)"

    suffix = []
    if username:
        suffix.append(f"@{username}")
    suffix.append(f"type={chat_type}")
    suffix.append(f"id={chat_id}")
    return f"{display} ({', '.join(suffix)})"


async def _fetch_recent_chats(
    settings: WebSettings, account_name: str, *, limit: int = 50
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    proxy = get_proxy()
    client = get_client(account_name, proxy, workdir=settings.sessions_dir)

    items: list[dict[str, Any]] = []
    try:
        if not _is_account_logged_in(settings, account_name):
            return [], ["账号未登录：请先在 /accounts 完成 Telegram 登录"]

        if not getattr(client, "is_connected", False):
            await client.connect()

        async for dialog in client.get_dialogs(limit):
            chat = dialog.chat
            items.append(
                {
                    "id": chat.id,
                    "title": chat.title,
                    "type": str(chat.type),
                    "username": chat.username,
                    "first_name": chat.first_name,
                    "last_name": chat.last_name,
                }
            )
    except Exception as e:
        errors.append(f"拉取最近对话失败：{e}")
    finally:
        try:
            if getattr(client, "is_connected", False):
                await client.disconnect()
        except Exception:
            pass

    return items, errors


def _parse_optional_int(value: str, *, label: str, errors: list[str]) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        errors.append(f"{label} 必须是整数")
        return None


def _parse_optional_float(value: str, *, label: str, errors: list[str]) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        errors.append(f"{label} 必须是数字")
        return None


def _build_actions_from_form(form: dict[str, str], *, errors: list[str]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for idx in range(1, 7):
        kind = (form.get(f"action_{idx}_type") or "").strip()
        if not kind:
            continue
        value = (form.get(f"action_{idx}_value") or "").strip()

        if kind == "send_text":
            if not value:
                errors.append(f"第 {idx} 个动作：发送文本不能为空")
                continue
            actions.append({"action": 1, "text": value})
        elif kind == "send_dice":
            if not value:
                value = "🎲"
            actions.append({"action": 2, "dice": value})
        elif kind == "click_text":
            if not value:
                errors.append(f"第 {idx} 个动作：按钮文本不能为空")
                continue
            actions.append({"action": 3, "text": value})
        elif kind == "choose_image":
            actions.append({"action": 4})
        elif kind == "reply_calc":
            actions.append({"action": 5})
        else:
            errors.append(f"第 {idx} 个动作：不支持的类型 {kind}")

    if not actions:
        errors.append("至少需要添加 1 个动作")
        return actions

    first_action = actions[0].get("action")
    if first_action not in {1, 2}:
        errors.append("第 1 个动作必须是「发送文本」或「发送骰子」")
    return actions


def _defaults_for_wizard() -> dict[str, Any]:
    form: dict[str, Any] = {
        "sign_at": "0 6 * * *",
        "random_seconds": "300",
        "sign_interval": "1",
        "chat_id": "",
        "chat_name": "",
        "delete_after": "",
        "action_interval": "1",
        "action_1_type": "send_text",
        "action_1_value": "checkin",
        "action_2_type": "click_text",
        "action_2_value": "签到",
    }
    for idx in range(3, 7):
        form.setdefault(f"action_{idx}_type", "")
        form.setdefault(f"action_{idx}_value", "")
    return form


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    templates = _get_templates(request)
    accounts_store: AccountsStore = request.app.state.accounts_store
    tasks_store: TasksStore = request.app.state.tasks_store

    accounts = [a.account_name for a in accounts_store.list()]
    tasks = [t.__dict__ for t in tasks_store.list()]
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "request": request,
            "accounts": accounts,
            "tasks": tasks,
            "csrf_token": issue_csrf_token(request),
            "error": None,
            "form": None,
        },
    )


@router.post("/tasks")
async def create_task(
    request: Request,
    task_name: str = Form(""),
    account_name: str = Form(""),
    csrf_token: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    templates = _get_templates(request)
    accounts_store: AccountsStore = request.app.state.accounts_store
    tasks_store: TasksStore = request.app.state.tasks_store
    try:
        task_name = validate_name(task_name, label="任务名")
        account_name = validate_name(account_name, label="账号名")
    except ValueError as e:
        accounts = [a.account_name for a in accounts_store.list()]
        tasks = [t.__dict__ for t in tasks_store.list()]
        return templates.TemplateResponse(
            request,
            "tasks.html",
            {
                "request": request,
                "accounts": accounts,
                "tasks": tasks,
                "csrf_token": issue_csrf_token(request),
                "error": str(e),
                "form": {"task_name": task_name, "account_name": account_name},
            },
            status_code=400,
        )

    tasks_store.ensure(task_name, account_name=account_name, type="signer", enabled=False)
    backup_manager = getattr(request.app.state, "backup_manager", None)
    if backup_manager:
        await backup_manager.schedule_push("task_create")
    return RedirectResponse(url="/tasks", status_code=303)


@router.get("/tasks/{task_name}/edit", response_class=HTMLResponse)
async def edit_task_page(request: Request, task_name: str, ok: str = ""):
    redirect = _require_login(request)
    if redirect:
        return redirect
    templates = _get_templates(request)
    tasks_store: TasksStore = request.app.state.tasks_store
    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if not task:
        return RedirectResponse(url="/tasks", status_code=303)
    config_text = tasks_store.read_config_text(task_name)
    return templates.TemplateResponse(
        request,
        "task_edit.html",
        {
            "request": request,
            "task": task,
            "config_text": config_text,
            "csrf_token": issue_csrf_token(request),
            "ok": ok == "1",
        },
    )


@router.get("/tasks/{task_name}/wizard", response_class=HTMLResponse)
async def task_wizard_page(request: Request, task_name: str, ok: str = ""):
    redirect = _require_login(request)
    if redirect:
        return redirect
    templates = _get_templates(request)
    settings: WebSettings = request.app.state.settings
    tasks_store: TasksStore = request.app.state.tasks_store

    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if not task:
        return RedirectResponse(url="/tasks", status_code=303)

    recent_chats, chat_errors = await _fetch_recent_chats(
        settings, task.account_name, limit=50
    )
    form = _defaults_for_wizard()

    return templates.TemplateResponse(
        request,
        "task_wizard.html",
        {
            "request": request,
            "task": task,
            "csrf_token": issue_csrf_token(request),
            "ok": ok == "1",
            "errors": chat_errors or None,
            "form": form,
            "recent_chats": [
                {"id": c["id"], "label": _format_chat_label(c)} for c in recent_chats
            ],
            "preview": None,
        },
    )


@router.post("/tasks/{task_name}/wizard", response_class=HTMLResponse)
async def task_wizard_save(
    request: Request,
    task_name: str,
    csrf_token: str = Form(""),
    sign_at: str = Form(""),
    random_seconds: str = Form(""),
    sign_interval: str = Form(""),
    chat_id: str = Form(""),
    chat_name: str = Form(""),
    delete_after: str = Form(""),
    action_interval: str = Form(""),
    action_1_type: str = Form(""),
    action_1_value: str = Form(""),
    action_2_type: str = Form(""),
    action_2_value: str = Form(""),
    action_3_type: str = Form(""),
    action_3_value: str = Form(""),
    action_4_type: str = Form(""),
    action_4_value: str = Form(""),
    action_5_type: str = Form(""),
    action_5_value: str = Form(""),
    action_6_type: str = Form(""),
    action_6_value: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    templates = _get_templates(request)
    settings: WebSettings = request.app.state.settings
    tasks_store: TasksStore = request.app.state.tasks_store

    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if not task:
        return RedirectResponse(url="/tasks", status_code=303)

    form = {
        "sign_at": sign_at,
        "random_seconds": random_seconds,
        "sign_interval": sign_interval,
        "chat_id": chat_id,
        "chat_name": chat_name,
        "delete_after": delete_after,
        "action_interval": action_interval,
        "action_1_type": action_1_type,
        "action_1_value": action_1_value,
        "action_2_type": action_2_type,
        "action_2_value": action_2_value,
        "action_3_type": action_3_type,
        "action_3_value": action_3_value,
        "action_4_type": action_4_type,
        "action_4_value": action_4_value,
        "action_5_type": action_5_type,
        "action_5_value": action_5_value,
        "action_6_type": action_6_type,
        "action_6_value": action_6_value,
    }

    errors: list[str] = []
    sign_at_value = (sign_at or "").strip() or "0 6 * * *"
    random_seconds_value = _parse_optional_int(
        random_seconds, label="签到随机秒数", errors=errors
    )
    sign_interval_value = _parse_optional_int(sign_interval, label="签到间隔秒数", errors=errors)
    delete_after_value = _parse_optional_int(delete_after, label="删除消息等待秒数", errors=errors)
    action_interval_value = _parse_optional_float(
        action_interval, label="动作间隔秒数", errors=errors
    )

    chat_id_value = (chat_id or "").strip()
    if not chat_id_value:
        errors.append("chat_id 不能为空")
    chat_id_int: int | None = None
    if chat_id_value:
        try:
            chat_id_int = int(chat_id_value)
        except ValueError:
            errors.append("chat_id 必须是整数（群/频道可能为负数）")

    actions = _build_actions_from_form(form, errors=errors)

    raw: dict[str, Any] = {
        "_version": 3,
        "chats": [],
        "sign_at": sign_at_value,
        "random_seconds": int(random_seconds_value or 0),
        "sign_interval": int(sign_interval_value or 1),
    }
    if chat_id_int is not None:
        chat: dict[str, Any] = {
            "chat_id": chat_id_int,
            "name": (chat_name or "").strip() or None,
            "delete_after": delete_after_value,
            "action_interval": float(action_interval_value or 1),
            "actions": actions,
        }
        raw["chats"] = [chat]

    recent_chats, chat_errors = await _fetch_recent_chats(
        settings, task.account_name, limit=50
    )

    preview = None
    if not errors:
        try:
            validated = _validate_signer_config(raw)
            preview = json.dumps(validated, ensure_ascii=False, indent=2) + "\n"
            tasks_store.write_config_text(task_name, preview)
            tasks_store.touch_updated_at(task_name)
            backup_manager = getattr(request.app.state, "backup_manager", None)
            if backup_manager:
                await backup_manager.schedule_push("task_wizard_save")
            return RedirectResponse(
                url=f"/tasks/{_quote_segment(task_name)}/wizard?ok=1", status_code=303
            )
        except Exception as e:
            errors.append(str(e))

    display_errors = errors + chat_errors
    if not display_errors:
        display_errors = None

    return templates.TemplateResponse(
        request,
        "task_wizard.html",
        {
            "request": request,
            "task": task,
            "csrf_token": issue_csrf_token(request),
            "ok": False,
            "errors": display_errors,
            "form": form,
            "recent_chats": [
                {"id": c["id"], "label": _format_chat_label(c)} for c in recent_chats
            ],
            "preview": preview,
        },
        status_code=400,
    )


@router.post("/tasks/{task_name}/edit", response_class=HTMLResponse)
async def edit_task_save(
    request: Request,
    task_name: str,
    config_text: str = Form(""),
    csrf_token: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    templates = _get_templates(request)
    tasks_store: TasksStore = request.app.state.tasks_store
    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if not task:
        return RedirectResponse(url="/tasks", status_code=303)

    try:
        raw = json.loads(config_text or "{}")
        validated = _validate_signer_config(raw)
        new_text = json.dumps(validated, ensure_ascii=False, indent=2)
        tasks_store.write_config_text(task_name, new_text + "\n")
        tasks_store.touch_updated_at(task_name)
        backup_manager = getattr(request.app.state, "backup_manager", None)
        if backup_manager:
            await backup_manager.schedule_push("task_save")
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "task_edit.html",
            {
                "request": request,
                "task": task,
                "config_text": config_text,
                "csrf_token": issue_csrf_token(request),
                "error": str(e),
                "ok": False,
            },
            status_code=400,
        )

    return RedirectResponse(
        url=f"/tasks/{_quote_segment(task_name)}/edit?ok=1", status_code=303
    )


@router.post("/tasks/{task_name}/delete")
async def delete_task(request: Request, task_name: str, csrf_token: str = Form("")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)

    tasks_store: TasksStore = request.app.state.tasks_store
    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if task:
        task_dir = tasks_store._task_dir(task_name)  # noqa: SLF001
        if task_dir.exists():
            shutil.rmtree(task_dir)
        backup_manager = getattr(request.app.state, "backup_manager", None)
        if backup_manager:
            await backup_manager.schedule_push("task_delete")
    return RedirectResponse(url="/tasks", status_code=303)


@router.post("/tasks/{task_name}/run-once")
async def run_once_task(request: Request, task_name: str, csrf_token: str = Form("")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    tasks_store: TasksStore = request.app.state.tasks_store
    manager: WorkerManager = request.app.state.worker_manager

    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if not task:
        return RedirectResponse(url="/tasks", status_code=303)

    run_id = await manager.start(
        StartRunRequest(
            task_name=task.task_name,
            account_name=task.account_name,
            mode="run_once",
        )
    )
    backup_manager = getattr(request.app.state, "backup_manager", None)
    if backup_manager:
        await backup_manager.schedule_push("run_start")
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


@router.post("/tasks/{task_name}/run")
async def run_task(request: Request, task_name: str, csrf_token: str = Form("")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    tasks_store: TasksStore = request.app.state.tasks_store
    manager: WorkerManager = request.app.state.worker_manager

    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if not task:
        return RedirectResponse(url="/tasks", status_code=303)

    run_id = await manager.start(
        StartRunRequest(
            task_name=task.task_name,
            account_name=task.account_name,
            mode="run",
        )
    )
    backup_manager = getattr(request.app.state, "backup_manager", None)
    if backup_manager:
        await backup_manager.schedule_push("run_start")
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)
