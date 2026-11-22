"""The run's web searches: counted, announced as they run out, refused past the run's limit.

The sandbox's loopback port for `search` is forwarded to this gate and not to SearXNG
itself (sandbox.full_command). So a search made any other way (curl, a script) counts
the same, and nothing inside a sandbox reaches SearXNG around the gate.

    GET /search?q=...   one search: passed on to SearXNG and counted if it answered
                        (an empty answer because the engines behind SearXNG were
                        blocked is not counted). The answer carries X-Searches-Used
                        and X-Searches-Limit. 429 once the limit is reached.
    GET /count          {"used": n, "limit": m}. Costs nothing.

The count lives in the run's database, so it survives a resume.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

from . import config

UPSTREAM = f"http://127.0.0.1:{config.SEARXNG_PORT}/search"
UPSTREAM_TIMEOUT = 40
REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
           429: "Too Many Requests", 502: "Bad Gateway"}
JSON = "application/json"


def _fetch(query: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(f"{UPSTREAM}?{query}", timeout=UPSTREAM_TIMEOUT) as r:
            return r.status, r.read(), r.headers.get("Content-Type", JSON)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", JSON)


def _unanswered(body: bytes) -> bool:
    """SearXNG answered, but with nothing, and says its engines did not answer (blocked, rate-limited)."""
    try:
        data = json.loads(body)
    except ValueError:
        return False
    return isinstance(data, dict) and not data.get("results") and bool(data.get("unresponsive_engines"))


def _error(text: str) -> bytes:
    return json.dumps({"error": text}).encode()


class SearchGate:
    def __init__(self, run):
        self.run = run
        self.db = run.db
        self.limit = run.cfg.max_searches       # 0: counted, never refused
        self.server: asyncio.Server | None = None
        self.port = config.SEARXNG_PORT         # until started, a sandbox talks to SearXNG itself

    @property
    def used(self) -> int:
        return self.db.get_meta("searches", 0)

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None

    def crossed(self) -> int | None:
        """The highest notice mark passed since the last call, if any; remembered in the database."""
        if not self.limit:
            return None
        done = set(self.db.get_meta("search_notices_sent", []))
        percent = 100 * self.used / self.limit
        new = [t for t in config.NOTICES if percent >= t and t not in done]
        if new:
            self.db.set_meta("search_notices_sent", sorted(done | set(new)))
        return max(new, default=None)

    # --- one request ---------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            method, target, _ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
            status, body, kind = await self._answer(method, target)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError, ConnectionError):
            status, body, kind = 400, _error("not a request this gate understands"), JSON
        lines = [
            f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}",
            f"Content-Type: {kind}",
            f"Content-Length: {len(body)}",
            f"X-Searches-Used: {self.used}",
            f"X-Searches-Limit: {self.limit}",
            "Connection: close",
        ]
        try:
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def _answer(self, method: str, target: str) -> tuple[int, bytes, str]:
        url = urlsplit(target)
        if method != "GET":
            return 405, _error("use GET /search?q=...&format=json"), JSON
        if url.path == "/count":
            return 200, json.dumps({"used": self.used, "limit": self.limit}).encode(), JSON
        if url.path != "/search":
            return 404, _error("only /search and /count exist here"), JSON
        used = self.used
        if self.limit and used >= self.limit:
            return 429, _error(f"the run's {self.limit} web searches are used up"), JSON
        # Counted before it is sent, so searches made at the same moment can't pass the limit together.
        self.db.set_meta("searches", used + 1)
        try:
            status, body, kind = await asyncio.to_thread(_fetch, url.query)
        except OSError as e:
            status, body, kind = 502, _error(f"the search service did not answer: {e}"), JSON
        if status != 200 or _unanswered(body):
            self.db.set_meta("searches", self.used - 1)     # nothing was searched: not counted
            return status, body, kind
        query = " ".join(parse_qs(url.query).get("q", []))
        self.db.event(None, "search", {"q": query[:300]})
        if self.limit and config.notice_mark(100 * used / self.limit, 100 * (used + 1) / self.limit):
            self.run.wake.set()                             # the orchestrator tells the team
        return status, body, kind
