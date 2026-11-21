"""Runs the plan: starts each task when its blockers have passed review, puts every
report in front of a reviewer, sends wrong work back to its worker and incomplete work
to its owner, and wakes whoever owns a task (the lead, or the worker that split its
task) when something it has to know happens.

Nothing here decides what the work is; that is the plan. This is the part of a
company that doesn't need judgment: the schedule, the hand-offs and the rule that
nothing counts as done until someone else has checked it.

Every state change is written to the database before the next step, so a run that
is killed (a crash, a power cut) continues from where it was: `resume`.
"""

from __future__ import annotations

import asyncio
import logging

from . import config, plan, prompts, readme
from .agent import run_agent
from .db import OPEN
from .handlers import Outcome
from .run import Run

log = logging.getLogger("taskswarm")

FINISHED = ("done", "done_with_gaps", "done_unverified")
STOP_REASONS = {
    "agent_budget": "its agent ran out of budget",
    "task_budget": "the task and its sub-team used up the task's budget",
    "context": "its agent's memory overflowed",
    "api_error": "the model API kept failing",
    "calls": "its agent used all its calls without handing in",
    "silent": "its agent stopped without handing in",
    "refusal": "the model refused to continue",
    "sandbox": "its terminal could not be started",
    "team_stalled": "its sub-tasks stopped moving and its worker did not act",
}
MAX_STALLS = 3
TEAM_STALLS = 2


class Orchestrator:
    def __init__(self, run: Run):
        self.run = run
        self.db = run.db
        self.jobs: dict[str, asyncio.Task] = {}
        self.waiting: set[str] = set()      # tasks whose worker is idle while its sub-team works
        self.stalls = 0

    # --- the loop ----------------------------------------------------------------------

    async def run_until_done(self) -> str:
        db, run = self.db, self.run
        if db.get_meta("status") in FINISHED:
            return db.get_meta("status")
        db.set_meta("status", "running")
        if db.agent("lead") is not None:
            run.send("lead", "system", "The run was interrupted and has resumed.\n" + plan.overview(run))
        elif self._use_planner():
            self._start("planner", self._planner_job())
        else:
            self._start_lead(planned=bool(db.get_meta("goal")))      # a plan the user gave is already set
        try:
            while True:
                run.wake.clear()
                self._reap()
                status = db.get_meta("status")
                if status in FINISHED or status == "failed":
                    break
                if run.budget.hard_stopped:
                    for name in self.waiting & set(self.jobs):      # idle workers; `resume` wakes them
                        self.jobs[name].cancel()
                    if not self.jobs:
                        db.set_meta("status", "paused")
                        break
                else:
                    self._notices()
                    self._start_tasks()
                    self._start_final_review()
                    self._deliver_mail()
                    if not self.jobs and not self._nudge_lead():
                        db.set_meta("status", "stalled")
                        break
                waiter = asyncio.create_task(run.wake.wait())
                await asyncio.wait([waiter, *self.jobs.values()], return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
        finally:
            for job in self.jobs.values():
                job.cancel()
            await asyncio.gather(*self.jobs.values(), return_exceptions=True)
        status = db.get_meta("status")
        db.event(None, "stopped", {"status": status, "spent": run.budget.spent})
        log.info("stopped: %s (US$ %.3f spent)", status, run.budget.spent)
        return status

    def _start(self, name: str, coro) -> None:
        self.jobs[name] = asyncio.create_task(coro, name=name)

    def _reap(self) -> None:
        for name, job in list(self.jobs.items()):
            task = self.db.task(name)
            if task is not None and task["status"] == "cancelled" and not job.done():
                job.cancel()            # cancelled while it was being worked on
                continue
            if not job.done():
                continue
            del self.jobs[name]
            self.waiting.discard(name)
            if job.cancelled():
                continue
            if exc := job.exception():
                log.error("job %s crashed", name, exc_info=exc)
                self.db.event(name, "crash", repr(exc)[:2000])
                if task is not None and task["status"] in ("running", "reviewing"):
                    self._fail_task(name, "an internal error stopped it")
                elif name == "lead":
                    self.db.set_meta("status", "failed")
                    self.db.set_meta("status_reason", f"the lead's turn crashed: {exc!r}"[:500])
                elif name == "planner":
                    self._start_lead(planned=False)

    # --- the plan's first draft --------------------------------------------------------------

    def _use_planner(self) -> bool:
        """The planner runs when one is set and one of its calls, at worst, fits the budget."""
        cfg = self.run.cfg
        if self.db.get_meta("planner_done") or not cfg.plan_model or cfg.plan_model == cfg.lead_model:
            return False                  # no planner, or the lead is the same model and plans itself
        worst = config.worst_call(cfg.plan_model, cfg.max_tokens)
        if not cfg.planner_fits():
            log.info("no planner: one %s call can cost up to US$ %.2f, too much for this budget; the lead plans",
                     cfg.plan_model, worst)
            self.db.event(None, "planner_skipped", {"worst_call": worst})
            self.db.set_meta("planner_done", True)
            return False
        return True

    async def _planner_job(self) -> None:
        """A stronger model drafts the plan; the lead owns it from then on."""
        run, db = self.run, self.db
        text = ""
        if db.agent("planner") is None:
            db.add_agent("planner", "planner")
            text = prompts.planner_first(db.get_meta("task"))
        out = await run_agent(run, "planner", text)
        if out.kind == "failed" and out.reason == "hard_stop":
            return
        planned = out.kind == "planned"
        if not planned:
            log.info("the planner did not produce a plan (%s); the lead plans", out.reason or out.kind)
        db.event("planner", "planner_done", {"planned": planned, "reason": out.reason})
        self._start_lead(planned)

    def _start_lead(self, planned: bool) -> None:
        self.db.set_meta("planner_done", True)
        self.db.add_agent("lead", "lead")
        self._start("lead", self._lead_job(prompts.lead_first(self.run, planned)))
        self.run.wake.set()

    # --- what can start ----------------------------------------------------------------

    def _notices(self) -> None:
        run = self.run
        if self.db.agent("lead") is None:
            return
        mark = run.budget.crossed()
        if mark is None:
            return
        # The lead and the workers at work are told; reviewers are not, a review is not cut short.
        run.send("lead", "system", prompts.budget_notice_lead(run, mark), quiet=mark < 100)
        for name in run.active:
            row, task = self.db.agent(name), self.db.task(name)
            if row["role"] == "worker" and task is not None and task["status"] == "running":
                run.send(name, "system", prompts.budget_notice_worker(run, mark), quiet=True)
        if mark >= 100:
            self.db.event(None, "wind_down", {"spent": run.budget.spent})
            self._wind_down_teams()

    def _wind_down_teams(self) -> None:
        """The budget is used up: sub-tasks that never started are dropped and their owners told."""
        dropped: dict[str, list[str]] = {}
        for t in self.db.tasks():
            if t["parent"] and t["status"] == "pending":
                self.db.update_task(t["id"], status="cancelled")
                dropped.setdefault(t["parent"], []).append(t["id"])
        for owner, ids in dropped.items():
            self.run.send(owner, "system",
                          f"The run's budget is used up, so no new task starts. Your sub-tasks that had not started "
                          f"were dropped: {', '.join(ids)}. Put together what exists and call `report`, saying what "
                          "is missing.")

    def _start_tasks(self) -> None:
        run = self.run
        # A worker waiting for its sub-team holds no slot: its sub-tasks need them.
        working = [n for n in self.jobs if self.db.task(n) is not None and n not in self.waiting]
        # Tasks a previous process left in the middle go first.
        for t in self.db.tasks():
            if t["status"] in ("running", "reviewing") and t["id"] not in self.jobs:
                self._start(t["id"], self._task_job(t["id"]))
                working.append(t["id"])
        if run.budget.winding_down:
            return
        for tid in plan.ready(run):
            if len(working) >= run.cfg.max_parallel:
                break
            self.db.update_task(tid, status="running")
            self.db.event(tid, "task_started")
            log.info("task %s started: %s", tid, self.db.task(tid)["title"])
            self._start(tid, self._task_job(tid))
            working.append(tid)
            self.stalls = 0

    def _start_final_review(self) -> None:
        if self.db.get_meta("final_pending") and "final" not in self.jobs:
            self._start("final", self._final_job())

    def _deliver_mail(self) -> None:
        """Wake agents that have mail and aren't working: the lead, or one that already handed in."""
        for agent in self.db.waiting_mail():
            if agent in self.run.active or agent in self.jobs or f"answer:{agent}" in self.jobs:
                continue
            row = self.db.agent(agent)
            if row is None:
                continue
            if agent == "lead":
                if "final" not in self.jobs:      # the lead waits for the final review's result
                    self._start("lead", self._lead_job())
            elif (row["task"] or "final") not in self.jobs:    # that job, while it runs, delivers it
                self._start(f"answer:{agent}", run_agent(self.run, agent, closed=True))

    def _nudge_lead(self) -> bool:
        """Nothing is running and the lead is idle: tell it where things stand. False = give up."""
        if self.db.get_meta("final_pending") or self.db.agent("lead") is None:
            return True
        self.stalls += 1
        if self.stalls > MAX_STALLS:
            self.db.set_meta("status_reason", "the lead stopped acting while the work was unfinished")
            return False
        self.run.send("lead", "system",
                      "Nothing is running and you have not finished.\n" + plan.overview(self.run) +
                      "\nAdd or fix tasks with `plan`, or write the deliverable and call `finish`.")
        self._deliver_mail()
        return True

    # --- the lead ----------------------------------------------------------------------

    async def _lead_job(self, text: str = "") -> None:
        out = await run_agent(self.run, "lead", text)
        if out.kind == "finish":
            n = self.db.get_meta("final_reviews", 0) + 1
            self.db.set_meta("final_reviews", n)
            self.db.set_meta("deliverable", out.data)
            self.db.set_meta("final_pending", True)
            self.db.event("lead", "finish", out.data)
            log.info("the lead handed in %s", out.data["file"])
            self.stalls = 0
        elif out.kind == "failed" and out.reason != "hard_stop":
            self.db.set_meta("status", "failed")
            self.db.set_meta("status_reason", f"the lead can't continue: {STOP_REASONS.get(out.reason, out.reason)}")
        self.run.wake.set()

    # --- one task: work, review, rework ---------------------------------------------------

    async def _task_job(self, tid: str) -> None:
        run, db, cfg = self.run, self.db, self.run.cfg
        text = ""
        if db.agent(tid) is None:
            db.add_agent(tid, "worker", tid)
            text = prompts.worker_first(run, db.task(tid))
        while True:
            task = db.task(tid)
            if task["status"] == "running":
                out = await run_agent(run, tid, text)
                stalls = 0
                while out.kind == "idle":          # it split its task and waits for the pieces
                    stalls, out = await self._wait_for_team(tid, stalls)
                if out.kind != "report":
                    return self._stopped(tid, out)
                db.update_task(tid, status="reviewing", attempts=task["attempts"] + 1,
                               summary=out.data["summary"], how=out.data["how"])
                db.event(tid, "report", {"attempt": task["attempts"] + 1})
                log.info("task %s handed in (report %d)", tid, task["attempts"] + 1)
                task = db.task(tid)

            reviewer, out = await self._review(tid, task)
            if out.kind != "verdict":
                return self._stopped(tid, out, reviewing=True)
            result, findings = out.data["result"], out.data["findings"]
            db.event(reviewer, "verdict", {"result": result})
            if result == "pass" and task["checkpoint"]:
                # The work is right. Whether the plan can go on from it is the owner's call, whatever the answer.
                db.update_task(tid, status="held", findings=findings, gaps=None, holds=out.data["holds"])
                log.info("checkpoint %s passed review, answer %s: its owner rules", tid, out.data["holds"])
                self._tell_owner(tid, prompts.checkpoint_result(run, db.task(tid)))
                return
            if result == "pass":
                db.update_task(tid, status="done", findings=findings, gaps=None)
                log.info("task %s passed review", tid)
                self._tell_owner(tid, f"Task `{tid}` ({task['title']}) passed review.\n\nIts report:\n"
                                      f"{task['summary']}\n\nIts files are in /task/work/{tid}/.")
                return
            if result == "partial":
                # Right as far as it goes, and its worker can't get the rest: that is the owner's call.
                db.update_task(tid, status="partial", findings=findings, gaps=out.data["missing"],
                               holds=out.data["holds"])
                log.info("task %s is partial after review %d: its owner decides", tid, task["attempts"])
                self._tell_owner(
                    tid,
                    (prompts.checkpoint_result(run, db.task(tid)) + "\n\nIt is also incomplete. "
                     if task["checkpoint"] else "") +
                    f"Task `{tid}` ({task['title']}) was reviewed. What it delivered is right, but something that "
                    "was asked for is missing, and the reviewer judges that its worker would not get it by trying "
                    f"again.\n\nMissing:\n{out.data['missing']}\n\nIts report:\n{task['summary']}\n\nIts files are "
                    f"in /task/work/{tid}/.\n\nThis is yours to decide, with `decide`: accept the task as "
                    "delivered (it counts as done and what is missing stays recorded on it), or send it back with "
                    "what to do instead (a smaller target, another source, a different method). You can also "
                    "cancel it with `plan` and define a replacement. Tasks blocked by it wait for you.")
                return
            fails = task["fails"] + 1
            log.info("task %s did not pass review %d", tid, task["attempts"])
            db.update_task(tid, fails=fails)
            if fails >= cfg.max_attempts:
                return self._fail_task(tid, f"its work was sent back {fails} times and still did not pass. "
                                            f"The last reviewer's findings:\n{findings}")
            db.update_task(tid, status="running", findings=findings)
            text = (f"The reviewer did not pass your work (failed review {fails} of at most {cfg.max_attempts})."
                    f" Its findings:\n\n{findings}\n\nFix them and call `report` again; the same reviewer then "
                    "checks that each finding is fixed. If a finding is wrong, say why in the report. If a finding "
                    "asks for something you tried and could not get, don't try the same way again: put it in "
                    "`not_done` with what you tried, and the task's owner decides.")

    async def _review(self, tid: str, task) -> tuple[str, Outcome]:
        """Review of the task's latest report. The first is a full review. After that the same reviewer
        checks that its own findings were fixed: a new reviewer starting over finds a different list of
        small problems each time, and work whose substance is right runs out of attempts."""
        fresh = f"{tid}.review{task['attempts']}"
        reviewer = task["reviewer"] or fresh
        out = await self._review_round(reviewer, task)
        if out.kind == "failed" and out.reason != "hard_stop" and reviewer != fresh:
            # It can't go on (its budget, its memory): a new reviewer starts from its findings.
            log.info("reviewer %s can't continue (%s); %s takes over", reviewer, out.reason, fresh)
            reviewer = fresh
            out = await self._review_round(reviewer, task, previous=task["findings"])
        return reviewer, out

    async def _review_round(self, reviewer: str, task, previous: str | None = None) -> Outcome:
        run, db, tid = self.run, self.db, task["id"]
        text = ""
        if db.agent(reviewer) is None:
            db.add_agent(reviewer, "reviewer", tid)
            text = prompts.reviewer_first(run, reviewer, task, previous)
            db.update_task(tid, reviewer=reviewer, asked=task["attempts"])
        elif task["asked"] < task["attempts"]:
            # In its inbox, so that a run killed right here still delivers it.
            run.send(reviewer, "system", prompts.review_again(run, task))
            db.update_task(tid, asked=task["attempts"])
        return await run_agent(run, reviewer, text)

    async def _wait_for_team(self, tid: str, stalls: int) -> tuple[int, Outcome]:
        """The worker of `tid` ended its turn with sub-tasks open: wake it when it has news."""
        run = self.run
        event = run.mail.setdefault(tid, asyncio.Event())
        while True:
            event.clear()
            if tid in self.db.waiting_mail():
                break
            if not plan.moving(run, tid):
                # Nothing under it is being worked on or can start: it has to act.
                if stalls >= TEAM_STALLS:
                    return stalls, Outcome("failed", reason="team_stalled")
                stalls += 1
                run.send(tid, "system",
                         "None of your sub-tasks is running or can start.\n" + plan.overview(run, tid) +
                         "\nDo what is left yourself, replace or cancel them with `plan`, or call `report`.")
                break
            self.waiting.add(tid)
            run.wake.set()                  # its slot is free for its sub-tasks
            await event.wait()
        self.waiting.discard(tid)
        return stalls, await run_agent(run, tid)

    def _stopped(self, tid: str, out: Outcome, reviewing: bool = False) -> None:
        """A worker or reviewer ended without handing in."""
        if out.reason == "hard_stop":
            return                       # the task keeps its state; `resume` continues it
        why = STOP_REASONS.get(out.reason, out.reason or out.kind)
        self._fail_task(tid, ("its review could not be completed: " if reviewing else "") + why)

    def _fail_task(self, tid: str, why: str) -> None:
        self.db.update_task(tid, status="failed", findings=why)
        self.db.event(tid, "task_failed", why[:500])
        log.info("task %s failed: %s", tid, why.splitlines()[0])
        for t in self.db.descendants(tid):          # its sub-team stops with it
            if t["status"] in OPEN:
                self.db.update_task(t["id"], status="cancelled")
        task = self.db.task(tid)
        self._tell_owner(tid, f"Task `{tid}` ({task['title']}) failed: {why}\n\nWhat it left is in "
                              f"/task/work/{tid}/. Decide what to do: define a replacement under a new id (and "
                              "point the tasks blocked by it at the replacement), or go on without it and say so "
                              "in what you hand in.")

    def _tell_owner(self, tid: str, text: str) -> None:
        """Tell whoever planned this task (the lead, or the worker that split its own) how it ended."""
        run = self.run
        owner = self.db.task(tid)["parent"]
        text += plan.what_next(run, owner, settled=tid)
        run.send(owner or "lead", "system", text)

    # --- the end ---------------------------------------------------------------------

    async def _final_job(self) -> None:
        """Review `n` of the deliverable, by the same rule as a task's review (`_review`): the first is a
        full review, and after that the same reviewer checks that its own findings were fixed."""
        run, db, cfg = self.run, self.db, self.run.cfg
        n = db.get_meta("final_reviews", 1)
        fresh = f"final.review{n}"
        reviewer = db.get_meta("final_reviewer")
        if reviewer is None:
            reviewer = fresh
            db.set_meta("final_reviewer", reviewer)
        out = await self._final_round(reviewer, n)
        if out.kind == "failed" and out.reason != "hard_stop" and reviewer != fresh:
            # It can't go on (its budget, its memory): a new reviewer starts from its findings.
            log.info("final reviewer %s can't continue (%s); %s takes over", reviewer, out.reason, fresh)
            reviewer = fresh
            db.set_meta("final_reviewer", reviewer)
            out = await self._final_round(reviewer, n)
        if out.kind == "failed" and out.reason == "hard_stop":
            return
        result = out.data["result"] if out.kind == "verdict" else "fail"
        findings = out.data["findings"] if out.kind == "verdict" else \
            f"The final review could not be completed: {STOP_REASONS.get(out.reason, out.reason)}."
        if result == "partial":
            findings += f"\n\nAsked for and not delivered:\n{out.data['missing']}"
        db.set_meta("final_findings", findings)
        db.set_meta("final_pending", False)
        db.event(reviewer, "final_verdict", {"result": result})
        if result == "pass":
            db.set_meta("status", "done")
        elif result == "partial":
            # What the deliverable says holds, and it can't be made complete: the user decides what that is worth.
            db.set_meta("status", "done_with_gaps")
        elif n >= cfg.max_final_reviews or run.budget.winding_down:
            db.set_meta("status", "done_unverified")
        else:
            run.send("lead", "system",
                     f"The final reviewer did not pass the deliverable (review {n} of at most "
                     f"{cfg.max_final_reviews}). Its findings:\n\n{findings}\n\nFix the deliverable, with new tasks "
                     "if it needs them, and call `finish` again. The same reviewer then checks that each of these "
                     "findings is fixed.")
        log.info("final review %d: %s", n, result)
        run.wake.set()

    async def _final_round(self, reviewer: str, n: int) -> Outcome:
        run, db = self.run, self.db
        deliverable = db.get_meta("deliverable")
        text = ""
        if db.agent(reviewer) is None:
            db.add_agent(reviewer, "reviewer")
            text = prompts.final_first(run, reviewer, deliverable["file"], deliverable["summary"],
                                       previous=db.get_meta("final_findings") if n > 1 else None)
            db.set_meta("final_asked", n)
        elif db.get_meta("final_asked", 1) < n:
            # In its inbox, so that a run killed right here still delivers it.
            run.send(reviewer, "system", prompts.final_again(run, n, deliverable["file"], deliverable["summary"]))
            db.set_meta("final_asked", n)
        return await run_agent(run, reviewer, text)


async def execute(run: Run) -> str:
    """Run until the work is done or can't go on. The run folder's README is current when it returns."""
    readme.write(run)
    try:
        return await Orchestrator(run).run_until_done()
    finally:
        readme.write(run)


def summary(run: Run) -> dict:
    """What a caller needs after a run."""
    db = run.db
    deliverable = db.get_meta("deliverable")
    return {
        "status": db.get_meta("status"),
        "reason": db.get_meta("status_reason"),
        "spent": round(run.budget.spent, 4),
        "deliverable": str(run.cfg.run_dir / "deliverable" / deliverable["file"]) if deliverable else None,
        "summary": deliverable["summary"] if deliverable else None,
        "final_findings": db.get_meta("final_findings"),
        "tasks": {t["id"]: t["status"] for t in db.tasks()},
        "store": dict(run.store.counts),
        "signals": run.store.signals,
    }
