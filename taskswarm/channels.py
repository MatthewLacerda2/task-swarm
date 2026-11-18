"""Channels: where agents tell each other what they learned while the work is going on.

A channel has a name (its theme) and a creation time; a post has an author, a time and
a text. The first post to a name creates the channel. Channels carry tips ("this API
limits us, read the web pages instead"); findings go in the store and results in
reports.

  - Posts are in the database (so a resumed run has them) and in one JSON Lines file
    per channel under <run>/channels/, read-only inside the sandboxes at
    /task/channels. The first line of a file is the channel itself.
  - An agent is shown the posts made by others since it last looked, together with
    the result of its next command or the next time it is woken for another reason.
    A post never wakes anyone and never costs anyone a model call.
  - A post changes no task: only a task's owner does that (handlers._send_message).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time

from .run import Run
from .sandbox import ROOT

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
NEWS_CHARS = 3_000      # of posts shown to an agent at once; the rest it reads in the files


def _stamp(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(run: Run, name: str):
    return run.cfg.run_dir / "channels" / f"{name}.jsonl"


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def names(run: Run) -> list[str]:
    return [r["name"] for r in run.db.all("SELECT name FROM channels ORDER BY created, name")]


def post(run: Run, agent: str, channel: object, text: object) -> tuple[str, bool]:
    """Add a post, creating the channel if it is new. Returns (text for the agent, is_error)."""
    cfg, db = run.cfg, run.db
    if not isinstance(text, str) or not text.strip():
        return "The post is empty.", True
    text = text.strip()
    if len(text) > cfg.post_chars:
        return (f"The post is {len(text):,} characters; the limit is {cfg.post_chars:,}. Say the one thing the "
                "others need; put detail in a file in your folder and name it."), True
    name = channel.strip().lstrip("#") if isinstance(channel, str) else ""
    if not NAME_RE.match(name):
        return ("The channel name must be 2 to 32 characters: lowercase letters, digits and -, starting with a "
                "letter (for example `github-api`)."), True
    have = names(run)
    now = time.time()
    created = name not in have
    if created and len(have) >= cfg.max_channels:
        return (f"There are already {len(have)} channels, the most a run can have: "
                f"{', '.join('#' + n for n in have)}. Post in the one closest to your theme."), True
    with db.transaction():
        if created:
            db.execute("INSERT INTO channels(name, created_by, created) VALUES(?, ?, ?)", (name, agent, now))
        db.execute("INSERT INTO posts(channel, agent, body, created) VALUES(?, ?, ?, ?)", (name, agent, text, now))
    with _path(run, name).open("a", encoding="utf-8") as f:
        if created:
            f.write(_line({"channel": name, "created": _stamp(now), "by": agent}))
        f.write(_line({"at": _stamp(now), "by": agent, "text": text}))
    db.event(agent, "post", {"channel": name, "chars": len(text), "created": created})
    if not created:
        return f"Posted to #{name}.", False
    others = [n for n in have if n != name]
    return (f"Posted to #{name}, a new channel." +
            (f" The other channels: {', '.join('#' + n for n in others)}." if others else "")), False


def unread(run: Run, agent: str) -> str | None:
    """The posts others made since this agent last looked, as one text; they then count as seen."""
    db = run.db
    row = db.agent(agent)
    last = db.one("SELECT COALESCE(MAX(id), 0) AS id FROM posts")["id"]
    if row is None or last <= row["seen_post"]:
        return None
    rows = db.all("SELECT * FROM posts WHERE id > ? AND agent != ? ORDER BY id", (row["seen_post"], agent))
    db.execute("UPDATE agents SET seen_post = ? WHERE id = ?", (last, agent))
    if not rows:
        return None
    shown: list[str] = []
    size = 0
    for p in reversed(rows):                       # when there are too many, the newest are shown
        body = "\n".join("  " + line for line in p["body"].splitlines())
        entry = f"#{p['channel']} · `{p['agent']}` · {_stamp(p['created'])}\n{body}"
        if shown and size + len(entry) > NEWS_CHARS:
            break
        shown.append(entry)
        size += len(entry)
    head = "[New in the channels]"
    if len(shown) < len(rows):
        head += (f" The {len(shown)} newest of {len(rows)} posts; the earlier ones are in "
                 f"{ROOT}/channels/<name>.jsonl.")
    return "\n".join([head, *reversed(shown)])


def sync(run: Run) -> None:
    """Write the channel files again from the database: after a crash a file may lack its last post."""
    folder = run.cfg.run_dir / "channels"
    for ch in run.db.all("SELECT * FROM channels ORDER BY created, name"):
        lines = [_line({"channel": ch["name"], "created": _stamp(ch["created"]), "by": ch["created_by"]})]
        for p in run.db.all("SELECT * FROM posts WHERE channel = ? ORDER BY id", (ch["name"],)):
            lines.append(_line({"at": _stamp(p["created"]), "by": p["agent"], "text": p["body"]}))
        tmp = folder / f".{ch['name']}.tmp"
        tmp.write_text("".join(lines), encoding="utf-8")
        tmp.replace(folder / f"{ch['name']}.jsonl")
