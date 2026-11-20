"""What each tool call does. Returns (text for the model, is_error, outcome).

An outcome ends the agent's assignment: a worker's report, a reviewer's verdict, the
lead's finish. Everything else returns None and the agent carries on.

A review has three results, and each goes to whoever can act on it: `pass` closes the
task; `fail` goes back to the worker, who can fix it; `partial` (right as far as it
goes, something asked for is missing, another try would not get it) goes to the task's
owner, who settles it with `decide`. Only `fail` counts against the task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import channels, plan, safeio, sandbox
from .db import OPEN
from .run import Run, work_dir
from .tools import ALLOWED

log = logging.getLogger("taskswarm")

MESSAGE_CHARS = 4_000
DELIVERABLE_MAX = 50 * 1024 * 1024
RESULTS = ("pass", "partial", "fail")
NOT_DONE = "Not delivered, and why:"      # heads that part of a report, wherever the report is shown
NOTE_MIN = 20


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
                     "for them: you are woken the moment one settles, and waiting inside a command (`sleep`) only "
                     "delays you. Your own report still has to cover the whole task.")
        return text, not ok, None
    ok, text = plan.apply(run, inp)
    if ok and row["role"] == "planner":
        return text + "\nThe lead takes it from here.", False, Outcome("planned")
    if ok:
        text += ("\nEnd your turn to wait: you are woken the moment a task settles. Waiting inside a command "
                 "(`sleep`) only delays you.")
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
    # Only a task's owner can change what the task is. What the owner tells its worker is
    # kept on the task, so the reviewer checks the work against the brief as it now stands.
    task = run.db.task(to)
    if task is not None and task["status"] in OPEN and (task["parent"] or "lead") == agent:
        decisions = json.loads(task["decisions"] or "[]") + [body.strip()]
        run.db.update_task(to, decisions=json.dumps(decisions, ensure_ascii=False))
        return (f"Sent to `{to}`. You own that task, so this is recorded on it as your decision and its reviewer "
                "will see it."), False, None
    return f"Sent to `{to}`.", False, None


async def _post(run: Run, agent: str, row, closed: bool, inp: dict) -> Result:
    text, is_error = channels.post(run, agent, inp.get("channel"), inp.get("text"))
    return text, is_error, None


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


REASON_MIN = 10


def _store_change(action: str):
    """store_update and store_delete: the same picking, the same owners, the same log."""

    async def handler(run: Run, agent: str, row, closed: bool, inp: dict) -> Result:
        collection = inp.get("collection")
        if collection not in run.store.schema:
            have = ", ".join(f"`{c}`" for c in run.store.schema) or "none yet"
            return f"There is no collection `{collection}` (collections: {have}).", True, None
        if row["role"] == "worker" and (closed or run.db.task(agent)["status"] != "running"):
            return ("Your task is handed in, so what it stored is what is being checked and can't change now. If the "
                    "review sends it back you can correct it then; otherwise tell `lead` what is wrong."), True, None
        reason = inp.get("reason")
        if not isinstance(reason, str) or len(reason.strip()) < REASON_MIN:
            return "`reason` is missing: say in a sentence why these records change. It is logged.", True, None
        ids, where, new_values = inp.get("ids"), inp.get("where"), inp.get("set")
        if (ids is None) == (where is None):
            return "Pick the records with either `ids` or `where`, not both.", True, None
        if ids is not None and not (isinstance(ids, list) and ids and all(type(i) is int for i in ids)):
            return "`ids` must be a list of record numbers (the `_id` of each record).", True, None
        if where is not None and not (isinstance(where, dict) and where):
            return ("`where` must name at least one field and its value. To pick everything a task stored, use "
                    '{"_task": "<task id>"}.'), True, None
        if action == "update" and not (isinstance(new_values, dict) and new_values):
            return "`set` must map at least one field to its new value.", True, None

        # A record belongs to whoever wrote it. A task's owner may also change what its tasks wrote:
        # the lead any record, a worker the records of its sub-tasks.
        def may_change(rec: dict) -> bool:
            return (row["role"] == "lead" or rec.get("_by") == agent
                    or str(rec.get("_task") or "").startswith(agent + "."))

        res = await asyncio.to_thread(run.store.change, collection, agent, may_change, reason.strip(), ids, where,
                                      new_values if action == "update" else None)
        run.db.event(agent, f"store_{action}", {"collection": collection, "changed": res.changed,
                                                 "refused": bool(res.problems)})
        return res.text(collection, action), bool(res.problems), None

    return handler


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
    not_done = inp.get("not_done").strip() if isinstance(inp.get("not_done"), str) else ""
    if len(not_done) > limit // 2:
        return (f"`not_done` is {len(not_done):,} characters; the limit is {limit // 2:,}. Name each thing and the "
                "reason; put the detail in a file in your folder."), True, None
    still = [t["id"] for t in run.db.children(agent) if t["status"] in OPEN]
    if still:
        text = (f"Your sub-tasks are still open: {', '.join(still)}. End your turn to wait for them, or cancel them "
                "with `plan`. Your report covers the whole task, so it comes after them.")
        if waiting := plan.undecided(run, agent):
            text += f" Waiting for your decision (`decide`): {', '.join(waiting)}."
        return text, True, None
    how = inp.get("how") if isinstance(inp.get("how"), str) else ""
    report = summary.strip() + (f"\n\n{NOT_DONE}\n{not_done}" if not_done else "")
    return ("Handed in. A reviewer will check it. If something in it is wrong you get the findings to fix; if "
            "something is missing that you could not get, whoever gave you the task decides what happens.", False,
            Outcome("report", {"summary": report, "how": how.strip()[:limit]}))


async def _verdict(run: Run, agent: str, row, closed: bool, inp: dict) -> Result:
    if closed:
        return "Your review is already handed in; answer with `send_message`.", True, None
    result, findings = inp.get("result"), inp.get("findings")
    if result not in RESULTS:
        return "`result` must be `pass`, `partial` or `fail`.", True, None
    if not isinstance(findings, str) or len(findings.strip()) < 20:
        return "`findings` is missing: say what you checked and how, and each problem if it didn't pass.", True, None
    missing = inp.get("missing").strip() if isinstance(inp.get("missing"), str) else ""
    if result == "partial" and len(missing) < 20:
        return ("`missing` is empty: with `partial`, list each thing asked for that is not delivered and what you "
                "did to check that it can't be."), True, None
    if result != "partial" and missing:
        return ("`missing` goes with `partial` only. If what is missing could still be delivered by trying again, "
                "the result is `fail` and it belongs in `findings`. If it can't be, and the rest is right, the "
                "result is `partial`."), True, None
    return "Verdict recorded.", False, Outcome("verdict", {"result": result, "findings": findings.strip()[:8000],
                                                           "missing": missing[:4000]})


async def _decide(run: Run, agent: str, row, closed: bool, inp: dict) -> Result:
    """The owner of a task settles a partial review: the task is done as it is, or its worker continues
    under a new decision. Until then nothing that is blocked by the task starts."""
    db = run.db
    owner = None if row["role"] == "lead" else agent
    if owner and (closed or db.task(agent)["status"] != "running"):
        return "Your task is already handed in, so its sub-tasks are not yours to settle now.", True, None
    waiting = plan.undecided(run, owner)
    tid = inp.get("task")
    if owner and isinstance(tid, str) and not tid.startswith(owner + "."):
        tid = f"{owner}.{tid}"
    if tid not in waiting:
        task = db.task(tid) if isinstance(tid, str) else None
        if task is None or task["parent"] != owner:
            text = f"You have no task `{tid}`."
        else:
            text = f"`{tid}` is {task['status']}; `decide` settles a task whose review ended in `partial`."
        return (text + (f" Waiting for your decision: {', '.join(waiting)}." if waiting
                        else " No task is waiting for your decision.")), True, None
    accept = inp.get("accept")
    if not isinstance(accept, bool):
        return "`accept` must be true or false.", True, None
    note = inp.get("note").strip() if isinstance(inp.get("note"), str) else ""
    if len(note) > MESSAGE_CHARS:
        return f"`note` is too long ({len(note):,} > {MESSAGE_CHARS:,} characters).", True, None
    task = db.task(tid)
    if accept:
        gaps = task["gaps"] + (f"\n\nWhy `{agent}` accepted it like this: {note}" if note else "")
        db.update_task(tid, status="done", gaps=gaps)
        db.event(agent, "decide", {"task": tid, "accept": True})
        log.info("task %s accepted by %s with something missing", tid, agent)
        run.wake.set()
        return (f"`{tid}` is done as delivered. What is missing stays recorded on it: the tasks that build on it and "
                "the final reviewer are told. Say it in what you hand in too." + plan.what_next(run, owner)), False, None
    if run.budget.winding_down:
        return ("The run's budget is used up, so the task can't go back to its worker. Accept it as delivered, or "
                "cancel it with `plan`, and say what is missing in what you hand in."), True, None
    if len(note) < NOTE_MIN:
        return ("`note` is missing: to send the task back, say what its worker should do instead. Asking for the "
                "same thing again gets the same result."), True, None
    decisions = json.loads(task["decisions"] or "[]") + [note]
    db.update_task(tid, status="running", decisions=json.dumps(decisions, ensure_ascii=False))
    run.send(tid, "system",
             f"Your report was reviewed. What you delivered is right, but this is missing:\n\n{task['gaps']}\n\n"
             f"`{agent}`, who gave you the task, decided:\n\n{note}\n\nThat decision is now part of your task. "
             "Carry on from what you have and call `report` again. This did not count as a failed review.")
    db.event(agent, "decide", {"task": tid, "accept": False})
    log.info("task %s sent back by %s with a decision", tid, agent)
    return (f"`{tid}` goes back to its worker with your decision, which is recorded on the task: its next reviewer "
            "checks the work against it. End your turn to wait."), False, None


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
    if waiting := plan.undecided(run):
        return (f"Tasks wait for your decision: {', '.join(waiting)}. Settle each with `decide`: accept it as "
                "delivered, or send it back with what to do instead."), True, None
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
    "plan": _plan, "send_message": _send_message, "post": _post, "store_write": _store_write,
    "store_update": _store_change("update"), "store_delete": _store_change("delete"),
    "report": _report, "decide": _decide, "verdict": _verdict, "finish": _finish,
}
