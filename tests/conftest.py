"""A scripted fake model, so whole runs can be tested without the API."""

from __future__ import annotations

import itertools
import re

import pytest

from taskswarm import config, llm
from taskswarm import run as runmod

_ids = itertools.count()
PRICES = config.MODELS["claude-haiku-5-5"]


def call(name: str, **inp) -> dict:
    return {"type": "tool_use", "id": f"toolu_{next(_ids):06d}", "name": name, "input": inp}


def bash(cmd: str) -> dict:
    return call("bash", command=cmd)


def texts_of(message: dict) -> str:
    out = []
    for b in message["content"]:
        if b.get("type") == "text":
            out.append(b["text"])
        elif b.get("type") == "tool_result":
            out.append(str(b.get("content")))
    return "\n".join(out)


class FakeLLM:
    """scripts: agent -> a list of steps, or a function(messages) -> step.

    A step is a list of content blocks (tool calls) or a string (the agent ends its
    turn saying it). An agent past the end of its list ends its turn. A reviewer
    without a script passes the work.
    """

    def __init__(self, scripts: dict, output_tokens: int = 200):
        self.scripts = scripts
        self.output_tokens = output_tokens
        self.calls: list[str] = []
        self.seen: dict[str, list[str]] = {}
        self.compactions = 0
        self.big: set[str] = set()

    @staticmethod
    def who(messages: list[dict]) -> str:
        for m in messages:
            if m["role"] == "user":
                if mm := re.search(r"You are `([^`]+)`", texts_of(m)):
                    return mm.group(1)
        raise AssertionError("an agent was called without being told who it is")

    async def sample(self, system, messages, max_tokens):
        agent = self.who(messages)
        self.calls.append(agent)
        self.seen.setdefault(agent, []).append(texts_of(messages[-1]))
        script = self.scripts.get(agent)
        if script is None and ".review" in agent:
            script = [[call("verdict", passed=True, findings="Opened the output and checked it against the brief.")]]
        n = sum(1 for m in messages if m["role"] == "assistant")
        if callable(script):
            step = script(messages)
        else:
            step = script[n] if script and n < len(script) else "Nothing more to do."
        prompt = 90_000 if agent in self.big else 3000
        usage = {"input_tokens": 500, "cache_creation_input_tokens": 0, "cache_read_input_tokens": prompt - 500,
                 "output_tokens": self.output_tokens}
        thinking = {"type": "thinking", "thinking": f"{agent} thinks", "signature": "sig"}
        if isinstance(step, str):
            content, stop = [thinking, {"type": "text", "text": step}], "end_turn"
        else:
            content, stop = [thinking, *step], "tool_use"
        return llm.LLMResponse(content, stop, llm.usage_from_dict(usage, PRICES), usage)

    async def compact(self, system, messages, max_tokens):
        self.compactions += 1
        agent = self.who(messages)
        self.big.discard(agent)
        usage = {"input_tokens": 0, "output_tokens": 0,
                 "iterations": [{"type": "compaction", "input_tokens": 91_000, "output_tokens": 800,
                                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}]}
        block = {"type": "compaction", "content": f"summary of what `{agent}` did", "signature": "s"}
        return llm.LLMResponse([block], "compaction", llm.usage_from_dict(usage, PRICES), usage)


@pytest.fixture
def make_run(tmp_path, monkeypatch):
    """make_run(scripts, **config) -> (run, fake). Runs live under the test's tmp folder."""
    monkeypatch.setattr(config, "RUNS_ROOT", tmp_path / "runs")
    made = []

    def make(scripts: dict, task: str = "Find things.", output_tokens: int = 200, run_id: str = "t", **cfg):
        fake = FakeLLM(scripts, output_tokens)
        run = runmod.open_run(config.RunConfig(run_id=run_id, **cfg), fake, task)
        made.append(run)
        return run, fake

    yield make
    for run in made:
        run.db.conn.close()


BRIEF = "Find the companies that make the thing and store each one in `companies` with its source. " \
        "Only companies with a public website count."
VERIFY = "Open three stored records and check that each source page names the company."
COLLECTIONS = {
    "companies": {
        "description": "Companies that make the thing",
        "key": ["name"],
        "fields": {
            "name": {"type": "string", "required": True},
            "employees": {"type": "integer", "min": 0},
            "kind": {"type": "string", "enum": ["maker", "reseller"]},
        },
    }
}


def task(tid: str, blocked_by: list[str] | None = None, **over) -> dict:
    return {"id": tid, "title": f"Task {tid}", "brief": BRIEF, "verify": VERIFY,
            "blocked_by": blocked_by or []} | over


def record(name: str, **over) -> dict:
    return {"name": name, "source": "https://example.com/" + name, "retrieved": "2026-11-01"} | over


def dump(fake, agent):   # for debugging a failing test
    return "\n=====\n".join(fake.seen.get(agent, []))
