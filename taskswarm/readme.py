"""The README.md of a run's folder: what the run was and where everything in it is.

Written by the tool, never by an agent, from what the database and the store hold, so
it is the same for every run and can't drift from the files. It is rewritten each time
the run starts or stops. Whoever opens the folder later (a person, or an agent of
another run given it as data) reads this first and knows where to look.
"""

from __future__ import annotations

import datetime as dt

from .run import Run

STATUS = {
    "done": "done: the final review passed",
    "done_unverified": "finished, but the final review did not pass",
    "paused": "paused at the hard stop; `task-swarm resume` continues it",
    "stalled": "stalled: the lead stopped acting",
    "failed": "failed",
    "interrupted": "interrupted; `task-swarm resume` continues it",
    "running": "running, or killed while running; `task-swarm resume` continues it",
}


def _when(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


def render(run: Run) -> str:
    db, cfg = run.db, run.cfg
    status = db.get_meta("status")
    reason = db.get_meta("status_reason")
    first = db.one("SELECT MIN(created) AS t FROM agents")["t"]
    models = f"lead {cfg.lead_model}, workers and reviewers {cfg.worker_model}"
    if db.agent("planner") is not None:
        models = f"plan drafted by {cfg.plan_model}, " + models
    out = [
        f"# Run `{cfg.run_id}`",
        "Written by task-swarm each time the run starts or stops; edits are overwritten.",
        "\n".join([
            f"- Status: {STATUS.get(status, status)}" + (f" ({reason})" if reason else ""),
            f"- Started: {_when(first) if first else 'not yet'}",
            f"- Spent: US$ {db.total_spent():.3f} of a budget of US$ {cfg.run_budget:.2f}",
            f"- Models: {models}",
        ]),
        f"## The task, as the user gave it\n\n{db.get_meta('task')}",
    ]
    if goal := db.get_meta("goal"):
        out.append(f"## What the team set out to deliver\n\nGoal: {goal}\n\nDone when: {db.get_meta('done_when')}")

    deliverable = db.get_meta("deliverable")
    where = []
    if deliverable:
        where.append(f"| `deliverable/{deliverable['file']}` | The result the lead handed in |")
    where += [f"| `store/{name}.jsonl` | {n:,} records: {run.store.schema[name].get('description') or name} |"
              for name, n in run.store.counts.items()]
    if run.store.schema:
        where.append("| `store/schema.json` | The fields of every collection, as declared in the plan |")
        where.append(f"| `store/signals.jsonl` | {run.store.signals} records that conflicted with a stored one |")
        where.append(f"| `store/changes.jsonl` | {run.store.changes} corrections: which records were updated or "
                     "deleted, by whom and why |")
    chans = db.all("SELECT c.name, c.created, c.created_by, COUNT(p.id) AS n FROM channels c"
                   " LEFT JOIN posts p ON p.channel = c.name GROUP BY c.name ORDER BY c.created")
    where += [f"| `channels/{c['name']}.jsonl` | {c['n']} tips agents posted for each other |" for c in chans]
    where += [
        "| `work/<agent>/` | Each agent's own files: downloads, scripts, notes |",
        "| `outputs/<agent>/NNNN.txt` | The full output of every command an agent ran |",
        "| `run.db` | SQLite: the plan (`tasks`), every conversation (`messages`), costs (`usage`), commands |",
    ]
    out.append("## Where things are\n\n| Path | What it holds |\n|---|---|\n" + "\n".join(where))

    if run.store.schema:
        out.append(
            "## The store\n\nOne JSON object per line. Unless a collection is derived, every record carries "
            "`source` (the URL it was read from), `retrieved` (the date) and optionally `quote` (the words on the "
            "page). The tool adds `_id` (the record's number), `_by` (the agent), `_task` and `_at` (when it was stored), "
            "and `_edited_by` and `_edited_at` on a record that was corrected.\n\n"
            + run.store.describe())

    tasks = db.tasks()
    if tasks:
        lines = []
        for t in tasks:
            line = f"- `{t['id']}` ({t['status']}): {t['title']}"
            note = t["summary"] if t["status"] == "done" else t["findings"] if t["status"] == "failed" else None
            if note:
                line += "\n  " + " ".join(note.split())[:400]
            lines.append(line)
        out.append("## Tasks\n\nA task's files are in `work/<task id>/` and its records have that `_task`.\n\n"
                   + "\n".join(lines))
    if chans:
        out.append("## Channels\n\n" + "\n".join(
            f"- `{c['name']}`: {c['n']} posts, created {_when(c['created'])} by `{c['created_by']}`" for c in chans))
    if deliverable:
        out.append(f"## What the lead said about the result\n\n{deliverable['summary']}")
    if findings := db.get_meta("final_findings"):
        out.append(f"## What the final reviewer found\n\n{findings}")
    return "\n\n".join(out) + "\n"


def write(run: Run) -> None:
    path = run.cfg.run_dir / "README.md"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(render(run), encoding="utf-8")
    tmp.replace(path)
