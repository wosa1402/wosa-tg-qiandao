from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Optional

from croniter import CroniterBadCronError, croniter

from tg_signer.config import SignConfigV3
from tg_signer.core import UserSigner, get_client, get_now, get_proxy
from tg_signer.logger import configure_logger
from tg_signer.webapp.store import TasksStore, utc_now_iso


@dataclass
class ScheduledTask:
    task_name: str
    cron_expr: str
    random_seconds: int
    fingerprint: str
    next_at: datetime
    config_ok: bool


@dataclass(frozen=True)
class RunCommand:
    run_id: str
    task_name: str
    mode: str  # run | run_once
    num_of_dialogs: int


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _normalize_sign_at(value: str) -> str:
    value = (value or "").replace("：", ":").strip()
    if not value:
        raise ValueError("sign_at 不能为空")
    try:
        parsed = dt_time.fromisoformat(value)
        return f"{parsed.minute} {parsed.hour} * * *"
    except ValueError:
        pass
    try:
        croniter(value)
    except CroniterBadCronError as e:
        raise ValueError("sign_at 不是合法的时间或 crontab") from e
    return value


def _read_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8") or "{}")


class AccountWorker:
    def __init__(
        self,
        *,
        account_name: str,
        workdir: Path,
        sessions_dir: Path,
        runs_dir: Path,
        num_of_dialogs: int = 50,
    ) -> None:
        self._account_name = account_name
        self._workdir = workdir
        self._sessions_dir = sessions_dir
        self._runs_dir = runs_dir
        self._num_of_dialogs = num_of_dialogs
        self._proxy = get_proxy()

        self._tasks_store = TasksStore(self._workdir)
        self._tasks: dict[str, ScheduledTask] = {}
        self._run_queue: list[RunCommand] = []
        self._reload_requested = True
        self._wakeup = asyncio.Event()
        self._shutdown = False

        self._current_run: Optional[RunCommand] = None
        self._current_run_task: Optional[asyncio.Task[None]] = None
        self._current_task_name: Optional[str] = None
        self._user_id: Optional[int] = None
        self._signers: dict[str, UserSigner] = {}

    def _get_signer(self, task_name: str) -> UserSigner:
        signer = self._signers.get(task_name)
        if signer:
            return signer
        signer = UserSigner(
            task_name=task_name,
            account=self._account_name,
            proxy=self._proxy,
            session_dir=str(self._sessions_dir),
            workdir=str(self._workdir),
            session_string=None,
            in_memory=False,
            loop=asyncio.get_running_loop(),
        )
        self._signers[task_name] = signer
        return signer

    async def _ensure_user_id(self) -> Optional[int]:
        if self._user_id is not None:
            return self._user_id
        client = get_client(self._account_name, self._proxy, workdir=self._sessions_dir)
        try:
            async with client:
                me = await client.get_me()
                self._user_id = int(me.id)
        except Exception:
            return None
        return self._user_id

    def _sign_record_path(self, task_name: str) -> Optional[Path]:
        if self._user_id is None:
            return None
        return (
            self._workdir
            / "signs"
            / task_name
            / str(self._user_id)
            / "sign_record.json"
        )

    def _should_run_now(self, task: ScheduledTask, now: datetime) -> bool:
        record_path = self._sign_record_path(task.task_name)
        if record_path is None:
            return False

        record = _read_json(record_path, default={})
        today_key = str(now.date())
        last_value = record.get(today_key)
        if not last_value:
            return True
        try:
            last_sign_at = datetime.fromisoformat(str(last_value))
        except ValueError:
            return True

        try:
            next_run: datetime = croniter(task.cron_expr, last_sign_at).next(datetime)
        except CroniterBadCronError:
            return True
        return not (next_run > now)

    def _compute_next_at(self, task: ScheduledTask, now: datetime) -> datetime:
        try:
            next_run: datetime = croniter(task.cron_expr, now).next(datetime)
        except CroniterBadCronError:
            return now + timedelta(minutes=5)
        delay = 0
        if task.random_seconds:
            delay = random.randint(0, max(0, int(task.random_seconds)))
        return next_run + timedelta(seconds=delay)

    async def _reload_tasks(self) -> None:
        now = get_now()
        enabled = [
            t
            for t in self._tasks_store.list()
            if t.enabled and t.account_name == self._account_name
        ]

        new_tasks: dict[str, ScheduledTask] = {}
        for t in enabled:
            task_name = t.task_name
            try:
                raw_text = self._tasks_store.read_config_text(task_name)
                raw = json.loads(raw_text or "{}")
                loaded = SignConfigV3.load(raw)
                if not loaded:
                    raise ValueError("配置不合法：无法匹配当前/旧版本配置结构")
                cfg, _from_old = loaded
                cron_expr = _normalize_sign_at(cfg.sign_at)
                random_seconds = int(getattr(cfg, "random_seconds", 0) or 0)
                fingerprint = f"{t.updated_at}|{cron_expr}|{random_seconds}"
                prev = self._tasks.get(task_name)
                next_at = now
                if prev and prev.fingerprint == fingerprint and prev.config_ok:
                    next_at = prev.next_at
                new_tasks[task_name] = ScheduledTask(
                    task_name=task_name,
                    cron_expr=cron_expr,
                    random_seconds=random_seconds,
                    fingerprint=fingerprint,
                    next_at=next_at,
                    config_ok=True,
                )
            except Exception:
                fingerprint = f"{t.updated_at}|invalid"
                new_tasks[task_name] = ScheduledTask(
                    task_name=task_name,
                    cron_expr="",
                    random_seconds=0,
                    fingerprint=fingerprint,
                    next_at=now + timedelta(seconds=60),
                    config_ok=False,
                )

        self._tasks = new_tasks

    async def _stdin_loop(self) -> None:
        while not self._shutdown:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                # stdin 关闭通常表示父进程退出（管道断开），避免 worker 成为孤儿进程。
                self._shutdown = True
                if self._current_run_task:
                    self._current_run_task.cancel()
                self._wakeup.set()
                return
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(cmd, dict):
                continue
            await self._on_command(cmd)

    async def _on_command(self, cmd: dict[str, Any]) -> None:
        name = str(cmd.get("cmd") or "").strip()
        if name == "reload":
            self._reload_requested = True
            self._wakeup.set()
            return

        if name == "shutdown":
            self._shutdown = True
            if self._current_run_task:
                self._current_run_task.cancel()
            self._wakeup.set()
            return

        if name == "stop_run":
            run_id = str(cmd.get("run_id") or "")
            if self._current_run and self._current_run.run_id == run_id:
                if self._current_run_task:
                    self._current_run_task.cancel()
            else:
                self._run_queue = [r for r in self._run_queue if r.run_id != run_id]
            self._wakeup.set()
            return

        if name == "run_once":
            run_id = str(cmd.get("run_id") or "")
            task_name = str(cmd.get("task_name") or "").strip()
            num_of_dialogs = int(cmd.get("num_of_dialogs") or self._num_of_dialogs)
            if not run_id or not task_name:
                return
            self._run_queue.append(
                RunCommand(
                    run_id=run_id,
                    task_name=task_name,
                    mode="run_once",
                    num_of_dialogs=num_of_dialogs,
                )
            )
            self._wakeup.set()
            return

    async def _sleep_or_wakeup(self, seconds: float) -> None:
        self._wakeup.clear()
        try:
            await asyncio.wait_for(self._wakeup.wait(), timeout=max(0.2, seconds))
        except asyncio.TimeoutError:
            return

    async def _execute(self, cmd: RunCommand) -> None:
        self._current_run = cmd
        self._current_task_name = cmd.task_name
        run_dir = (self._runs_dir / cmd.run_id).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        configure_logger(
            log_level=os.environ.get("TG_SIGNER_LOG_LEVEL", "info"),
            log_dir=run_dir,
            log_file=run_dir / "run.log",
        )

        _emit(
            {
                "event": "run_started",
                "run_id": cmd.run_id,
                "task_name": cmd.task_name,
                "account_name": self._account_name,
                "mode": cmd.mode,
                "created_at": utc_now_iso(),
                "started_at": utc_now_iso(),
                "pid": os.getpid(),
            }
        )

        status = "success"
        exit_code = 0
        error_message: str | None = None
        try:
            signer = self._get_signer(cmd.task_name)
            self._current_run_task = asyncio.create_task(self._run_signer(signer, cmd))
            await self._current_run_task
        except asyncio.CancelledError:
            status = "stopped"
            exit_code = 130
        except Exception as e:
            status = "failed"
            exit_code = 1
            error_message = str(e)[:500]
        finally:
            self._current_run_task = None
            self._current_run = None
            self._current_task_name = None

        _emit(
            {
                "event": "run_finished",
                "run_id": cmd.run_id,
                "status": status,
                "finished_at": utc_now_iso(),
                "exit_code": exit_code,
                "error_message": error_message,
                "pid": os.getpid(),
            }
        )

    async def _run_signer(self, signer: UserSigner, cmd: RunCommand) -> None:
        if cmd.mode == "run_once":
            await signer.run_once(cmd.num_of_dialogs)
            return
        await signer.run(cmd.num_of_dialogs, only_once=True, force_rerun=False)

    async def run(self) -> None:
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)

        _emit(
            {
                "event": "ready",
                "account_name": self._account_name,
                "pid": os.getpid(),
            }
        )

        stdin_task = asyncio.create_task(self._stdin_loop())
        try:
            while not self._shutdown:
                if self._reload_requested:
                    await self._reload_tasks()
                    self._reload_requested = False

                if self._run_queue:
                    cmd = self._run_queue.pop(0)
                    await self._execute(cmd)
                    continue

                if not self._tasks:
                    await self._sleep_or_wakeup(10)
                    continue

                now = get_now()
                enabled_tasks = [t for t in self._tasks.values() if t.config_ok]
                if not enabled_tasks:
                    await self._sleep_or_wakeup(10)
                    continue

                next_task = min(enabled_tasks, key=lambda t: t.next_at)
                wait_seconds = (next_task.next_at - now).total_seconds()
                if wait_seconds > 0:
                    await self._sleep_or_wakeup(min(wait_seconds, 60))
                    continue

                if await self._ensure_user_id() is None:
                    next_task.next_at = now + timedelta(seconds=30)
                    await self._sleep_or_wakeup(5)
                    continue

                if self._should_run_now(next_task, now):
                    run_id = str(uuid.uuid4())
                    await self._execute(
                        RunCommand(
                            run_id=run_id,
                            task_name=next_task.task_name,
                            mode="run",
                            num_of_dialogs=self._num_of_dialogs,
                        )
                    )

                next_task.next_at = self._compute_next_at(next_task, now)
        finally:
            stdin_task.cancel()
            with contextlib.suppress(Exception):
                await stdin_task


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tg-signer-account-worker")
    parser.add_argument("--account-name", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--num-of-dialogs", type=int, default=50)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    worker = AccountWorker(
        account_name=args.account_name,
        workdir=Path(args.workdir).resolve(),
        sessions_dir=Path(args.session_dir).resolve(),
        runs_dir=Path(args.runs_dir).resolve(),
        num_of_dialogs=int(args.num_of_dialogs),
    )
    asyncio.run(worker.run())


if __name__ == "__main__":
    main()
