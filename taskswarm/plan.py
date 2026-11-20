"""The plan: the goal, how the finished work is checked, the store's structure, and the
tasks with their dependencies. The dependency graph is the schedule: a task starts
when every task it is blocked by has passed review.

The lead owns the plan. A worker may split its own task into sub-tasks, which makes
it the owner of those. What keeps that from running away:

  - depth: a sub-task can't be split again (`max_depth`);
  - width: one task splits into at most `max_subtasks` pieces, and never into one;
  - money: a sub-team spends its parent task's budget, so splitting buys parallel
    work and fresh memories, never more budget (`Budget.tree_left`);
  - scope: only the lead sets the goal, `done_when` and the store's structure;
  - accountability: the parent hands in its own report for the whole task, and can't
    until its sub-tasks are settled.

`apply` is the only way the plan changes. It checks the whole change first and
writes nothing if any check fails, and its message names every problem and what to
do about it.
"""

from __future__ import annotations

import json
import logging
import re
import time

from .db import OPEN
from .run import Run

log = logging.getLogger("taskswarm")

ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
RESERVED = {"lead", "final", "system", "planner"}
REVIEW_RE = re.compile(r"^review\d*$")
MIN_BRIEF = 100
MIN_VERIFY = 20
STARTED = ("running", "reviewing", "partial", "done", "failed")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def depth(task_id: str) -> int:
    return task_id.count(".") + 1


def apply(run: Run, inp: dict, owner: str | None = None) -> tuple[bool, str]:
    """Apply a `plan` call. `owner` is None for the lead, or the id of the task a worker is splitting.

    Returns (ok, message for the caller).
    """
    db, cfg = run.db, run.cfg
    if inp.get("tasks") and run.budget.winding_down:
        return False, ("Nothing was changed: the run's budget is used up, so no new task can start. Hand in what "
                       "exists and say what is missing.")
    problems: list[str] = []
    prefix = f"{owner}." if owner else ""

    def full(name: object) -> object:
        """A sub-task may be named `x` or `<owner>.x`."""
        if not isinstance(name, str) or not owner:
            return name
        return name if name.startswith(prefix) else prefix + name

    goal, done_when = db.get_meta("goal"), db.get_meta("done_when")
    collections = inp.get("collections") or {}
    if owner:
        if depth(owner) >= cfg.max_depth:
            if cfg.max_depth == 1:
                why = "in this run only the lead splits work"
            else:
                why = f"your task is already a piece of `{owner.rsplit('.', 1)[0]}`, and a piece can't be split again"
            return False, (f"Nothing was changed: {why}. Do the task yourself. If it is too big for one agent, hand "
                           "in what you managed and say in the report what is left.")
        if _text(inp.get("goal")) or _text(inp.get("done_when")) or collections:
            problems.append("only the lead sets the goal, `done_when` and the store's collections. If your task "
                            "needs a new collection or field, ask `lead` with `send_message`.")
            collections = {}
    else:
        goal = _text(inp.get("goal")) or goal
        done_when = _text(inp.get("done_when")) or done_when
        if not goal:
            problems.append("`goal` is missing: say in a sentence or two what the finished work is.")
        if not done_when:
            problems.append("`done_when` is missing: say what a reviewer who has only the deliverable and the store "
                            "should check to decide the goal is met.")
        problems += run.store.merge_problems(collections)

    existing = {t["id"]: dict(t) for t in db.tasks()}
    tasks = {tid: t | {"blocked_by": json.loads(t["blocked_by"])} for tid, t in existing.items()}
    touched: list[str] = []

    cancel_in = inp.get("cancel") or []
    if not isinstance(cancel_in, list):
        problems.append("`cancel` must be a list of task ids.")
        cancel_in = []
    cancel = [full(c) for c in cancel_in]
    for tid in cancel:
        if tid not in tasks or tasks[tid]["parent"] != owner:
            problems.append(f"cancel: you have no task `{tid}`.")
        elif tasks[tid]["status"] not in OPEN:
            problems.append(f"cancel: `{tid}` is already {tasks[tid]['status']}; leave it out.")
        else:
            tasks[tid]["status"] = "cancelled"
    # What was split off a cancelled task goes with it.
    cascade = [t["id"] for c in cancel if c in existing for t in db.descendants(c) if t["status"] in OPEN]
    for tid in cascade:
        tasks[tid]["status"] = "cancelled"

    new = inp.get("tasks") or []
    if not isinstance(new, list):
        problems.append("`tasks` must be a list.")
        new = []
    for i, t in enumerate(new):
        if not isinstance(t, dict):
            problems.append(f"tasks[{i}] must be an object.")
            continue
        tid = full(t.get("id"))
        name = tid[len(prefix):] if isinstance(tid, str) else None
        if name is None or not ID_RE.match(name) or name in RESERVED or REVIEW_RE.match(name):
            problems.append(f"tasks[{i}]: `id` must be 1-24 of a-z, 0-9 and -, starting with a letter, and not one of "
                            f"{sorted(RESERVED)} or `review`.")
            continue
        where = f"task `{tid}`"
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
            "id": tid, "parent": owner, "title": title, "brief": brief, "verify": verify,
            "blocked_by": blocked_by, "status": "pending",
        }
        touched.append(tid)
    for tid in touched:
        # `x` means the owner's own `<owner>.x`; a name that only exists elsewhere is kept, to be refused below.
        names = [b if full(b) not in tasks and b in tasks else full(b) for b in tasks[tid]["blocked_by"]]
        tasks[tid]["blocked_by"] = list(dict.fromkeys(names))

    live = {tid: t for tid, t in tasks.items() if t["status"] != "cancelled"}
    for tid, t in live.items():
        if t["status"] != "pending" or t["parent"] != owner:
            continue
        for b in t["blocked_by"]:
            if b == tid:
                problems.append(f"task `{tid}` is blocked by itself.")
            elif b not in tasks:
                problems.append(f"task `{tid}` is blocked by `{b}`, which doesn't exist.")
            elif tasks[b]["parent"] != owner:
                problems.append(f"task `{tid}` is blocked by `{b}`, which is not one of your tasks. A task can only "
                                "wait for tasks planned next to it.")
            elif tasks[b]["status"] == "cancelled":
                problems.append(f"task `{tid}` is blocked by `{b}`, which is cancelled. Define `{tid}` again with a "
                                "new `blocked_by`, or cancel it too.")
            elif tasks[b]["status"] == "failed" and tid in touched:
                problems.append(f"task `{tid}` is blocked by `{b}`, which failed and will never unblock it. Point it "
                                "at the task that replaces it.")
    if cycle := _cycle(live):
        problems.append("the tasks block each other in a circle: " + " -> ".join(cycle) + ".")
    if len(live) > cfg.max_tasks:
        problems.append(f"the run would have {len(live)} tasks; the limit is {cfg.max_tasks}.")
    if owner:
        mine = [t for t in live.values() if t["parent"] == owner]
        if len(mine) > cfg.max_subtasks:
            problems.append(f"your task would have {len(mine)} sub-tasks; the limit is {cfg.max_subtasks}. Group "
                            "them, or do some yourself.")
        if len(mine) == 1:
            problems.append("a task can't be split into one piece: that only hands your task to someone else. Split "
                            "it into pieces that can be worked and checked separately, or do it yourself.")

    if problems:
        return False, "Nothing was changed. Fix these and call `plan` again:\n" + "\n".join(f"- {p}" for p in problems)

    now = time.time()
    seq = max((t["seq"] for t in existing.values()), default=0)
    with db.transaction():
        if not owner:
            db.set_meta("goal", goal)
            db.set_meta("done_when", done_when)
        for tid in cancel + cascade:
            db.update_task(tid, status="cancelled")
        for tid in touched:
            t = tasks[tid]
            if tid in existing:
                db.update_task(tid, title=t["title"], brief=t["brief"], verify=t["verify"],
                               blocked_by=json.dumps(t["blocked_by"]))
            else:
                seq += 1
                db.execute(
                    "INSERT INTO tasks(id, parent, seq, title, brief, verify, blocked_by, status, created, updated)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (tid, owner, seq, t["title"], t["brief"], t["verify"], json.dumps(t["blocked_by"]), now, now),
                )
    if collections:
        run.store.merge(collections)
    db.event(owner or "lead", "plan", {"tasks": touched, "cancel": cancel + cascade,
                                       "collections": sorted(collections)})
    log.info("plan%s: %d tasks defined, %d cancelled, %d collections", f" of {owner}" if owner else "",
             len(touched), len(cancel) + len(cascade), len(collections))
    run.wake.set()
    return True, "Plan saved.\n" + overview(run, owner)


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
    """Pending tasks whose blockers have all passed review, oldest first.

    A sub-task also needs its parent to be at work: nothing starts under a task that
    is being reviewed, has failed or was cancelled.
    """
    status = {t["id"]: t["status"] for t in run.db.tasks()}
    return [
        t["id"] for t in run.db.tasks()
        if t["status"] == "pending"
        and all(status.get(b) == "done" for b in json.loads(t["blocked_by"]))
        and (t["parent"] is None or status.get(t["parent"]) == "running")
    ]


def stuck(run: Run, owner: str | None = None) -> list[str]:
    """An owner's pending tasks that can never start: something they depend on failed."""
    status = {t["id"]: t["status"] for t in run.db.tasks()}
    return [
        t["id"] for t in run.db.children(owner)
        if t["status"] == "pending" and any(status.get(b) == "failed" for b in json.loads(t["blocked_by"]))
    ]


def undecided(run: Run, owner: str | None = None) -> list[str]:
    """An owner's tasks that were reviewed as partial and wait for its decision."""
    return [t["id"] for t in run.db.children(owner) if t["status"] == "partial"]


def moving(run: Run, owner: str | None = None) -> list[str]:
    """An owner's open tasks that are being worked on or can still start without it doing anything."""
    bad = stuck(run, owner) + undecided(run, owner)
    return [t["id"] for t in run.db.children(owner) if t["status"] in OPEN and t["id"] not in bad]


def overview(run: Run, owner: str | None = None) -> str:
    """An owner's tasks. The lead sees its own tasks, not the pieces workers split them into."""
    rows = run.db.children(owner)
    if not rows:
        return "You have no sub-tasks." if owner else "The plan has no tasks."
    lines = []
    for t in rows:
        line = f"- `{t['id']}` {t['status']}: {t['title']}"
        blockers = json.loads(t["blocked_by"])
        if blockers and t["status"] == "pending":
            line += f" (blocked by {', '.join(blockers)})"
        lines.append(line)
    if bad := stuck(run, owner):
        lines.append(f"Can never start, because a task they depend on failed: {', '.join(bad)}.")
    if waiting := undecided(run, owner):
        lines.append(f"Waiting for your decision (`decide`): {', '.join(waiting)}. What each is missing was sent "
                     "to you when it was reviewed.")
    return "\n".join(lines)


def what_next(run: Run, owner: str | None = None, settled: str | None = None) -> str:
    """Added to what an owner is told when one of its tasks settles: what can't start, what waits for
    its decision, and what is left to do. `settled` is the task the text is already about."""
    text = ""
    if bad := stuck(run, owner):
        text += f"\n\nCan never start because of a failed task: {', '.join(bad)}."
    waiting = undecided(run, owner)
    if others := [t for t in waiting if t != settled]:
        text += f"\n\nWaiting for your decision (`decide`): {', '.join(others)}."
    if still := moving(run, owner):
        text += f"\n\nStill open: {', '.join(still)}." + (
            "" if waiting else " End your turn unless the plan should change.")
    elif waiting:
        pass                               # the next step is the decision, and the text already says so
    elif owner:
        text += ("\n\nNone of your sub-tasks is running or waiting to start.\n" + overview(run, owner) +
                 "\nPut the results together, do what is left yourself, and call `report`.")
    else:
        text += ("\n\nNo task is running or waiting to start.\n" + overview(run) +
                 "\nIf the goal is met, write the deliverable and call `finish`; otherwise add tasks.")
    return text


def give(run: Run, inp: object) -> tuple[bool, str]:
    """Start a run from a plan the user wrote. It passes the same gates as a plan an agent sets, no
    planner is called, and the lead owns it from the first minute."""
    if not isinstance(inp, dict):
        return False, "The plan must be a JSON object with `goal`, `done_when`, and usually `collections` and `tasks`."
    ok, text = apply(run, inp)
    if ok:
        run.db.set_meta("planner_done", True)
        run.db.set_meta("plan_from", "user")
        run.db.event(None, "plan_given", {"tasks": [t.get("id") for t in inp.get("tasks") or []]})
    return ok, text
