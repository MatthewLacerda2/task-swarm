"""A run: its folder, database, budget, store and model client."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field

from . import config, llm
from .db import DB
from .store import Store

FOLDERS = ("work", "store", "deliverable", "outputs", "sys")


@dataclass
class Run:
    cfg: config.RunConfig
    db: DB
    budget: llm.Budget
    llm: llm.LLM
    system: list[dict]
    store: Store
    semaphore: asyncio.Semaphore
    wake: asyncio.Event = field(default_factory=asyncio.Event)        # something changed: look again
    active: set[str] = field(default_factory=set)                     # agents inside a model/command loop now
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)      # one conversation is never run twice at once
    mail: dict[str, asyncio.Event] = field(default_factory=dict)      # set when a message for that agent arrives

    def send(self, to: str, sender: str, body: str, quiet: bool = False) -> None:
        """Put a message in an agent's inbox. `quiet` waits for its next turn instead of waking it."""
        self.db.send(to, sender, body, quiet)
        if not quiet:
            self.mail.setdefault(to, asyncio.Event()).set()
            self.wake.set()

    def call_settings(self, agent: str) -> tuple[str, str, bool]:
        """(model, effort, keep the cache for an hour) for an agent's calls.

        A reviewer runs on the model of whoever's work it checks: a task's reviewer on
        the workers' model, the final reviewer on the lead's. The planner makes a few
        calls in a row and is gone, so a five-minute cache is enough for it.
        """
        cfg = self.cfg
        if agent == "planner":
            return cfg.plan_model, cfg.plan_effort, False
        if agent == "lead" or agent.startswith("final."):
            return cfg.lead_model, cfg.effort, True
        return cfg.worker_model, cfg.effort, True


def open_run(cfg: config.RunConfig, client: llm.LLM, task: str | None = None) -> Run:
    """Open or create a run. The config is stored at creation and reused on resume."""
    from . import prompts

    for name in FOLDERS:
        (cfg.run_dir / name).mkdir(parents=True, exist_ok=True)
    db = DB(cfg.run_dir / "run.db")
    stored = db.get_meta("config")
    if stored is None:
        if task is None:
            raise SystemExit(f"no run {cfg.run_id}")
        if problems := cfg.problems():
            db.conn.close()
            delete_run(cfg)
            raise SystemExit("This run can't start:\n" + "\n".join(f"- {p}" for p in problems))
        db.set_meta("config", cfg.to_dict())
        db.set_meta("task", task)
        db.set_meta("status", "new")
    else:
        cfg = config.RunConfig.from_dict(stored)
    return Run(
        cfg=cfg, db=db, budget=llm.Budget(db, cfg), llm=client,
        system=llm.system_blocks(prompts.system_prompt(cfg)),
        store=Store(cfg.run_dir / "store"),
        semaphore=asyncio.Semaphore(cfg.concurrency),
    )


def work_dir(run: Run, agent: str):
    path = run.cfg.run_dir / "work" / agent
    path.mkdir(parents=True, exist_ok=True)
    return path


def delete_run(cfg: config.RunConfig) -> None:
    shutil.rmtree(cfg.run_dir, ignore_errors=True)
