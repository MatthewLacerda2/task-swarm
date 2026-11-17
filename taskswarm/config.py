"""Paths, prices and run values.

Everything an agent may learn about its limits lives in RunConfig and is rendered
into the system prompt, so every number an agent reads is the number the code enforces.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass
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
MODELS = {
    "claude-haiku-5-5": Prices(input=0.10, cache_write_5m=0.125, cache_write_1h=0.20, cache_read=0.01, output=0.50),
}

LEAD_NOTICES = (50, 80, 100)   # % of the run budget at which the lead is told


@dataclass
class RunConfig:
    run_id: str
    model: str = "claude-haiku-5-5"
    # Budgets (US$). At run_budget the wind-down starts: no new task starts and the lead
    # is asked to deliver. At run_budget x hard_stop_ratio the run stops.
    run_budget: float = 2.0
    hard_stop_ratio: float = 1.2
    lead_budget: float = 0.60
    agent_budget: float = 0.40
    # The plan
    max_tasks: int = 40
    max_parallel: int = 4            # tasks worked on at once
    max_attempts: int = 3            # reports a worker gets reviewed before its task fails
    max_final_reviews: int = 2
    summary_chars: int = 4_000       # a report's summary: what the lead carries in its context
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
    block_lan: bool = False          # nftables filter for private addresses
    # Store
    import_max_mb: int = 2_000

    @property
    def hard_stop(self) -> float:
        return round(self.run_budget * self.hard_stop_ratio, 2)

    @property
    def prices(self) -> Prices:
        if self.model not in MODELS:
            raise SystemExit(f"no prices for model {self.model!r}: add them to MODELS in taskswarm/config.py")
        return MODELS[self.model]

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
