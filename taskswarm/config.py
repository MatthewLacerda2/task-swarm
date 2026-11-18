"""Paths, prices and run values.

Everything an agent may learn about its limits lives in RunConfig and is rendered
into the system prompt, so every number an agent reads is the number the code enforces.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
AGENT_BIN = PROJECT / "agent_bin"
PROMPTS = Path(__file__).resolve().parent / "prompts"

# Machine-specific settings (gitignored): see config.local.example.toml.
_LOCAL_FILE = Path(__file__).resolve().parent / "config.local.toml"
LOCAL: dict = tomllib.loads(_LOCAL_FILE.read_text()) if _LOCAL_FILE.exists() else {}

_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
RUNS_ROOT = Path(os.environ.get("TASK_SWARM_RUNS", LOCAL.get("runs_root", str(_DATA_HOME / "task-swarm" / "runs"))))

API_KEY_FILE = Path(LOCAL.get("api_key_file", str(Path.home() / ".config" / "task-swarm" / "anthropic.key")))

UV_BINARY = Path.home() / ".local" / "bin" / "uv"

# Host port reachable from inside the sandbox (pasta -T): the SearXNG behind `search`.
SEARXNG_PORT = 8888


@dataclass(frozen=True)
class Prices:
    """US$ per million tokens, for prompts up to `long_prompt` tokens."""

    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float
    long_prompt: int = 100_000       # above this the whole request costs `long_multiplier` times more
    long_multiplier: int = 5


# The budget is only as good as these numbers: a model is usable once its prices are here.
# Cache writes cost 1.25x (5 minutes) and 2x (1 hour) the input price. Only Haiku 5.5
# charges more for long prompts.
MODELS = {
    "claude-haiku-5-5": Prices(input=0.10, cache_write_5m=0.125, cache_write_1h=0.20, cache_read=0.01, output=0.50),
    "claude-sonnet-5-5": Prices(input=2.00, cache_write_5m=2.50, cache_write_1h=4.00, cache_read=0.10, output=10.00,
                                long_multiplier=1),
    "claude-opus-5-5": Prices(input=4.00, cache_write_5m=5.00, cache_write_1h=8.00, cache_read=0.20, output=20.00,
                              long_multiplier=1),
    "claude-fable-5-2": Prices(input=8.00, cache_write_5m=10.00, cache_write_1h=16.00, cache_read=0.20, output=40.00,
                               long_multiplier=1),
}


# Weakest to strongest. An agent only hands work to a model equal to or below its own.
RANK = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2")
ALIASES = {"haiku": RANK[0], "sonnet": RANK[1], "opus": RANK[2], "fable": RANK[3], "none": ""}


def prices(model: str) -> Prices:
    if model not in MODELS:
        raise SystemExit(f"no prices for model {model!r}: add them to MODELS in taskswarm/config.py")
    return MODELS[model]


def resolve_model(name: str) -> str:
    """`opus` -> `claude-opus-5-5`; a full id is kept."""
    return ALIASES.get(name.strip().lower(), name.strip())


def worst_call(model: str, max_tokens: int, prompt_tokens: int = 8_000) -> float:
    """Upper bound for one early call: the whole prompt written to the 1-hour cache, full output, +20%."""
    p = prices(model)
    return (prompt_tokens * p.cache_write_1h + max_tokens * p.output) / 1_000_000 * 1.2


LEAD_NOTICES = (50, 80, 100)   # % of the run budget at which the lead is told


@dataclass
class RunConfig:
    run_id: str
    # Who runs on what. The user picks these before the run, by how much the task matters
    # and how hard it is: the planner drafts the plan ("" = the lead plans), the lead
    # owns it and writes the deliverable, workers and their reviewers do the tasks.
    # No one hands work to a stronger model than its own: worker <= lead <= planner.
    plan_model: str = "claude-opus-5-5"
    lead_model: str = "claude-haiku-5-5"
    worker_model: str = "claude-haiku-5-5"
    plan_effort: str = "high"
    effort: str = "high"             # the lead's, the workers' and the reviewers'
    planner_budget: float = 0.60
    plan_max_calls: int = 6
    # Budgets (US$). At run_budget the wind-down starts: no new task starts and the lead
    # is asked to deliver. At run_budget x hard_stop_ratio the run stops.
    run_budget: float = 2.0
    hard_stop_ratio: float = 1.2
    lead_budget: float = 0.60
    agent_budget: float = 0.40       # any one worker or reviewer
    task_budget: float = 1.00        # a top-level task with its reviewers and its whole sub-team
    # The plan
    max_tasks: int = 40              # in the whole run, sub-tasks included
    max_depth: int = 2               # 1 = only the lead splits work; 2 = a worker may split its task once
    max_subtasks: int = 6            # pieces one worker may split its task into
    max_parallel: int = 4            # tasks worked on at once
    max_attempts: int = 3            # reports a worker gets reviewed before its task fails
    max_final_reviews: int = 2
    summary_chars: int = 4_000       # a report's summary: what the lead carries in its context
    # Channels
    post_chars: int = 1_000          # one post
    max_channels: int = 12           # in the whole run
    # Calls
    concurrency: int = 8
    max_tokens: int = 12_000
    output_chars: int = 4_000
    max_calls: int = 150             # model calls one agent may make on one assignment
    # Context
    compaction_trigger: int = 85_000
    refuse_above: int = 99_000
    # Sandbox
    command_timeout: int = 300
    sandbox_memory_gb: int = 3
    sandbox_cpus: int = 2
    sandbox_tasks: int = 512
    block_lan: bool = True           # agents can't reach the local network (nftables filter)
    data_dir: str = ""               # a folder of yours every agent can read at /task/data (big downloads, once)
    # Store
    import_max_mb: int = 2_000

    @property
    def hard_stop(self) -> float:
        return round(self.run_budget * self.hard_stop_ratio, 2)

    @classmethod
    def from_dict(cls, data: dict) -> "RunConfig":
        """A stored config, including one written by an older version."""
        data = dict(data)
        if "model" in data:                       # one model for the whole team
            data.setdefault("worker_model", data["model"])
            data.setdefault("lead_model", data["model"])
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def planner_fits(self) -> bool:
        """A planner is set, is not the lead's own model, and one of its calls at worst fits the budget."""
        if not self.plan_model or self.plan_model == self.lead_model:
            return False
        worst = worst_call(self.plan_model, self.max_tokens)
        return worst <= self.run_budget / 3 and worst <= self.planner_budget

    def scale_caps(self, ratio: float) -> None:
        """The per-agent and per-task caps grow with the run budget."""
        for name in ("lead_budget", "agent_budget", "task_budget", "planner_budget"):
            setattr(self, name, round(getattr(self, name) * ratio, 2))

    def problems(self) -> list[str]:
        """What must be fixed before this run can start."""
        out = []
        roles = {"planner": self.plan_model, "lead": self.lead_model, "worker": self.worker_model}
        for role, model in roles.items():
            if model and model not in MODELS:
                out.append(f"the {role}'s model {model!r} has no prices in taskswarm/config.py; priced models: "
                           f"{', '.join(RANK)} (or haiku, sonnet, opus, fable)")
        if out:
            return out
        rank = RANK.index
        if rank(self.worker_model) > rank(self.lead_model):
            out.append(f"a {self.lead_model} lead can't hand work to {self.worker_model} workers: no one hands work "
                       "to a stronger model than its own. Raise the lead or lower the workers.")
        if self.plan_model and rank(self.lead_model) > rank(self.plan_model):
            out.append(f"a {self.plan_model} planner can't hand its plan to a {self.lead_model} lead: no one hands "
                       "work to a stronger model than its own. Raise the planner, lower the lead, or run without a "
                       "planner (--planner none).")
        for role, model, cap, name in (("lead", self.lead_model, self.lead_budget, "lead_budget"),
                                       ("worker", self.worker_model, self.agent_budget, "agent_budget")):
            need = 3 * worst_call(model, self.max_tokens)
            if cap < need:
                out.append(f"{name} is US$ {cap:.2f}, too little for a {model} {role}: one of its calls can cost up "
                           f"to US$ {need / 3:.2f}. Set {name} to at least US$ {need:.2f} (--set {name}=...), or "
                           "raise --budget, which the caps grow with.")
        if self.data_dir and not Path(self.data_dir).is_dir():
            out.append(f"data_dir {self.data_dir!r} is not a folder")
        return out

    @property
    def run_dir(self) -> Path:
        return RUNS_ROOT / self.run_id

    def to_dict(self) -> dict:
        return asdict(self)


def read_api_key() -> str:
    if API_KEY_FILE.exists():
        key = API_KEY_FILE.read_text().strip()
        if key:
            return key
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    raise SystemExit(
        f"No API key: put only the key in {API_KEY_FILE} and chmod 600 it, or set ANTHROPIC_API_KEY."
    )
