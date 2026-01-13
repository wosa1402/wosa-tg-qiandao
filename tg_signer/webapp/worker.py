from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from tg_signer.core import UserSigner, get_proxy
from tg_signer.logger import configure_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tg-signer-worker")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--account-name", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--mode", choices=["run", "run_once"], required=True)
    parser.add_argument("--num-of-dialogs", type=int, default=50)
    return parser.parse_args()


async def run_worker(args: argparse.Namespace) -> None:
    run_id = args.run_id
    task_name = args.task_name
    account_name = args.account_name
    workdir = Path(args.workdir).resolve()
    session_dir = Path(args.session_dir).resolve()
    runs_dir = Path(args.runs_dir).resolve()

    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    configure_logger(log_level=os.environ.get("TG_SIGNER_LOG_LEVEL", "info"), log_dir=run_dir, log_file=run_dir / "run.log")

    signer = UserSigner(
        task_name=task_name,
        account=account_name,
        proxy=get_proxy(),
        session_dir=str(session_dir),
        workdir=str(workdir),
        session_string=None,
        in_memory=False,
        loop=asyncio.get_running_loop(),
    )

    if args.mode == "run_once":
        await signer.run_once(args.num_of_dialogs)
        return
    await signer.run(args.num_of_dialogs)


def main() -> None:
    args = parse_args()
    asyncio.run(run_worker(args))


if __name__ == "__main__":
    main()
