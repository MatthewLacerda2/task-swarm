"""The run's web searches: counted by a gate between the sandbox and SearXNG, announced
as they run out, refused past the limit. A fake SearXNG, real sandboxes, no API calls."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

import pytest

from conftest import COLLECTIONS, GOAL, bash, call, lead_script, task
from taskswarm import config, orchestrator, prompts, searches, status


@pytest.fixture
async def searxng(monkeypatch):
    """A stand-in for SearXNG: answers every search with one result that repeats the query."""
    asked = []

    async def serve(reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        target = head.split(" ")[1]
        asked.append(target)
        answer = {"results": [{"title": "A page", "url": "https://example.com/a", "content": target}]}
        if "blocked" in target:
            answer = {"results": [], "unresponsive_engines": [["google cse", "too many requests"]]}
        body = json.dumps(answer).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n" % len(body) + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(searches, "UPSTREAM", f"http://127.0.0.1:{port}/search")
    yield asked
    server.close()


def _get(port: int, path: str, method: str = "GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e), dict(e.headers)


async def get(gate, path: str, method: str = "GET"):
    return await asyncio.to_thread(_get, gate.port, path, method)


async def test_gate_counts_passes_on_and_refuses(make_run, searxng, monkeypatch):
    run, _ = make_run({}, max_searches=3)
    gate = searches.SearchGate(run)
    await gate.start()
    try:
        assert await get(gate, "/count") == (200, {"used": 0, "limit": 3}, (await get(gate, "/count"))[2])
        for n in (1, 2, 3):
            code, data, headers = await get(gate, f"/search?q=truck+{n}&format=json")
            assert code == 200 and data["results"][0]["url"] == "https://example.com/a"
            assert headers["X-Searches-Used"] == str(n) and headers["X-Searches-Limit"] == "3"
        assert searxng == [f"/search?q=truck+{n}&format=json" for n in (1, 2, 3)]      # passed on as asked

        code, data, headers = await get(gate, "/search?q=one+more&format=json")
        assert code == 429 and "3 web searches are used up" in data["error"] and headers["X-Searches-Used"] == "3"
        assert len(searxng) == 3                                                       # never reached SearXNG
        # asking for the count is free, and nothing else is served
        assert (await get(gate, "/count"))[1] == {"used": 3, "limit": 3}
        assert (await get(gate, "/"))[0] == 404 and (await get(gate, "/search?q=x", "POST"))[0] == 405

        # the count is the run's, in its database, with what was searched for
        assert run.db.get_meta("searches") == 3 and searches.SearchGate(run).used == 3
        said = [json.loads(e["detail"])["q"] for e in run.db.all("SELECT detail FROM events WHERE kind = 'search'")]
        assert said == ["truck 1", "truck 2", "truck 3"]
        # the marks passed are given once
        assert gate.crossed() == 100 and gate.crossed() is None
    finally:
        await gate.close()


async def test_a_search_that_got_no_answer_is_not_counted(make_run, monkeypatch):
    run, _ = make_run({}, max_searches=3)
    monkeypatch.setattr(searches, "UPSTREAM", "http://127.0.0.1:9/search")      # nothing listens there
    gate = searches.SearchGate(run)
    await gate.start()
    try:
        code, data, _ = await get(gate, "/search?q=x&format=json")
        assert code == 502 and "did not answer" in data["error"]
        assert gate.used == 0 and gate.crossed() is None
    finally:
        await gate.close()


async def test_no_limit_counts_and_never_refuses(make_run, searxng):
    run, _ = make_run({}, max_searches=0)
    assert "web searches in all" not in prompts.system_prompt(run.cfg)
    gate = searches.SearchGate(run)
    await gate.start()
    try:
        for _ in range(4):
            assert (await get(gate, "/search?q=x&format=json"))[0] == 200
        assert gate.used == 4 and gate.crossed() is None
    finally:
        await gate.close()
    # a run stored before searches had a limit keeps none when it resumes
    old = {k: v for k, v in run.cfg.to_dict().items() if k != "max_searches"}
    assert config.RunConfig.from_dict(old).max_searches == 0 and config.RunConfig(run_id="x").max_searches == 100


async def test_a_run_is_told_as_its_searches_run_out_and_then_refused(make_run, searxng):
    first = ('search --left; search "blocked maker"; for i in 1 2 3 4; do search "maker $i" | tail -n 1; done; sleep 0.5')
    second = ('search "maker 5"; search "maker 6"; echo "exit $?"; '
              "curl -s -o /dev/null -w 'direct %{http_code}\\n' 'http://127.0.0.1:8888/search?q=x&format=json'; "
              "search --json 'maker 7' 2>/dev/null; search --left; sleep 0.5")
    run, fake = make_run({
        "lead": lead_script(GOAL | {"collections": COLLECTIONS, "tasks": [task("find")]}),
        "find": [[bash(first)], [bash(second)], [bash("echo still here")],
                 [call("report", summary="Looked up the makers.", how="Searched.")]],
    }, max_searches=5)
    assert "- The run has 5 web searches in all, shared by everyone." in prompts.system_prompt(run.cfg)
    assert await orchestrator.execute(run) == "done"

    saw = "\n".join(fake.seen["find"])
    # every search says where the count stands, and warns from 80%
    assert "[search 0 of 5 for the whole run, 5 left]" in saw
    assert "[search 3 of 5 for the whole run, 2 left]" in saw
    assert "[search 4 of 5 for the whole run, 1 left: keep them for what can't be found another way]" in saw
    assert "https://example.com/a" in saw and "[search 5 of 5 for the whole run, 0 left: `search` refuses from now on]" in saw
    # past the limit: refused, whichever way the search is made
    assert "search refused: the run's 5 web searches are used up" in saw and "exit 2" in saw and "direct 429" in saw
    # a search the engines did not answer is said to be that, and is not counted
    assert "no results, because the search engines did not answer (google cse: too many requests)" in saw
    assert len(searxng) == 6 and run.db.get_meta("searches") == 5
    # the worker and the lead are told at the marks; a reviewer is not
    assert sum("Web searches: 80% used (4 of 5 for the whole run, shared by everyone)" in t for t in fake.seen["find"]) == 1
    assert sum("The run's 5 web searches are used up" in t for t in fake.seen["find"]) == 1
    lead_saw = "\n".join(fake.seen["lead"])
    assert "Web searches: 80% used (4 of 5" in lead_saw and "The run's 5 web searches are used up" in lead_saw
    assert not any("Web searches" in t or "searches are used up" in t for t in fake.seen["find.review1"])
    assert "Web searches 5 of 5" in status.render("t")
