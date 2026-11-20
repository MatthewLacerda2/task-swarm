"""One agent working on one assignment: model call -> tools -> model call, until it
hands in (report, verdict, finish), ends its turn, or can't go on.

The history is append-only. The only time it is shortened is an on-demand
compaction, after which it restarts at the returned compaction block.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from . import channels, config, handlers, llm, plan, prompts, sandbox
from .db import OPEN
from .handlers import Outcome
from .run import Run, work_dir

RESUMED = "[The run was interrupted and has resumed. Carry on from where you were.]"
# `sleep 120`, `time.sleep(120)`: a wait, as opposed to a short pause between requests.
WAIT_RE = re.compile(r"\bsleep\b[\s(]*(\d+(?:\.\d+)?)")
WAIT_SECONDS = 15


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool_result(tool_use_id: str, content: str, is_error: bool) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def _mail(run: Run, agent: str) -> list[str]:
    out = []
    for m in run.db.take_inbox(agent):
        head = "[Update]" if m["from_agent"] == "system" else f"[Message from `{m['from_agent']}`]"
        out.append(f"{head}\n{m['body']}")
    return out


def _news(run: Run, agent: str) -> list[str]:
    """Channel posts the agent hasn't seen. They ride along with something else it is given
    (a command's result, the text that woke it), so a post never costs it a call."""
    text = channels.unread(run, agent)
    return [text] if text else []


class _Box:
    """The agent's sandbox, started on the first command."""

    def __init__(self, run: Run, agent: str):
        self.run, self.agent = run, agent
        self.box: sandbox.Sandbox | None = None

    async def get(self) -> sandbox.Sandbox:
        if self.box is None or not self.box.alive:
            await self.close()
            work_dir(self.run, self.agent)
            (self.run.cfg.run_dir / "outputs" / self.agent).mkdir(parents=True, exist_ok=True)
            self.box = sandbox.Sandbox(sandbox.agent_spec(self.run.cfg, self.agent))
            await self.box.start()
        return self.box

    async def close(self) -> None:
        if self.box is not None:
            await self.box.close()
            self.box = None


async def run_agent(run: Run, agent: str, text: str = "", closed: bool = False) -> Outcome:
    """Give the agent `text` plus its waiting messages and let it work.

    `closed` means its assignment is already handed in and it was woken to answer a
    message: it may not hand in again.
    """
    lock = run.locks.setdefault(agent, asyncio.Lock())
    async with lock:
        run.active.add(agent)
        box = _Box(run, agent)
        try:
            return await _loop(run, agent, text, closed, box)
        except sandbox.SandboxError as e:
            run.db.event(agent, "sandbox_error", str(e)[:2000])
            return Outcome("failed", reason="sandbox")
        finally:
            run.active.discard(agent)
            await box.close()


async def _loop(run: Run, agent: str, text: str, closed: bool, box: _Box) -> Outcome:
    db, cfg = run.db, run.cfg
    row = db.agent(agent)
    role = row["role"]
    messages = db.history(agent)
    est = row["context_est"]

    parts = [p for p in [text, *_mail(run, agent)] if p]
    if not parts:
        parts = [RESUMED]
    parts += _news(run, agent)
    content: list[dict] = []
    if messages and messages[-1]["role"] == "assistant":
        # A tool call left without a result (a crash or a stop mid-call) must be answered first.
        for b in messages[-1]["content"]:
            if b.get("type") == "tool_use":
                content.append(_tool_result(b["id"], "Not run: the run was interrupted.", True))
    content += [_text_block(p) for p in parts]
    est += sum(llm.estimate_tokens(p) for p in parts)

    if est >= cfg.compaction_trigger and messages:
        if content[0].get("type") == "tool_result":      # close the open tool calls before summarizing
            closing = {"role": "user", "content": [b for b in content if b.get("type") == "tool_result"]}
            messages.append(closing)
            db.add_message(agent, "user", closing["content"])
            content = [b for b in content if b.get("type") != "tool_result"]
        messages, est = await _compact(run, agent, messages, est)
        if messages is None:
            return Outcome("failed", reason="context")
        content.insert(0, _text_block(prompts.compacted_note(agent, role)))
    user = {"role": "user", "content": content}
    messages.append(user)
    db.add_message(agent, "user", content)

    nudges = cut_off = calls = 0
    limit = cfg.plan_max_calls if role == "planner" else cfg.max_calls      # for this assignment; a hard stop
    seq = db.one("SELECT COUNT(*) AS n FROM commands WHERE agent = ?", (agent,))["n"]
    while True:
        if est >= cfg.compaction_trigger:
            messages, est = await _compact(run, agent, messages, est)
            if messages is None:
                return Outcome("failed", reason="context")
            note = {"role": "user", "content": [_text_block(prompts.compacted_note(agent, role))]}
            messages.append(note)
            db.add_message(agent, "user", note["content"])
        if est > cfg.refuse_above:
            db.event(agent, "stopped", {"reason": "context", "estimate": est})
            return Outcome("failed", reason="context")
        if calls >= limit:
            db.event(agent, "stopped", {"reason": "calls"})
            return Outcome("failed", reason="calls")

        resp, why = await _call(run, agent, messages, est)
        if resp is None:
            return Outcome("failed", reason=why)
        calls += 1
        mark = config.notice_mark(100 * (calls - 1) / limit, 100 * calls / limit)
        running_low = [prompts.calls_notice(role, calls, limit, mark)] if mark and calls < limit and not closed else []
        messages.append({"role": "assistant", "content": resp.content})
        db.add_message(agent, "assistant", resp.content)
        est = resp.usage.prompt_tokens + resp.usage.output_tokens
        _save_est(run, agent, est)

        if resp.stop_reason == "refusal":
            db.event(agent, "refusal", {"content": resp.content})
            return Outcome("failed", reason="refusal")

        tool_uses = [b for b in resp.content if b.get("type") == "tool_use"]
        if not tool_uses:
            note = None
            if resp.stop_reason == "max_tokens" and cut_off < 2:
                # All thinking, no action: say so instead of ending the turn silently.
                cut_off += 1
                note = (f"Your reply hit the {cfg.max_tokens:,}-token limit before you did anything. "
                        "Think in smaller steps and act sooner.")
            # Ending a turn is how the lead waits, and how a worker waits for its sub-team.
            may_wait = role == "lead" or closed or (
                role == "worker" and any(t["status"] in OPEN for t in db.children(agent)))
            if note is None and not may_wait and nudges < 2:
                nudges += 1
                tool = {"worker": "report", "reviewer": "verdict", "planner": "plan"}[role]
                note = (f"You ended your turn without handing in. Carry on, and call `{tool}` when you are done; "
                        "if you can't finish, hand in anyway and say why.")
            mail = _mail(run, agent)
            if note is None and not mail:
                if may_wait:
                    return Outcome("idle")
                db.event(agent, "stopped", {"reason": "silent"})
                return Outcome("failed", reason="silent")
            blocks = [_text_block(t) for t in ([note] if note else []) + running_low + mail]
            messages.append({"role": "user", "content": blocks})
            db.add_message(agent, "user", blocks)
            est += sum(llm.estimate_tokens(b["text"]) for b in blocks)
            continue

        per_output = max(1000, cfg.output_chars // len(tool_uses))
        results: list[dict] = []
        outcome: Outcome | None = None
        for tu in tool_uses:
            if resp.stop_reason == "max_tokens":
                out, err = (f"Not run: your reply hit the {cfg.max_tokens:,}-token limit and was cut off. "
                            "Write long files in several smaller commands."), True
            elif outcome is not None:
                out, err = "Not run: you had already handed in.", True
            elif tu["name"] == "bash":
                seq += 1
                out, err = await _execute(run, agent, seq, box, tu, per_output)
            else:
                out, err, outcome = await handlers.handle(run, agent, closed, tu["name"], tu.get("input"))
            results.append(_tool_result(tu["id"], out, err))
        blocks = results + ([] if outcome else
                            [_text_block(t) for t in running_low + _mail(run, agent) + _news(run, agent)])
        messages.append({"role": "user", "content": blocks})
        db.add_message(agent, "user", blocks)
        est += sum(llm.estimate_tokens(b.get("content") or b.get("text") or "") for b in blocks)
        _save_est(run, agent, est)
        if outcome is not None:
            return outcome


def _waiting_command(run: Run, agent: str, cmd: str) -> str | None:
    """Refuse a command that sleeps while the agent has tasks open under it.

    Whoever owns open tasks waits by ending its turn: it is woken the moment one
    settles. Inside a sleeping command it hears nothing until the command returns.
    """
    waits = [float(n) for n in WAIT_RE.findall(cmd)]
    if not waits or max(waits) < WAIT_SECONDS:
        return None
    open_tasks = plan.moving(run, None if agent == "lead" else agent)
    if not open_tasks:
        return None
    return (f"Not run: this command sleeps {max(waits):g} seconds while you have tasks open ({', '.join(open_tasks)}). "
            "End your turn instead: you are woken the moment one of them settles, and a sleeping command can't be.")


def _save_est(run: Run, agent: str, est: int) -> None:
    run.db.execute("UPDATE agents SET context_est = ? WHERE id = ?", (est, agent))


async def _call(run: Run, agent: str, messages: list[dict], est: int) -> tuple[llm.LLMResponse | None, str]:
    """One sampling call under the budget and the concurrency limit: (response, "") or (None, why not)."""
    model, effort, long_cache = run.call_settings(agent)
    res = run.budget.reserve(agent, llm.worst_case_cost(config.prices(model), est + 2000, run.cfg.max_tokens))
    if isinstance(res, str):
        run.db.event(agent, "stopped", {"reason": res})
        run.wake.set()
        return None, res
    try:
        async with run.semaphore:
            resp = await run.llm.sample(run.system, messages, run.cfg.max_tokens, model, effort, long_cache)
    except asyncio.CancelledError:
        run.budget.release(res)
        raise
    except Exception as e:  # noqa: BLE001 - an API failure stops this agent, never the run
        run.budget.release(res)
        run.db.event(agent, "api_error", {"type": type(e).__name__, "message": str(e)[:2000]})
        return None, "api_error"
    before = run.budget.percent
    run.budget.settle(res, resp.usage.cost)
    if config.notice_mark(before, run.budget.percent):
        run.wake.set()                  # a budget mark was passed: those who are told hear it now
    _record_usage(run, agent, resp)
    return resp, ""


def _record_usage(run: Run, agent: str, resp: llm.LLMResponse) -> None:
    u = resp.usage
    run.db.execute(
        "INSERT INTO usage(agent, input_tokens, cache_write_tokens, cache_read_tokens, output_tokens,"
        " prompt_tokens, compaction, cost, latency, stop_reason, raw, created)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (agent, u.input_tokens, u.cache_write_tokens, u.cache_read_tokens, u.output_tokens,
         u.prompt_tokens, int(u.compaction), u.cost, resp.latency, resp.stop_reason,
         json.dumps(resp.raw_usage), time.time()),
    )


async def _compact(run: Run, agent: str, messages: list[dict], est: int):
    """On-demand compaction. Returns (new history, estimate), or (None, est) if the agent must stop."""
    if est > run.cfg.refuse_above:
        run.db.event(agent, "stopped", {"reason": "context", "estimate": est})
        return None, est
    for attempt in range(2):
        model, effort, long_cache = run.call_settings(agent)
        res = run.budget.reserve(agent, llm.worst_case_cost(config.prices(model), est, run.cfg.max_tokens))
        if isinstance(res, str):
            run.db.event(agent, "stopped", {"reason": res})
            return None, est
        try:
            async with run.semaphore:
                resp = await run.llm.compact(run.system, messages, run.cfg.max_tokens, model, effort, long_cache)
        except asyncio.CancelledError:
            run.budget.release(res)
            raise
        except Exception as e:  # noqa: BLE001
            run.budget.release(res)
            run.db.event(agent, "compaction_error", {"attempt": attempt, "error": str(e)[:2000]})
            continue
        run.budget.settle(res, resp.usage.cost)
        _record_usage(run, agent, resp)
        blocks = resp.content
        if resp.stop_reason == "compaction" and len(blocks) == 1 and blocks[0].get("type") == "compaction":
            run.db.add_message(agent, "assistant", blocks, kind="compaction")
            run.db.event(agent, "compacted", {"before": est})
            new_est = llm.estimate_tokens(blocks[0].get("content") or "") + 4000
            _save_est(run, agent, new_est)
            return [{"role": "assistant", "content": blocks}], new_est
        run.db.event(agent, "compaction_error", {"attempt": attempt, "stop_reason": resp.stop_reason})
    run.db.event(agent, "stopped", {"reason": "compaction_failed", "estimate": est})
    return None, est


async def _execute(run: Run, agent: str, seq: int, box: _Box, tool_use: dict, max_chars: int) -> tuple[str, bool]:
    """Run one bash tool call. Returns (text for the model, is_error)."""
    inp = tool_use.get("input") or {}
    if inp.get("restart"):
        await box.close()
        await box.get()
        return "The terminal was restarted.", False
    cmd = inp.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return "Error: the bash tool needs a non-empty `command`.", True
    if refusal := _waiting_command(run, agent, cmd):
        return refusal, True

    sb = await box.get()
    t0 = time.monotonic()
    res = await sb.run(cmd, run.cfg.command_timeout)
    seconds = time.monotonic() - t0

    out_path = run.cfg.run_dir / "outputs" / agent / f"{seq:04d}.txt"
    out_path.write_text(res.output)
    inside_path = f"{sandbox.ROOT}/outputs/{agent}/{out_path.name}"
    run.db.execute(
        "INSERT INTO commands(agent, cmd, exit_code, seconds, output_bytes, output_path, timed_out, created)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (agent, cmd, res.exit_code, seconds, res.total_bytes, str(out_path), int(res.timed_out), time.time()),
    )

    text = res.output
    parts = []
    if len(text) > max_chars:
        parts.append(f"[output cut: last {max_chars:,} of {len(text):,} characters; full output in {inside_path}]")
        text = text[-max_chars:]
    if res.dropped_bytes:
        parts.append(f"[{res.dropped_bytes:,} bytes from the middle of the output were not kept]")
    parts.append(text)
    is_error = False
    if res.timed_out:
        parts.append(f"[timed out after {run.cfg.command_timeout} s: the terminal was restarted, so "
                     "running processes, the current directory and variables were lost]")
        is_error = True
        run.db.event(agent, "command_timeout", {"cmd": cmd[:500]})
    elif res.sandbox_died:
        parts.append("[the shell exited: the terminal was restarted]")
        is_error = True
    elif res.exit_code:
        parts.append(f"[exit code {res.exit_code}]")
    if res.timed_out or res.sandbox_died:
        await box.close()
    return "\n".join(p for p in parts if p) or "(no output)", is_error
