"""Renders what agents are told: the system prompt (identical for every agent of a run,
so it is read from one shared cache) and each role's first message."""

from __future__ import annotations

import json

from . import config
from .run import Run


def _call_cost(model: str, cached: int, fresh: int, output: int) -> str:
    p = config.prices(model)
    return f"{(cached * p.cache_read + fresh * p.input + output * p.output) / 1e6:.2g}"


def system_prompt(cfg: config.RunConfig) -> str:
    template = (config.PROMPTS / "system.md").read_text()
    values = cfg.to_dict() | {
        "hard_stop": cfg.hard_stop,
        # a short early call and a long late one, on the workers' model
        "call_low": _call_cost(cfg.worker_model, 5_000, 1_000, 400),
        "call_high": _call_cost(cfg.worker_model, 40_000, 4_000, 2_000),
        "lan_line": "The local network is not reachable." if cfg.block_lan else "",
        "data_line": "the user's own files are in /task/data (read-only), and " if cfg.data_dir else "",
    }
    return template.format(**values)


def identity(agent: str, role: str) -> str:
    what = {"planner": "the planner", "lead": "the lead of this team", "worker": "a worker",
            "reviewer": "a reviewer"}[role]
    return f"You are `{agent}`, {what}."


def compacted_note(agent: str, role: str) -> str:
    return (
        f"[Your conversation was compacted into the summary above to save memory. {identity(agent, role)} "
        "Anything not in the summary is gone, except what you saved in files and in the store. Continue.]"
    )


def planner_first(task: str) -> str:
    return f"{identity('planner', 'planner')}\n\n# The task\n\n{task}"


def lead_first(run: Run, planned: bool) -> str:
    parts = [identity("lead", "lead"), f"# The task\n\n{run.db.get_meta('task')}"]
    if planned:
        tasks = []
        for t in run.db.children(None):
            blockers = json.loads(t["blocked_by"])
            after = f" (after {', '.join(blockers)})" if blockers else ""
            tasks.append(f"## `{t['id']}`: {t['title']}{after}\n\n{t['brief']}\n\nVerify: {t['verify']}")
        parts += [
            "# The plan\n\nThe planner drafted this plan and its first tasks have started. It is yours now.",
            f"Goal: {run.db.get_meta('goal')}",
            f"Done when: {run.db.get_meta('done_when')}",
            *tasks,
            _store(run),
            "End your turn to wait for results, unless you see something in the plan to fix first.",
        ]
    return "\n\n".join(parts)


def _store(run: Run) -> str:
    return f"# The store\n\n{run.store.describe()}"


def worker_first(run: Run, task) -> str:
    parts = [
        identity(task["id"], "worker"),
        f"# The goal of the whole team\n\n{run.db.get_meta('goal')}",
        f"# Your task: {task['title']}\n\n{task['brief']}",
        f"# How your work will be checked\n\n{task['verify']}",
    ]
    owner = task["parent"] or "lead"
    from .plan import depth
    if depth(task["id"]) < run.cfg.max_depth:
        split = "You may split it into sub-tasks if it is too much for one agent."
    else:
        split = "It can't be split into sub-tasks."
    if task["parent"]:
        parent = run.db.task(task["parent"])
        parts.insert(2, f"# The task yours is a piece of: `{parent['id']}`, {parent['title']}")
    parts.append(f"This task was given to you by `{owner}`: that is who to ask, and who gets your report. {split}")
    blockers = [run.db.task(b) for b in json.loads(task["blocked_by"])]
    if blockers:
        lines = ["# Reports of the tasks yours builds on"]
        for b in blockers:
            lines.append(f"## `{b['id']}`: {b['title']}\n\n{b['summary']}\n\nIts files are in /task/work/{b['id']}/.")
        parts.append("\n\n".join(lines))
    parts.append(_store(run))
    return "\n\n".join(parts)


def reviewer_first(run: Run, reviewer: str, task) -> str:
    report = task["summary"] or "(empty)"
    if task["how"]:
        report += f"\n\nHow it was done, according to the worker:\n{task['how']}"
    parts = [
        identity(reviewer, "reviewer"),
        f"# The goal of the whole team\n\n{run.db.get_meta('goal')}",
        f"# The task under review: `{task['id']}`, {task['title']}\n\n{task['brief']}",
        f"# What to check\n\n{task['verify']}",
        f"# The worker's report\n\n{report}",
        *(["# Decisions by the task's owner since the brief\n\n" +
           "\n".join(f"- {d}" for d in json.loads(task["decisions"]))] if task["decisions"] else []),
        f"The worker's files are in /task/work/{task['id']}/. The records it stored have "
        f"\"_task\": \"{task['id']}\". This is review {task['attempts']} of at most {run.cfg.max_attempts}.",
        _store(run),
    ]
    pieces = run.db.children(task["id"])
    if pieces:
        lines = [f"- `{t['id']}` ({t['status']}): {t['title']}" for t in pieces]
        parts.insert(-2, "# Sub-tasks\n\nThe worker split this task. Each piece that is done was reviewed by itself; "
                        "you check the whole, including what the pieces don't cover and how they were put "
                        "together. Their files are in /task/work/<id>/.\n" + "\n".join(lines))
    return "\n\n".join(parts)


def final_first(run: Run, reviewer: str, deliverable: str, summary: str) -> str:
    tasks = []
    for t in run.db.children(None):
        line = f"- `{t['id']}` ({t['status']}): {t['title']}"
        if t["status"] == "done" and t["summary"]:
            line += f"\n  Report: {t['summary'][:600]}"
        elif t["status"] == "failed":
            line += f"\n  Failed: {(t['findings'] or '')[:400]}"
        tasks.append(line)
    parts = [
        identity(reviewer, "reviewer"),
        "You review the finished work of the whole team.",
        f"# The task the user gave\n\n{run.db.get_meta('task')}",
        f"# The goal the lead set\n\n{run.db.get_meta('goal')}",
        f"# What to check\n\n{run.db.get_meta('done_when')}",
        f"# The deliverable\n\n/task/deliverable/{deliverable}\n\nThe lead's summary:\n{summary}",
        "# The tasks\n\n" + ("\n".join(tasks) or "(none)"),
        _store(run) + f"\n\nSignals logged (conflicting records): {run.store.signals}.",
        "Check the deliverable against the store and the sources, not only against itself: a claim in it should "
        "trace to a record or a file. Check too that it is honest about what was not found.",
    ]
    return "\n\n".join(parts)
