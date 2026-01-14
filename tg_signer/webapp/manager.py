from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from tg_signer.webapp.settings import WebSettings
from tg_signer.webapp.store import RunRecord, RunsStore, TasksStore, utc_now_iso, validate_name


class BackupScheduler(Protocol):
    async def schedule_push(self, reason: str = "") -> None: ...


@dataclass(frozen=True)
class RunOnceRequest:
    task_name: str
    account_name: str
    num_of_dialogs: int = 50


class WorkerManager:
    """
    Web 侧编排器：
    - 每个账号启动 1 个 account_worker 子进程
    - 子进程内部串行执行多个任务（避免同账号会话冲突）
    - Web 通过 stdin 下发命令，stdout 接收事件并写入 runs_store
    """

    def __init__(
        self,
        settings: WebSettings,
        *,
        tasks_store: TasksStore,
        runs_store: RunsStore,
    ) -> None:
        self._settings = settings
        self._tasks_store = tasks_store
        self._runs_store = runs_store
        self._logger = logging.getLogger("uvicorn.error")

        self._lock = asyncio.Lock()
        self._proc_by_account: dict[str, asyncio.subprocess.Process] = {}
        self._stdin_lock_by_account: dict[str, asyncio.Lock] = {}
        self._stdout_task_by_account: dict[str, asyncio.Task[None]] = {}
        self._stderr_task_by_account: dict[str, asyncio.Task[None]] = {}
        self._watch_task_by_account: dict[str, asyncio.Task[None]] = {}

        self._current_run_by_account: dict[str, str] = {}
        self._current_task_by_account: dict[str, str] = {}
        self._account_by_run_id: dict[str, str] = {}

        self._backup_scheduler: BackupScheduler | None = None

    def set_backup_scheduler(self, backup_scheduler: BackupScheduler | None) -> None:
        self._backup_scheduler = backup_scheduler

    async def _schedule_backup_push(self, reason: str) -> None:
        scheduler = self._backup_scheduler
        if not scheduler:
            return
        try:
            await scheduler.schedule_push(reason)
        except Exception:
            return

    def _run_dir(self, run_id: str) -> Path:
        return (self._settings.runs_dir / run_id).resolve()

    def get_log_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "run.log"

    def _is_account_logged_in(self, account_name: str) -> bool:
        sessions_dir = self._settings.sessions_dir
        candidates = [
            sessions_dir / f"{account_name}.session_string",
            sessions_dir / f"{account_name}.session",
        ]
        return any(p.exists() for p in candidates)

    async def get_running_run_id(self, account_name: str) -> str | None:
        async with self._lock:
            return self._current_run_by_account.get(account_name)

    async def get_running_task_name(self, account_name: str) -> str | None:
        async with self._lock:
            return self._current_task_by_account.get(account_name)

    async def ensure_account_worker(self, account_name: str) -> None:
        account_name = validate_name(account_name, label="账号名")
        async with self._lock:
            existing = self._proc_by_account.get(account_name)
            if existing and existing.returncode is None:
                return
        await self._start_account_worker(account_name)

    async def _start_account_worker(self, account_name: str) -> None:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["TG_SIGNER_DATA_DIR"] = str(self._settings.data_dir)
        env["TG_SIGNER_PRINT_CHATS"] = env.get("TG_SIGNER_PRINT_CHATS", "0")

        cmd = [
            sys.executable,
            "-m",
            "tg_signer.webapp.account_worker",
            "--account-name",
            account_name,
            "--workdir",
            str(self._settings.workdir),
            "--session-dir",
            str(self._settings.sessions_dir),
            "--runs-dir",
            str(self._settings.runs_dir),
            "--num-of-dialogs",
            "50",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        async with self._lock:
            self._proc_by_account[account_name] = proc
            self._stdin_lock_by_account.setdefault(account_name, asyncio.Lock())
            self._stdout_task_by_account[account_name] = asyncio.create_task(
                self._read_stdout(account_name, proc)
            )
            self._stderr_task_by_account[account_name] = asyncio.create_task(
                self._read_stderr(account_name, proc)
            )
            self._watch_task_by_account[account_name] = asyncio.create_task(
                self._watch_account_worker(account_name, proc)
            )

    async def reload_account(self, account_name: str) -> None:
        account_name = validate_name(account_name, label="账号名")
        async with self._lock:
            proc = self._proc_by_account.get(account_name)
        if not proc or proc.returncode is not None:
            return
        await self._send(account_name, {"cmd": "reload"})

    async def shutdown_account(self, account_name: str) -> None:
        account_name = validate_name(account_name, label="账号名")
        async with self._lock:
            proc = self._proc_by_account.get(account_name)
        if not proc or proc.returncode is not None:
            return
        await self._send(account_name, {"cmd": "shutdown"})

    async def run_once(self, req: RunOnceRequest) -> str:
        task_name = validate_name(req.task_name, label="任务名")
        account_name = validate_name(req.account_name, label="账号名")

        task = self._tasks_store.get(task_name)
        if not task:
            raise ValueError("任务不存在")
        if task.account_name != account_name:
            raise ValueError("任务绑定的账号与请求账号不一致")
        if not self._is_account_logged_in(account_name):
            raise RuntimeError("账号未登录，请先在 /accounts 完成登录")

        await self.ensure_account_worker(account_name)

        run_id = str(uuid.uuid4())
        record = RunRecord(
            run_id=run_id,
            task_name=task_name,
            account_name=account_name,
            mode="run_once",
            status="queued",
            created_at=utc_now_iso(),
        )
        self._runs_store.create(record)

        async with self._lock:
            self._account_by_run_id[run_id] = account_name

        await self._send(
            account_name,
            {
                "cmd": "run_once",
                "run_id": run_id,
                "task_name": task_name,
                "num_of_dialogs": int(req.num_of_dialogs),
            },
        )
        await self._schedule_backup_push("run_start")
        return run_id

    async def stop_run(self, run_id: str) -> bool:
        run_id = (run_id or "").strip()
        if not run_id:
            return False
        async with self._lock:
            account_name = self._account_by_run_id.get(run_id)
        if not account_name:
            run = self._runs_store.get(run_id)
            if run:
                account_name = run.account_name
        if not account_name:
            return False

        async with self._lock:
            proc = self._proc_by_account.get(account_name)
        if not proc or proc.returncode is not None:
            return False

        self._runs_store.update(run_id, status="stopping")
        await self._send(account_name, {"cmd": "stop_run", "run_id": run_id})
        await self._schedule_backup_push("run_stop")
        return True

    async def _send(self, account_name: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            proc = self._proc_by_account.get(account_name)
            stdin_lock = self._stdin_lock_by_account.get(account_name)
        if not proc or proc.returncode is not None:
            raise RuntimeError("worker 未运行")
        if proc.stdin is None:
            raise RuntimeError("worker stdin 不可用")
        if stdin_lock is None:
            stdin_lock = asyncio.Lock()
            async with self._lock:
                self._stdin_lock_by_account[account_name] = stdin_lock

        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with stdin_lock:
            proc.stdin.write(data)
            await proc.stdin.drain()

    async def _read_stdout(
        self, account_name: str, proc: asyncio.subprocess.Process
    ) -> None:
        if proc.stdout is None:
            return
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            await self._handle_event(account_name, event)

    async def _read_stderr(
        self, account_name: str, proc: asyncio.subprocess.Process
    ) -> None:
        if proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            self._logger.info("[worker:%s] %s", account_name, text)

    async def _watch_account_worker(
        self, account_name: str, proc: asyncio.subprocess.Process
    ) -> None:
        try:
            exit_code = await proc.wait()
        except Exception as e:
            self._logger.warning("worker wait failed: account=%s err=%s", account_name, e)
            exit_code = 1

        async with self._lock:
            self._proc_by_account.pop(account_name, None)
            self._stdin_lock_by_account.pop(account_name, None)
            self._stdout_task_by_account.pop(account_name, None)
            self._stderr_task_by_account.pop(account_name, None)
            self._watch_task_by_account.pop(account_name, None)

            run_id = self._current_run_by_account.pop(account_name, None)
            self._current_task_by_account.pop(account_name, None)

        if run_id:
            self._runs_store.update(
                run_id,
                status="failed" if exit_code != 0 else "stopped",
                finished_at=utc_now_iso(),
                exit_code=exit_code,
                error_message="worker exited",
            )
            await self._schedule_backup_push("run_finish")

    async def _handle_event(self, account_name: str, event: dict[str, Any]) -> None:
        kind = str(event.get("event") or "")

        if kind == "ready":
            self._logger.info(
                "worker ready: account=%s pid=%s", account_name, event.get("pid")
            )
            return

        if kind == "run_started":
            run_id = str(event.get("run_id") or "")
            task_name = str(event.get("task_name") or "")
            mode = str(event.get("mode") or "run")

            if not run_id or not task_name:
                return

            existing = self._runs_store.get(run_id)
            if existing:
                self._runs_store.update(
                    run_id,
                    status="running",
                    started_at=event.get("started_at") or utc_now_iso(),
                    pid=event.get("pid"),
                )
            else:
                record = RunRecord(
                    run_id=run_id,
                    task_name=task_name,
                    account_name=account_name,
                    mode=mode,
                    status="running",
                    created_at=event.get("created_at") or utc_now_iso(),
                    started_at=event.get("started_at") or utc_now_iso(),
                    pid=event.get("pid"),
                )
                self._runs_store.create(record)

            async with self._lock:
                self._current_run_by_account[account_name] = run_id
                self._current_task_by_account[account_name] = task_name
                self._account_by_run_id[run_id] = account_name
            return

        if kind == "run_finished":
            run_id = str(event.get("run_id") or "")
            status = str(event.get("status") or "failed")
            exit_code = event.get("exit_code")
            error_message = event.get("error_message")

            if not run_id:
                return

            self._runs_store.update(
                run_id,
                status=status,
                finished_at=event.get("finished_at") or utc_now_iso(),
                exit_code=exit_code,
                error_message=error_message,
            )
            async with self._lock:
                current = self._current_run_by_account.get(account_name)
                if current == run_id:
                    self._current_run_by_account.pop(account_name, None)
                    self._current_task_by_account.pop(account_name, None)
            await self._schedule_backup_push("run_finish")
            return

