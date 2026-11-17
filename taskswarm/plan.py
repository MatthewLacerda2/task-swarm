"""The plan: the goal, how the finished work is checked, the store's structure, and the
tasks with their dependencies. The dependency graph is the schedule: a task starts
when every task it is blocked by has passed review.

`apply` is the only way the plan changes. It checks the whole change first and
writes nothing if any check fails, and its message names every problem and what to
do about it.
"""

from __future__ import annotations

import json
import logging
import re
import time

from .run import Run

log = logging.getLogger("taskswarm")

ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
RESERVED = {"lead", "final", "system"}
MIN_BRIEF = 100
MIN_VERIFY = 20
STARTED = ("running", "reviewing", "done", "failed")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def apply(run: Run, inp: dict) -> tuple[bool, str]:
    """Apply a `plan` call. Returns (ok, message for the lead)."""
    db = run.db
    problems: list[str] = []

    goal = _text(inp.get("goal")) or db.get_meta("goal")
    done_when = _text(inp.get("done_when")) or db.get_meta("done_when")
    if not goal:
        problems.append("`goal` is missing: say in a sentence or two what the finished work is.")
    if not done_when:
        problems.append("`done_when` is missing: say what a reviewer who has only the deliverable and the store "
                        "should check to decide the goal is met.")

    collections = inp.get("collections") or {}
    problems += run.store.merge_problems(collections)

    existing = {t["id"]: dict(t) for t in db.tasks()}
    tasks = {tid: t | {"blocked_by": json.loads(t["blocked_by"])} for tid, t in existing.items()}
    touched: list[str] = []

    cancel = inp.get("cancel") or []
    if not isinstance(cancel, list):
        problems.append("`cancel` must be a list of task ids.")
        cancel = []
    for tid in cancel:
        if tid not in tasks:
            problems.append(f"cancel: there is no task `{tid}`.")
        elif tasks[tid]["status"] in ("done", "failed", "cancelled"):
            problems.append(f"cancel: `{tid}` is already {tasks[tid]['status']}; leave it out.")
        else:
            tasks[tid]["status"] = "cancelled"

    new = inp.get("tasks") or []
    if not isinstance(new, list):
        problems.append("`tasks` must be a list.")
        new = []
    for i, t in enumerate(new):
        if not isinstance(t, dict):
            problems.append(f"tasks[{i}] must be an object.")
            continue
        tid = t.get("id")
        where = f"task `{tid}`"
        if not isinstance(tid, str) or not ID_RE.match(tid) or tid in RESERVED:
            problems.append(f"tasks[{i}]: `id` must be 1-24 of a-z, 0-9 and -, starting with a letter, and not one of "
                            f"{sorted(RESERVED)}.")
            continue
        if tid in touched:
            problems.append(f"{where} appears twice in this call.")
            continue
        if tid in cancel:
            problems.append(f"{where} is both cancelled and defined in this call; pick one.")
            continue
        if tid in existing and existing[tid]["status"] in STARTED + ("cancelled",):
            problems.append(f"{where} already exists and is {existing[tid]['status']}, so it can't be redefined. "
                            "Give the new work a new id.")
            continue
        title, brief, verify = _text(t.get("title")), _text(t.get("brief")), _text(t.get("verify"))
        if not title:
            problems.append(f"{where}: `title` is missing.")
        if len(brief) < MIN_BRIEF:
            problems.append(f"{where}: the brief is too short to stand alone ({len(brief)} characters). The brief is "
                            "all the worker knows: say what to produce, what is already known or ruled out, and "
                            "where the output goes.")
        if len(verify) < MIN_VERIFY:
            problems.append(f"{where}: `verify` is missing or too short. Say how a reviewer who hasn't seen the work "
                            "can check it. A piece that can't be checked isn't ready to hand out.")
        blocked_by = t.get("blocked_by") or []
        if not isinstance(blocked_by, list) or any(not isinstance(b, str) for b in blocked_by):
            problems.append(f"{where}: `blocked_by` must be a list of task ids.")
            blocked_by = []
        tasks[tid] = {
            "id": tid, "title": title, "brief": brief, "verify": verify,
            "blocked_by": list(dict.fromkeys(blocked_by)), "status": "pending",
        }
        touched.append(tid)

    live = {tid: t for tid, t in tasks.items() if t["status"] != "cancelled"}
    for tid, t in live.items():
        if t["status"] != "pending":
            continue
        for b in t["blocked_by"]:
            if b == tid:
                problems.append(f"task `{tid}` is blocked by itself.")
            elif b not in tasks:
                problems.append(f"task `{tid}` is blocked by `{b}`, which doesn't exist.")
            elif tasks[b]["status"] == "cancelled":
                problems.append(f"task `{tid}` is blocked by `{b}`, which is cancelled. Define `{tid}` again with a "
                                "new `blocked_by`, or cancel it too.")
            elif tasks[b]["status"] == "failed" and tid in touched:
                problems.append(f"task `{tid}` is blocked by `{b}`, which failed and will never unblock it. Point it "
                                "at the task that replaces it.")
    if cycle := _cycle(live):
        problems.append("the tasks block each other in a circle: " + " -> ".join(cycle) + ".")
    if len(live) > run.cfg.max_tasks:
        problems.append(f"the plan would have {len(live)} tasks; the limit is {run.cfg.max_tasks}.")

    if problems:
        return False, "Nothing was changed. Fix these and call `plan` again:\n" + "\n".join(f"- {p}" for p in problems)

    now = time.time()
    seq = max((t["seq"] for t in existing.values()), default=0)
    with db.transaction():
        db.set_meta("goal", goal)
        db.set_meta("done_when", done_when)
        for tid in cancel:
            db.update_task(tid, status="cancelled")
        for tid in touched:
            t = tasks[tid]
            if tid in existing:
                db.update_task(tid, title=t["title"], brief=t["brief"], verify=t["verify"],
                               blocked_by=json.dumps(t["blocked_by"]))
            else:
                seq += 1
                db.execute(
                    "INSERT INTO tasks(id, seq, title, brief, verify, blocked_by, status, created, updated)"
                    " VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (tid, seq, t["title"], t["brief"], t["verify"], json.dumps(t["blocked_by"]), now, now),
                )
    if collections:
        run.store.merge(collections)
    db.event("lead", "plan", {"tasks": touched, "cancel": cancel, "collections": sorted(collections)})
    log.info("plan: %d tasks defined, %d cancelled, %d collections", len(touched), len(cancel), len(collections))
    run.wake.set()
    return True, "Plan saved.\n" + overview(run)


def _cycle(tasks: dict[str, dict]) -> list[str] | None:
    state: dict[str, int] = {}

    def visit(tid: str, path: list[str]) -> list[str] | None:
        if state.get(tid) == 2:
            return None
        if state.get(tid) == 1:
            return path[path.index(tid):] + [tid]
        state[tid] = 1
        for b in tasks[tid]["blocked_by"]:
            if b in tasks and (found := visit(b, path + [tid])):
                return found
        state[tid] = 2
        return None

    for tid in tasks:
        if found := visit(tid, []):
            return found
    return None


def ready(run: Run) -> list[str]:
    """Pending tasks whose blockers have all passed review, oldest first."""
    status = {t["id"]: t["status"] for t in run.db.tasks()}
    return [
        t["id"] for t in run.db.tasks()
        if t["status"] == "pending" and all(status.get(b) == "done" for b in json.loads(t["blocked_by"]))
    ]


def stuck(run: Run) -> list[str]:
    """Pending tasks that can never start: something they depend on failed."""
    status = {t["id"]: t["status"] for t in run.db.tasks()}
    return [
        t["id"] for t in run.db.tasks()
        if t["status"] == "pending" and any(status.get(b) == "failed" for b in json.loads(t["blocked_by"]))
    ]


def overview(run: Run) -> str:
    rows = run.db.tasks()
    if not rows:
        return "The plan has no tasks."
    lines = []
    for t in rows:
        line = f"- `{t['id']}` {t['status']}: {t['title']}"
        blockers = json.loads(t["blocked_by"])
        if blockers and t["status"] == "pending":
            line += f" (blocked by {', '.join(blockers)})"
        lines.append(line)
    if bad := stuck(run):
        lines.append(f"Can never start, because a task they depend on failed: {', '.join(bad)}.")
    return "\n".join(lines)
