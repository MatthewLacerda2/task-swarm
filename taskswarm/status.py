"""What a person sees of a run: `status` and `transcript`."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from . import config


def _open(run_id: str) -> sqlite3.Connection:
    path = config.RUNS_ROOT / run_id / "run.db"
    if not path.exists():
        raise SystemExit(f"no run {run_id} under {config.RUNS_ROOT}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _meta(db: sqlite3.Connection, key: str, default=None):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row["value"])


def render(run_id: str) -> str:
    db = _open(run_id)
    cfg = config.RunConfig.from_dict(_meta(db, "config"))
    spent = db.execute("SELECT COALESCE(SUM(cost), 0) FROM usage").fetchone()[0]
    out = [f"Run {run_id}: {_meta(db, 'status')}" + (f" ({r})" if (r := _meta(db, "status_reason")) else "")]
    out.append(f"Folder: {cfg.run_dir}")
    out.append(f"Spent US$ {spent:.3f} of {cfg.run_budget:.2f} ({100 * spent / cfg.run_budget:.0f}%), "
               f"hard stop at {cfg.hard_stop:.2f}")
    u = db.execute("SELECT COUNT(*) n, SUM(input_tokens) i, SUM(cache_write_tokens) w, SUM(cache_read_tokens) r,"
                   " SUM(output_tokens) o, SUM(compaction) c, AVG(latency) l FROM usage").fetchone()
    if u["n"]:
        prompt = (u["i"] or 0) + (u["w"] or 0) + (u["r"] or 0)
        hit = 100 * (u["r"] or 0) / prompt if prompt else 0
        out.append(f"Calls {u['n']}, cache hit {hit:.0f}%, output {u['o'] or 0:,} tokens, compactions {u['c'] or 0}, "
                   f"avg latency {u['l'] or 0:.1f} s, US$ {spent / u['n']:.4f} per call")
    planner = db.execute("SELECT spent, calls FROM agents WHERE id = 'planner'").fetchone()
    if planner:
        out.append(f"Plan drafted by {cfg.plan_model}: {planner['calls']} calls, US$ {planner['spent']:.3f}")
    if goal := _meta(db, "goal"):
        out += ["", f"Goal: {goal}", f"Done when: {_meta(db, 'done_when')}"]

    tasks = db.execute("SELECT * FROM tasks ORDER BY seq").fetchall()
    if tasks:
        out += ["", "Tasks:"]
        # A task's cost includes its reviewers and, for a task that was split, its sub-team.
        for t in sorted(tasks, key=lambda t: [next(x["seq"] for x in tasks if x["id"] == ".".join(
                t["id"].split(".")[:i + 1])) for i in range(t["id"].count(".") + 1)]):
            cost = db.execute("SELECT COALESCE(SUM(spent), 0) FROM agents WHERE task = ? OR task LIKE ?",
                              (t["id"], t["id"] + ".%")).fetchone()[0]
            blockers = json.loads(t["blocked_by"])
            name = "  " * t["id"].count(".") + t["id"]
            state = t["status"] + ("*" if t["status"] == "done" and "gaps" in t.keys() and t["gaps"] else "")
            line = f"  {name:<28} {state:<10} reports {t['attempts']}  US$ {cost:.3f}  {t['title']}"
            if blockers:
                line += f"  (after {', '.join(blockers)})"
            out.append(line)
        if any(t["status"] == "done" and "gaps" in t.keys() and t["gaps"] for t in tasks):
            out.append("  * done with something missing, accepted by the task's owner; see the run's README.md")

    schema_path = cfg.run_dir / "store" / "schema.json"
    if schema_path.exists():
        out += ["", "Store:"]
        for name in json.loads(schema_path.read_text()):
            path = cfg.run_dir / "store" / f"{name}.jsonl"
            with path.open() as f:
                n = sum(1 for _ in f)
            out.append(f"  {name:<24} {n:,} records")
        sig = cfg.run_dir / "store" / "signals.jsonl"
        if sig.exists() and sig.stat().st_size:
            with sig.open() as f:
                out.append(f"  signals: {sum(1 for _ in f)} conflicting records, in {sig}")
        chg = cfg.run_dir / "store" / "changes.jsonl"
        if chg.exists() and chg.stat().st_size:
            with chg.open() as f:
                out.append(f"  corrections: {sum(1 for _ in f)} updates or deletions, in {chg}")

    try:
        chans = db.execute("SELECT c.name, c.created, c.created_by, COUNT(p.id) AS n FROM channels c"
                           " LEFT JOIN posts p ON p.channel = c.name GROUP BY c.name ORDER BY c.created").fetchall()
    except sqlite3.OperationalError:      # a run made before channels existed
        chans = []
    if chans:
        out += ["", f"Channels (in {cfg.run_dir / 'channels'}):"]
        for c in chans:
            made = dt.datetime.fromtimestamp(c["created"]).strftime("%Y-%m-%d %H:%M")
            out.append(f"  #{c['name']:<24} {c['n']} posts, created {made} by {c['created_by']}")

    if d := _meta(db, "deliverable"):
        out += ["", f"Deliverable: {cfg.run_dir / 'deliverable' / d['file']}", f"  {d['summary']}"]
    if findings := _meta(db, "final_findings"):
        out += ["", "Final review:", f"  {findings}"]
    return "\n".join(out)


def first_plan(run_id: str) -> dict:
    """The plan a run started from: the input of the first `plan` call that was accepted."""
    db = _open(run_id)
    rows = db.execute("SELECT role, content FROM messages WHERE agent IN ('planner', 'lead') ORDER BY id").fetchall()
    refused = {b["tool_use_id"] for r in rows if r["role"] == "user" for b in json.loads(r["content"])
               if b.get("type") == "tool_result" and b.get("is_error")}
    for r in rows:
        if r["role"] == "assistant":
            for b in json.loads(r["content"]):
                if b.get("type") == "tool_use" and b["name"] == "plan" and b["id"] not in refused:
                    return b.get("input") or {}
    if given := db.execute("SELECT 1 FROM meta WHERE key = 'plan_from'").fetchone():
        raise SystemExit(f"run {run_id} started from a plan file; no agent set its plan")
    raise SystemExit(f"run {run_id} has no accepted plan")


def transcript(run_id: str, agent: str) -> str:
    """One agent's conversation as readable text."""
    db = _open(run_id)
    rows = db.execute("SELECT role, kind, content FROM messages WHERE agent = ? ORDER BY id", (agent,)).fetchall()
    if not rows:
        names = ", ".join(r["id"] for r in db.execute("SELECT id FROM agents ORDER BY created"))
        raise SystemExit(f"no agent {agent} in {run_id}; agents: {names}")
    out = [f"# {agent} ({run_id})"]
    for r in rows:
        for b in json.loads(r["content"]):
            kind = b.get("type")
            if kind == "text":
                out.append(f"\n## {'To the agent' if r['role'] == 'user' else agent}\n\n{b['text']}")
            elif kind == "thinking" and b.get("thinking"):
                out.append(f"\n## {agent} (thinking)\n\n{b['thinking']}")
            elif kind == "tool_use":
                inp = b.get("input") or {}
                body = inp.get("command") if b["name"] == "bash" else json.dumps(inp, indent=1, ensure_ascii=False)
                out.append(f"\n## {agent} calls {b['name']}\n\n```\n{body}\n```")
            elif kind == "tool_result":
                out.append(f"\n## Result{' (error)' if b.get('is_error') else ''}\n\n```\n{b.get('content')}\n```")
            elif kind == "compaction":
                out.append(f"\n## Memory compacted into this summary\n\n{b.get('content')}")
    return "\n".join(out)
