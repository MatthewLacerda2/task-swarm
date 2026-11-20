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


HAND_IN = {      # per role: the tool that hands in, and where what is missing goes
    "worker": "call `report` with what you have, and put the rest under `not_done`",
    "reviewer": "call `verdict` on what you have checked, and say in `findings` what you could not check",
    "planner": "call `plan` with the plan as it is",
    "lead": "end your turn if you are waiting for tasks, or write the deliverable and call `finish`",
}


def calls_notice(role: str, used: int, limit: int, mark: int) -> str:
    """Told to an agent as its calls for the assignment run down. At `limit` it is stopped."""
    left = limit - used
    calls = "1 call" if left == 1 else f"{left} calls"
    what = "the run fails" if role == "lead" else "you are stopped and work that was not handed in is lost"
    if mark >= 95:
        return f"{calls} left of {limit}. Hand in now: {HAND_IN[role]}."
    if mark > 80:
        return f"{used} of your {limit} calls are used, {calls} left. Hand in before they run out."
    return (f"You have used {used} of the {limit} model calls you have for this "
            f"{'turn' if role == 'lead' else 'assignment'}. At {limit}, {what}. Spend the {calls} left on "
            f"the part that is worth most and can still be finished, then {HAND_IN[role]}.")


def _budget(run: Run) -> str:
    cfg = run.cfg
    return f"US$ {run.budget.spent:.2f} of the run's budget of US$ {cfg.run_budget:.2f}"


WORTH_MOST = ("Put what is left where it is worth most: the part of your task that matters most to the goal and "
              "can still be finished, even if a smaller part is closer to done. Drop the rest and list it under "
              "`not_done`.")


def budget_notice_lead(run: Run, mark: int) -> str:
    cfg = run.cfg
    if mark >= 100:
        return (f"The run's budget of US$ {cfg.run_budget:.2f} is used up. No new task will start; tasks "
                "that never started are dropped when you finish. Write the deliverable with what "
                f"exists and call `finish`. The run stops for good at US$ {cfg.hard_stop:.2f}.")
    if mark > 80:
        return f"Run budget: {mark}% used ({_budget(run)})."
    return (f"Run budget: {mark}% used ({_budget(run)}). Put what is left where it is worth most: if not "
            "everything will fit, cancel the tasks that matter least to the goal so that the ones that "
            "matter most can finish. The workers have been told to do the same inside their tasks.")


def budget_notice_worker(run: Run, mark: int) -> str:
    cfg = run.cfg
    if mark >= 100:
        return (f"The run's budget of US$ {cfg.run_budget:.2f} is used up, and the run stops for good at "
                f"US$ {cfg.hard_stop:.2f}. Hand in now: {HAND_IN['worker']}.")
    if mark > 80:
        return f"Run budget: {mark}% used ({_budget(run)})."
    return f"Run budget: {mark}% used ({_budget(run)}). {WORTH_MOST}"


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
            "# The plan\n\n" + ("The user gave this plan with the task" if run.db.get_meta("plan_from") == "user"
                                  else "The planner drafted this plan")
            + " and its first tasks have started. It is yours now.",
            f"Goal: {run.db.get_meta('goal')}",
            f"Done when: {run.db.get_meta('done_when')}",
            *tasks,
            _store(run),
            "End your turn to wait for results, unless you see something in the plan to fix first.",
        ]
    return "\n\n".join(parts)


def _store(run: Run) -> str:
    return f"# The store\n\n{run.store.describe()}"


def _gaps(task) -> str:
    """For a task its owner accepted with something missing: what, as its reviewer confirmed."""
    if task["status"] != "done" or not task["gaps"]:
        return ""
    return ("\n\nIts owner accepted it with this missing, as its reviewer confirmed. Work with what exists and "
            f"don't assume the rest:\n{task['gaps']}")


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
            lines.append(f"## `{b['id']}`: {b['title']}\n\n{b['summary']}{_gaps(b)}\n\n"
                         f"Its files are in /task/work/{b['id']}/.")
        parts.append("\n\n".join(lines))
    parts.append(_store(run))
    if run.budget.percent >= config.NOTICES[0] and not run.budget.winding_down:
        parts.append(f"The run has used {_budget(run)}. {WORTH_MOST}")
    return "\n\n".join(parts)


def _handed_in(task) -> list[str]:
    """What a reviewer is shown of a task's latest report, and what its owner ruled since the brief."""
    report = task["summary"] or "(empty)"
    if task["how"]:
        report += f"\n\nHow it was done, according to the worker:\n{task['how']}"
    return [
        f"# The worker's report\n\n{report}",
        *(["# Decisions by the task's owner since the brief\n\n" +
           "\n".join(f"- {d}" for d in json.loads(task["decisions"]))] if task["decisions"] else []),
        *([f"# What an earlier review found missing\n\n{task['gaps']}\n\nThe owner then sent the task back with "
           "the latest decision above."] if task["gaps"] else []),
    ]


def _review_count(run: Run, task) -> str:
    return (f"This is review {task['attempts']} of this task. Its work was sent back {task['fails']} times so far; "
            f"at {run.cfg.max_attempts} the task fails.")


def review_again(run: Run, task) -> str:
    """To a task's reviewer, when the worker hands in again after its review."""
    return "\n\n".join([
        f"The worker of `{task['id']}` handed in again. {_review_count(run, task)}",
        *_handed_in(task),
        "Check that each problem in your findings is fixed, and that the fixes are right: re-read what changed and "
        "re-run what it rests on. Where the worker says a finding is wrong or could not be met, check its reason. "
        "What you already checked and found right stands unless the worker changed it, so don't start the review "
        "over. A new problem counts only if the work doesn't meet `verify` because of it; anything smaller goes in "
        "`findings` as a remark, without failing the work. Then give your `verdict` again; in `findings`, say for "
        "each earlier problem whether it is fixed.",
    ])


def reviewer_first(run: Run, reviewer: str, task, previous: str | None = None) -> str:
    parts = [
        identity(reviewer, "reviewer"),
        f"# The goal of the whole team\n\n{run.db.get_meta('goal')}",
        f"# The task under review: `{task['id']}`, {task['title']}\n\n{task['brief']}",
        f"# What to check\n\n{task['verify']}",
        *_handed_in(task),
        *([f"# What the previous reviewer found\n\nIt could not continue, so the review is yours. The worker has "
           f"reworked the task since.\n\n{previous}"] if previous else []),
        f"The worker's files are in /task/work/{task['id']}/. The records it stored have "
        f"\"_task\": \"{task['id']}\". {_review_count(run, task)}",
        _store(run),
    ]
    pieces = run.db.children(task["id"])
    if pieces:
        lines = [f"- `{t['id']}` ({t['status']}): {t['title']}" +
                 (" (accepted by the worker with something missing: " + " ".join(t["gaps"].split())[:300] + ")"
                  if t["status"] == "done" and t["gaps"] else "") for t in pieces]
        parts.insert(-2, "# Sub-tasks\n\nThe worker split this task. Each piece that is done was reviewed by itself; "
                        "you check the whole, including what the pieces don't cover and how they were put "
                        "together. Their files are in /task/work/<id>/.\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _final_tasks(run: Run) -> str:
    tasks = []
    for t in run.db.children(None):
        line = f"- `{t['id']}` ({t['status']}): {t['title']}"
        if t["status"] == "done" and t["summary"]:
            line += f"\n  Report: {t['summary'][:600]}"
            if t["gaps"]:
                line += f"\n  Accepted by the lead with this missing: {t['gaps'][:600]}"
        elif t["status"] == "failed":
            line += f"\n  Failed: {(t['findings'] or '')[:400]}"
        tasks.append(line)
    return "# The tasks\n\n" + ("\n".join(tasks) or "(none)")


def final_first(run: Run, reviewer: str, deliverable: str, summary: str, previous: str | None = None) -> str:
    parts = [
        identity(reviewer, "reviewer"),
        "You review the finished work of the whole team.",
        f"# The task the user gave\n\n{run.db.get_meta('task')}",
        f"# The goal the lead set\n\n{run.db.get_meta('goal')}",
        f"# What to check\n\n{run.db.get_meta('done_when')}",
        f"# The deliverable\n\n/task/deliverable/{deliverable}\n\nThe lead's summary:\n{summary}",
        _final_tasks(run),
        _store(run) + f"\n\nSignals logged (conflicting records): {run.store.signals}. Corrections logged "
                      f"(records updated or deleted after they were stored): {run.store.changes}, in changes.jsonl.",
        "Check the deliverable against the store and the sources, not only against itself: a claim in it should "
        "trace to a record or a file. Check too that it is honest about what was not found.",
        "Your verdict: `fail` when something the deliverable says is wrong, unsupported or contradicts itself, or "
        "when it hides something missing; the lead can fix that. `partial` when everything it says holds and it "
        "states plainly what is missing, but part of what was asked for could not be delivered: the user then "
        "decides what the result is worth. Something missing that the lead accepted on a task (above) is a reason "
        "for `partial`, never for `fail`, as long as the deliverable says it.",
    ]
    if previous:
        parts.insert(-2, "# What the previous final reviewer found\n\nIt could not continue, so the review is yours. "
                         f"The lead has reworked the deliverable since.\n\n{previous}")
    return "\n\n".join(parts)


def final_again(run: Run, n: int, deliverable: str, summary: str) -> str:
    """To the final reviewer, when the lead hands in again after its findings."""
    return "\n\n".join([
        f"The lead handed in the deliverable again: /task/deliverable/{deliverable} (review {n} of at most "
        f"{run.cfg.max_final_reviews}).\n\nThe lead's summary:\n{summary}",
        _final_tasks(run),
        "Check that each problem in your findings is fixed, and that the fixes are right: re-read what changed and "
        "re-run what it rests on. What you already checked and found right stands unless the lead changed it, so "
        "don't start the review over. A new problem counts only if it makes something the deliverable says wrong. "
        "Then give your `verdict` again; in `findings`, say for each earlier problem whether it is fixed.",
    ])
