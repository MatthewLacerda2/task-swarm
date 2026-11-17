"""task-swarm run | resume | status | transcript | list"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from . import config


def _apply(cfg: config.RunConfig, overrides: list[str] | None) -> None:
    for kv in overrides or []:
        key, _, value = kv.partition("=")
        if not hasattr(cfg, key) or key == "run_id":
            raise SystemExit(f"unknown setting {key!r}; see RunConfig in taskswarm/config.py")
        current = getattr(cfg, key)
        setattr(cfg, key, value.lower() in ("1", "true", "yes") if isinstance(current, bool) else type(current)(value))


def _execute(run) -> int:
    from . import orchestrator, status

    try:
        asyncio.run(orchestrator.execute(run))
    except KeyboardInterrupt:
        run.db.set_meta("status", "interrupted")
        print(f"\ninterrupted; continue with: task-swarm resume {run.cfg.run_id}")
        return 130
    print()
    print(status.render(run.cfg.run_id))
    return 0 if run.db.get_meta("status") == "done" else 1


def cmd_run(args) -> int:
    from . import llm, run as runmod

    if args.file:
        task = Path(args.file).read_text().strip()
    elif args.task:
        task = " ".join(args.task).strip()
    else:
        raise SystemExit("give the task as text or with --file")
    if not task:
        raise SystemExit("the task is empty")
    run_id = args.id or time.strftime("%Y%m%d-%H%M%S")
    cfg = config.RunConfig(run_id=run_id)
    if (cfg.run_dir / "run.db").exists():
        raise SystemExit(f"run {run_id} already exists; continue it with `task-swarm resume {run_id}`")
    if args.budget is not None:
        cfg.run_budget = args.budget
    _apply(cfg, args.set)
    client = llm.AnthropicLLM(config.read_api_key(), cfg)
    run = runmod.open_run(cfg, client, task)
    print(f"run {run_id} in {cfg.run_dir} (budget US$ {cfg.run_budget:.2f}, hard stop US$ {cfg.hard_stop:.2f})")
    return _execute(run)


def cmd_resume(args) -> int:
    from . import llm, run as runmod

    cfg = config.RunConfig(run_id=args.run_id)
    if not (cfg.run_dir / "run.db").exists():
        raise SystemExit(f"no run {args.run_id} under {config.RUNS_ROOT}")
    probe = runmod.open_run(cfg, client=None)  # type: ignore[arg-type]
    cfg = probe.cfg
    if args.budget is not None or args.set:
        if args.budget is not None:
            cfg.run_budget = args.budget
        _apply(cfg, args.set)
        probe.db.set_meta("config", cfg.to_dict())
    probe.db.conn.close()
    run = runmod.open_run(cfg, llm.AnthropicLLM(config.read_api_key(), cfg))
    return _execute(run)


def cmd_status(args) -> int:
    from . import status

    print(status.render(args.run_id))
    return 0


def cmd_transcript(args) -> int:
    from . import status

    print(status.transcript(args.run_id, args.agent))
    return 0


def cmd_list(args) -> int:
    import sqlite3

    if not config.RUNS_ROOT.exists():
        return 0
    for d in sorted(config.RUNS_ROOT.iterdir()):
        if (d / "run.db").exists():
            db = sqlite3.connect(f"file:{d / 'run.db'}?mode=ro", uri=True)
            meta = dict(db.execute("SELECT key, value FROM meta WHERE key IN ('status', 'task')").fetchall())
            task = " ".join(json.loads(meta.get("task", '""')).split())[:70]
            print(f"{d.name:<24} {json.loads(meta.get('status', '\"?\"')):<16} {task}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="task-swarm")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="give the team a task and run it to the end")
    p.add_argument("task", nargs="*", help="the task, as text")
    p.add_argument("--file", help="read the task from a file")
    p.add_argument("--id", help="name of the run (default: date and time)")
    p.add_argument("--budget", type=float, help="run budget in US$ (default 2.00)")
    p.add_argument("--set", nargs="*", metavar="KEY=VALUE", help="override a RunConfig value, e.g. max_parallel=2")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("resume", help="continue a run that was interrupted, paused or stalled")
    p.add_argument("run_id")
    p.add_argument("--budget", type=float, help="new run budget in US$")
    p.add_argument("--set", nargs="*", metavar="KEY=VALUE")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("status", help="where a run stands")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("transcript", help="one agent's conversation, readable")
    p.add_argument("run_id")
    p.add_argument("agent", help="lead, a task id, <task>.review1, final.review1")
    p.set_defaults(func=cmd_transcript)

    p = sub.add_parser("list", help="runs on this machine")
    p.set_defaults(func=cmd_list)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
