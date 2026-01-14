from __future__ import annotations

import json
import shutil
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from croniter import CroniterBadCronError, croniter
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
from tg_signer.webapp.store import AccountsStore, RunsStore, TasksStore, validate_name

router = APIRouter()


def _get_templates(request: Request):
    return request.app.state.templates


def _require_login(request: Request):
    if request.session.get("logged_in") is not True:
        return redirect_to_login(request)
    return None


def _quote_segment(value: str) -> str:
    return quote(value, safe="")

def _quote_query(value: str) -> str:
    return quote(value, safe="")


def _redirect_tasks(*, ok: str = "", error: str = "") -> RedirectResponse:
    url = "/tasks"
    if ok:
        url = f"{url}?ok={_quote_query(ok)}"
    elif error:
        url = f"{url}?error={_quote_query(error)}"
    return RedirectResponse(url=url, status_code=303)


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


def _humanize_sign_at(sign_at: str) -> str:
    sign_at = (sign_at or "").strip()
    if not sign_at:
        return "-"
    try:
        parsed = dt_time.fromisoformat(sign_at)
        return f"每天 {parsed.hour:02d}:{parsed.minute:02d}"
    except ValueError:
        pass
    parts = sign_at.split()
    if len(parts) == 5 and parts[2:] == ["*", "*", "*"]:
        try:
            minute = int(parts[0])
            hour = int(parts[1])
        except ValueError:
            return sign_at
        if 0 <= hour < 24 and 0 <= minute < 60:
            return f"每天 {hour:02d}:{minute:02d}"
    return sign_at


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


def _normalize_sign_at(value: str, *, errors: list[str]) -> str | None:
    value = (value or "").replace("：", ":").strip()
    if not value:
        errors.append("签到时间不能为空")
        return None
    try:
        parsed = dt_time.fromisoformat(value)
        return f"{parsed.minute} {parsed.hour} * * *"
    except ValueError:
        pass
    try:
        croniter(value)
    except CroniterBadCronError:
        errors.append("签到时间格式不正确：请输入 HH:MM 或 crontab 表达式（如 0 6 * * *）")
        return None
    return value


def _cron_to_time_value(sign_at: str) -> str | None:
    sign_at = (sign_at or "").strip()
    if not sign_at:
        return None
    try:
        parsed = dt_time.fromisoformat(sign_at)
        return f"{parsed.hour:02d}:{parsed.minute:02d}"
    except ValueError:
        pass
    parts = sign_at.split()
    if len(parts) != 5:
        return None
    if parts[2:] != ["*", "*", "*"]:
        return None
    try:
        minute = int(parts[0])
        hour = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return f"{hour:02d}:{minute:02d}"


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


async def _collect_accounts_and_tasks(
    request: Request,
) -> tuple[list[str], list[dict[str, Any]]]:
    settings: WebSettings = request.app.state.settings
    accounts_store: AccountsStore = request.app.state.accounts_store
    tasks_store: TasksStore = request.app.state.tasks_store
    runs_store: RunsStore = request.app.state.runs_store
    manager: WorkerManager = request.app.state.worker_manager

    accounts = [a.account_name for a in accounts_store.list()]
    tasks: list[dict[str, Any]] = []
    for t in tasks_store.list():
        config_summary: dict[str, Any] = {
            "sign_at": None,
            "schedule_label": "-",
            "random_seconds": None,
            "sign_interval": None,
            "config_ok": False,
        }
        try:
            raw_text = tasks_store.read_config_text(t.task_name)
            raw = json.loads(raw_text or "{}")
            loaded = SignConfigV3.load(raw)
            if loaded:
                cfg, _from_old = loaded
                config_summary = {
                    "sign_at": cfg.sign_at,
                    "schedule_label": _humanize_sign_at(cfg.sign_at),
                    "random_seconds": cfg.random_seconds,
                    "sign_interval": cfg.sign_interval,
                    "config_ok": True,
                }
        except Exception:
            config_summary = {
                "sign_at": None,
                "schedule_label": "配置有误",
                "random_seconds": None,
                "sign_interval": None,
                "config_ok": False,
            }

        logged_in = _is_account_logged_in(settings, t.account_name)
        running_run_id = await manager.get_running_run_id(t.account_name)
        running = False
        if running_run_id:
            run = runs_store.get(running_run_id)
            running = bool(
                run and run.task_name == t.task_name and run.status in {"running", "stopping"}
            )

        tasks.append(
            {
                **t.__dict__,
                **config_summary,
                "logged_in": logged_in,
                "running": running,
                "running_run_id": running_run_id if running else None,
            }
        )

    return accounts, tasks


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request, ok: str = "", error: str = ""):
    redirect = _require_login(request)
    if redirect:
        return redirect
    templates = _get_templates(request)
    accounts, tasks = await _collect_accounts_and_tasks(request)
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "request": request,
            "accounts": accounts,
            "tasks": tasks,
            "csrf_token": issue_csrf_token(request),
            "error": error or None,
            "ok": ok or None,
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
        accounts, tasks = await _collect_accounts_and_tasks(request)
        return templates.TemplateResponse(
            request,
            "tasks.html",
            {
                "request": request,
                "accounts": accounts,
                "tasks": tasks,
                "csrf_token": issue_csrf_token(request),
                "error": str(e),
                "ok": None,
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

@router.get("/tasks/{task_name}/schedule", response_class=HTMLResponse)
async def task_schedule_page(request: Request, task_name: str, ok: str = ""):
    redirect = _require_login(request)
    if redirect:
        return redirect
    templates = _get_templates(request)
    tasks_store: TasksStore = request.app.state.tasks_store

    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if not task:
        return RedirectResponse(url="/tasks", status_code=303)

    errors: list[str] = []
    form: dict[str, Any] = {
        "mode": "daily",
        "daily_time": "06:00",
        "cron_expr": "0 6 * * *",
        "random_seconds": "0",
        "sign_interval": "1",
        "restart": True,
    }

    raw_text = tasks_store.read_config_text(task_name)
    try:
        raw = json.loads(raw_text or "{}")
        loaded = SignConfigV3.load(raw)
        if not loaded:
            errors.append("当前配置无法解析，请使用 JSON 编辑修复后再调整时间。")
        else:
            cfg, _from_old = loaded
            daily_time = _cron_to_time_value(cfg.sign_at)
            form.update(
                {
                    "mode": "daily" if daily_time else "cron",
                    "daily_time": daily_time or "06:00",
                    "cron_expr": cfg.sign_at,
                    "random_seconds": str(cfg.random_seconds),
                    "sign_interval": str(cfg.sign_interval),
                }
            )
    except json.JSONDecodeError:
        errors.append("当前 config.json 不是合法 JSON，请先使用 JSON 编辑修复。")
    except Exception as e:
        errors.append(f"读取配置失败：{e}")

    return templates.TemplateResponse(
        request,
        "task_schedule.html",
        {
            "request": request,
            "task": task,
            "csrf_token": issue_csrf_token(request),
            "ok": ok or None,
            "errors": errors or None,
            "form": form,
        },
    )


@router.post("/tasks/{task_name}/schedule", response_class=HTMLResponse)
async def task_schedule_save(
    request: Request,
    task_name: str,
    csrf_token: str = Form(""),
    mode: str = Form("daily"),
    daily_time: str = Form(""),
    cron_expr: str = Form(""),
    random_seconds: str = Form(""),
    sign_interval: str = Form(""),
    restart: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    templates = _get_templates(request)

    tasks_store: TasksStore = request.app.state.tasks_store
    runs_store: RunsStore = request.app.state.runs_store
    manager: WorkerManager = request.app.state.worker_manager
    settings: WebSettings = request.app.state.settings

    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if not task:
        return RedirectResponse(url="/tasks", status_code=303)

    form = {
        "mode": mode,
        "daily_time": daily_time,
        "cron_expr": cron_expr,
        "random_seconds": random_seconds,
        "sign_interval": sign_interval,
        "restart": restart == "1",
    }

    errors: list[str] = []
    sign_at_value: str | None = None
    if mode == "daily":
        sign_at_value = _normalize_sign_at(daily_time, errors=errors)
    elif mode == "cron":
        sign_at_value = _normalize_sign_at(cron_expr, errors=errors)
    else:
        errors.append("mode 不合法")

    random_seconds_value = _parse_optional_int(
        random_seconds, label="签到时间随机误差", errors=errors
    )
    sign_interval_value = _parse_optional_int(sign_interval, label="签到间隔", errors=errors)

    if random_seconds_value is None:
        random_seconds_value = 0
    if random_seconds_value < 0:
        errors.append("签到时间随机误差不能为负数")

    if sign_interval_value is None:
        sign_interval_value = 1
    if sign_interval_value < 0:
        errors.append("签到间隔不能为负数")

    if errors:
        return templates.TemplateResponse(
            request,
            "task_schedule.html",
            {
                "request": request,
                "task": task,
                "csrf_token": issue_csrf_token(request),
                "ok": None,
                "errors": errors,
                "form": form,
            },
            status_code=400,
        )

    try:
        raw_text = tasks_store.read_config_text(task_name)
        raw = json.loads(raw_text or "{}")
        loaded = SignConfigV3.load(raw)
        if not loaded:
            raise ValueError("当前配置无法解析，请使用 JSON 编辑修复后再调整时间。")
        cfg, _from_old = loaded
        new_raw = cfg.to_jsonable()
        new_raw["sign_at"] = sign_at_value
        new_raw["random_seconds"] = int(random_seconds_value)
        new_raw["sign_interval"] = int(sign_interval_value)
        validated = _validate_signer_config(new_raw)
        new_text = json.dumps(validated, ensure_ascii=False, indent=2) + "\n"
        tasks_store.write_config_text(task_name, new_text)
        tasks_store.touch_updated_at(task_name)
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "task_schedule.html",
            {
                "request": request,
                "task": task,
                "csrf_token": issue_csrf_token(request),
                "ok": None,
                "errors": [f"保存失败：{e}"],
                "form": form,
            },
            status_code=400,
        )

    restart_requested = restart == "1"
    restart_done = False
    if restart_requested and task.enabled:
        if not _is_account_logged_in(settings, task.account_name):
            return RedirectResponse(
                url=f"/tasks/{_quote_segment(task_name)}/schedule?ok={_quote_query('已保存（账号未登录，无法重启）')}",
                status_code=303,
            )

        existing = await manager.get_running_run_id(task.account_name)
        if existing:
            run = runs_store.get(existing)
            if run and run.task_name == task_name and run.status in {"running", "stopping"}:
                await manager.stop(existing)
                for _ in range(40):
                    await asyncio.sleep(0.5)
                    if not await manager.get_running_run_id(task.account_name):
                        break

        if not await manager.get_running_run_id(task.account_name):
            try:
                await manager.start(
                    StartRunRequest(
                        task_name=task.task_name,
                        account_name=task.account_name,
                        mode="run",
                    )
                )
                restart_done = True
            except Exception:
                restart_done = False

    backup_manager = getattr(request.app.state, "backup_manager", None)
    if backup_manager:
        await backup_manager.schedule_push("task_schedule")

    ok_message = "已保存"
    if restart_requested and task.enabled:
        ok_message = "已保存并重启" if restart_done else "已保存（重启失败，可到任务页手动启动）"
    return RedirectResponse(
        url=f"/tasks/{_quote_segment(task_name)}/schedule?ok={_quote_query(ok_message)}",
        status_code=303,
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
    sign_at_raw = (sign_at or "").strip() or "0 6 * * *"
    sign_at_value = _normalize_sign_at(sign_at_raw, errors=errors) or sign_at_raw
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

@router.post("/tasks/{task_name}/enable")
async def enable_task(request: Request, task_name: str, csrf_token: str = Form("")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)

    tasks_store: TasksStore = request.app.state.tasks_store
    runs_store: RunsStore = request.app.state.runs_store
    manager: WorkerManager = request.app.state.worker_manager

    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if not task:
        return _redirect_tasks(error="任务不存在")

    existing = await manager.get_running_run_id(task.account_name)
    if existing:
        run = runs_store.get(existing)
        if run and run.task_name == task_name and run.status in {"running", "stopping"}:
            tasks_store.set_enabled(task_name, True)
            return _redirect_tasks(ok="任务已启用（当前已在运行）")
        return _redirect_tasks(error="该账号已有运行中的任务，请先停止后再启用")

    tasks_store.set_enabled(task_name, True)
    settings: WebSettings = request.app.state.settings
    if not _is_account_logged_in(settings, task.account_name):
        return _redirect_tasks(ok="任务已启用（待账号登录后自动运行）")

    try:
        await manager.start(
            StartRunRequest(
                task_name=task.task_name,
                account_name=task.account_name,
                mode="run",
            )
        )
    except Exception as e:
        return _redirect_tasks(error=f"启用失败：{e}")

    backup_manager = getattr(request.app.state, "backup_manager", None)
    if backup_manager:
        await backup_manager.schedule_push("task_enable")
    return _redirect_tasks(ok="任务已启用（按计划常驻运行）")


@router.post("/tasks/{task_name}/disable")
async def disable_task(request: Request, task_name: str, csrf_token: str = Form("")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)

    tasks_store: TasksStore = request.app.state.tasks_store
    runs_store: RunsStore = request.app.state.runs_store
    manager: WorkerManager = request.app.state.worker_manager

    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if not task:
        return _redirect_tasks(error="任务不存在")

    tasks_store.set_enabled(task_name, False)

    existing = await manager.get_running_run_id(task.account_name)
    if existing:
        run = runs_store.get(existing)
        if run and run.task_name == task_name:
            await manager.stop(existing)

    backup_manager = getattr(request.app.state, "backup_manager", None)
    if backup_manager:
        await backup_manager.schedule_push("task_disable")
    return _redirect_tasks(ok="任务已停用")


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
    return await enable_task(request, task_name, csrf_token=csrf_token)
