"""Renders what agents are told: the system prompt (identical for every agent of a run,
so it is read from one shared cache) and each role's first message."""

from __future__ import annotations

import json

from . import config
from .run import Run


def system_prompt(cfg: config.RunConfig) -> str:
    template = (config.PROMPTS / "system.md").read_text()
    return template.format(**(cfg.to_dict() | {"hard_stop": cfg.hard_stop}))


def identity(agent: str, role: str) -> str:
    what = {"lead": "the lead of this team", "worker": "a worker", "reviewer": "a reviewer"}[role]
    return f"You are `{agent}`, {what}."


def compacted_note(agent: str, role: str) -> str:
    return (
        f"[Your conversation was compacted into the summary above to save memory. {identity(agent, role)} "
        "Anything not in the summary is gone, except what you saved in files and in the store. Continue.]"
    )


def lead_first(task: str) -> str:
    return f"{identity('lead', 'lead')}\n\n# The task\n\n{task}"


def _store(run: Run) -> str:
    return f"# The store\n\n{run.store.describe()}"


def worker_first(run: Run, task) -> str:
    parts = [
        identity(task["id"], "worker"),
        f"# The goal of the whole team\n\n{run.db.get_meta('goal')}",
        f"# Your task: {task['title']}\n\n{task['brief']}",
        f"# How your work will be checked\n\n{task['verify']}",
    ]
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
        f"The worker's files are in /task/work/{task['id']}/. The records it stored have "
        f"\"_task\": \"{task['id']}\". This is review {task['attempts']} of at most {run.cfg.max_attempts}.",
        _store(run),
    ]
    return "\n\n".join(parts)


def final_first(run: Run, reviewer: str, deliverable: str, summary: str) -> str:
    tasks = []
    for t in run.db.tasks():
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
