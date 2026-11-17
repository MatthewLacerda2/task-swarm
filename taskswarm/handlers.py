"""What each tool call does. Returns (text for the model, is_error, outcome).

An outcome ends the agent's assignment: a worker's report, a reviewer's verdict, the
lead's finish. Everything else returns None and the agent carries on.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import plan, safeio, sandbox
from .db import OPEN
from .run import Run, work_dir
from .tools import ALLOWED

MESSAGE_CHARS = 4_000
DELIVERABLE_MAX = 50 * 1024 * 1024


@dataclass
class Outcome:
    kind: str                      # planned | report | verdict | finish | idle | failed
    data: dict = field(default_factory=dict)
    reason: str = ""               # for failed: hard_stop, agent_budget, task_budget, context, api_error, calls, ...


Result = tuple[str, bool, Outcome | None]


def _own_file(run: Run, agent: str, path: object) -> tuple[Path, str]:
    """(agent's host folder, path relative to it) for a path the agent gave."""
    if not isinstance(path, str) or not path.strip():
        raise safeio.UnsafePath("`path` is missing")
    home = PurePosixPath(sandbox.ROOT) / "work" / agent
    p = PurePosixPath(path.strip())
    if not p.is_absolute():
        p = home / p
    parts = [x for x in p.parts if x != "."]
    if ".." in parts or tuple(parts[:len(home.parts)]) != home.parts or len(parts) == len(home.parts):
        raise safeio.UnsafePath(f"the file must be inside your folder, {home}")
    return work_dir(run, agent), str(PurePosixPath(*parts[len(home.parts):]))


async def handle(run: Run, agent: str, closed: bool, name: str, inp: object) -> Result:
    row = run.db.agent(agent)
    role = row["role"]
    if name not in ALLOWED[role]:
        who = next((r for r, names in ALLOWED.items() if name in names), None)
        return (f"`{name}` is not for your role ({role})." + (f" It is for the {who}." if who else ""), True, None)
    if not isinstance(inp, dict):
        return f"`{name}` needs an object as input.", True, None
    return await _HANDLERS[name](run, agent, row, closed, inp)


async def _plan(run: Run, agent: str, row, closed: bool, inp: dict) -> Result:
    if row["role"] == "worker":
        if closed or run.db.task(agent)["status"] != "running":
            return "Your task is already handed in, so it can't be split now.", True, None
        ok, text = plan.apply(run, inp, owner=agent)
        if ok:
            text += ("\nYou own these sub-tasks: their reports come to you, not to the lead. End your turn to wait "
                     "for them. Your own report still has to cover the whole task.")
        return text, not ok, None
    ok, text = plan.apply(run, inp)
    if ok and row["role"] == "planner":
        return text + "\nThe lead takes it from here.", False, Outcome("planned")
    return text, not ok, None


async def _send_message(run: Run, agent: str, row, closed: bool, inp: dict) -> Result:
    to, body = inp.get("to"), inp.get("message")
    if not isinstance(body, str) or not body.strip():
        return "The message is empty.", True, None
    if len(body) > MESSAGE_CHARS:
        return f"The message is too long ({len(body):,} > {MESSAGE_CHARS:,} characters). Put the detail in a file " \
               "and say where it is.", True, None
    if to == agent:
        return "That's you.", True, None
    if not isinstance(to, str) or run.db.agent(to) is None:
        task = run.db.task(to) if isinstance(to, str) else None
        if task is not None:
            return (f"`{to}` hasn't started yet ({task['status']}), so nobody is there to read it. If you are the "
                    "lead, put it in the task's brief with `plan`."), True, None
        names = ", ".join(f"`{a['id']}`" for a in run.db.agents())
        return f"There is no agent `{to}`. Agents so far: {names}.", True, None
    run.send(to, agent, body.strip())
    run.db.event(agent, "message", {"to": to, "chars": len(body)})
    return f"Sent to `{to}`.", False, None


def _jsonl(fd: int, bad: list[int]):
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except ValueError:
                bad.append(n)
                yield None


async def _store_write(run: Run, agent: str, row, closed: bool, inp: dict) -> Result:
    collection = inp.get("collection")
    if collection not in run.store.schema:
        have = ", ".join(f"`{c}`" for c in run.store.schema) or "none yet"
        return (f"There is no collection `{collection}` (collections: {have}). Only the lead can add one, with "
                "`plan`; ask with `send_message` if the task needs it."), True, None
    records, path = inp.get("records"), inp.get("path")
    if (records is None) == (path is None):
        return "Give either `records` or `path`, not both.", True, None
    task = row["task"]
    bad_lines: list[int] = []
    if path is not None:
        try:
            root, rel = _own_file(run, agent, path)
            fd = safeio.open_beneath(root, rel)
        except safeio.UnsafePath as e:
            return f"Can't read that file: {e}", True, None
        if os.fstat(fd).st_size > run.cfg.import_max_mb * 1024 * 1024:
            os.close(fd)
            return f"The file is larger than {run.cfg.import_max_mb:,} MB; split it.", True, None
        source = _jsonl(fd, bad_lines)
    elif isinstance(records, list):
        source = iter(records)
    else:
        return "`records` must be a list of objects.", True, None
    res = await asyncio.to_thread(run.store.write, collection, source, agent, task)
    text = res.text(collection)
    if bad_lines:
        text += f"\n{len(bad_lines)} of the rejected lines are not valid JSON (first: line {bad_lines[0]})."
    run.db.event(agent, "store_write", {"collection": collection, "accepted": res.accepted, "rejected": res.rejected,
                                        "duplicates": res.duplicates, "conflicts": res.conflicts})
    return text, res.accepted == 0 and res.rejected > 0, None


async def _report(run: Run, agent: str, row, closed: bool, inp: dict) -> Result:
    if closed:
        return "Your task is already handed in; you were woken to answer a message. Answer with `send_message`.", True, None
    summary = inp.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return "`summary` is empty: the report is what the rest of the team sees.", True, None
    limit = run.cfg.summary_chars
    if len(summary) > limit:
        return (f"The summary is {len(summary):,} characters; the limit is {limit:,}. Keep the result and what the "
                "lead needs; put the detail in a file in your folder and name it."), True, None
    still = [t["id"] for t in run.db.children(agent) if t["status"] in OPEN]
    if still:
        return (f"Your sub-tasks are still open: {', '.join(still)}. End your turn to wait for them, or cancel them "
                "with `plan`. Your report covers the whole task, so it comes after them."), True, None
    how = inp.get("how") if isinstance(inp.get("how"), str) else ""
    return ("Handed in. A reviewer will check it; if it doesn't pass you will get the findings.", False,
            Outcome("report", {"summary": summary.strip(), "how": how.strip()[:limit]}))


async def _verdict(run: Run, agent: str, row, closed: bool, inp: dict) -> Result:
    if closed:
        return "Your review is already handed in; answer with `send_message`.", True, None
    passed, findings = inp.get("passed"), inp.get("findings")
    if not isinstance(passed, bool):
        return "`passed` must be true or false.", True, None
    if not isinstance(findings, str) or len(findings.strip()) < 20:
        return "`findings` is missing: say what you checked and how, and each problem if it didn't pass.", True, None
    return "Verdict recorded.", False, Outcome("verdict", {"passed": passed, "findings": findings.strip()[:8000]})


async def _finish(run: Run, agent: str, row, closed: bool, inp: dict) -> Result:
    db = run.db
    if not db.get_meta("goal"):
        return "Set the plan first (`plan` with `goal` and `done_when`), even if it has no tasks.", True, None
    summary = inp.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return "`summary` is empty.", True, None
    busy = [t["id"] for t in db.tasks() if t["status"] in ("running", "reviewing")]
    pending = [t["id"] for t in db.tasks() if t["status"] == "pending"]
    if busy:
        return (f"Tasks are still being worked on: {', '.join(busy)}. End your turn; you are woken when they settle. "
                "Or cancel them with `plan`."), True, None
    if pending and not run.budget.winding_down:
        return (f"Tasks have not started: {', '.join(pending)}. Cancel them with `plan` if they are not needed, or "
                "fix what blocks them."), True, None
    try:
        root, rel = _own_file(run, agent, inp.get("path"))
        data = safeio.read_beneath(root, rel, DELIVERABLE_MAX)
    except safeio.UnsafePath as e:
        return f"Can't read the deliverable: {e}", True, None
    if not data.strip():
        return "The deliverable file is empty.", True, None
    for tid in pending:      # the budget is used up: what never started is dropped
        db.update_task(tid, status="cancelled")
    name = Path(rel).name
    out = run.cfg.run_dir / "deliverable"     # mounted in running sandboxes: empty it, don't replace it
    for old in out.iterdir():
        old.unlink()
    (out / name).write_bytes(data)
    return ("Handed in. A final reviewer checks it against `done_when`; end your turn.", False,
            Outcome("finish", {"file": name, "summary": summary.strip()[:4000]}))


_HANDLERS = {
    "plan": _plan, "send_message": _send_message, "store_write": _store_write,
    "report": _report, "verdict": _verdict, "finish": _finish,
}
