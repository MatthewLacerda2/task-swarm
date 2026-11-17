"""What a person sees of a run: `status` and `transcript`."""

from __future__ import annotations

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
    cfg = config.RunConfig(**_meta(db, "config"))
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
    if goal := _meta(db, "goal"):
        out += ["", f"Goal: {goal}", f"Done when: {_meta(db, 'done_when')}"]

    tasks = db.execute("SELECT * FROM tasks ORDER BY seq").fetchall()
    if tasks:
        out += ["", "Tasks:"]
        for t in tasks:
            cost = db.execute("SELECT COALESCE(SUM(spent), 0) FROM agents WHERE task = ?", (t["id"],)).fetchone()[0]
            blockers = json.loads(t["blocked_by"])
            line = f"  {t['id']:<24} {t['status']:<10} reports {t['attempts']}  US$ {cost:.3f}  {t['title']}"
            if blockers:
                line += f"  (after {', '.join(blockers)})"
            out.append(line)

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

    if d := _meta(db, "deliverable"):
        out += ["", f"Deliverable: {cfg.run_dir / 'deliverable' / d['file']}", f"  {d['summary']}"]
    if findings := _meta(db, "final_findings"):
        out += ["", "Final review:", f"  {findings}"]
    return "\n".join(out)


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
