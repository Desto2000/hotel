"""MCP server: a client model gets the guest's rights — gated retrieval, verified
annotations, drafts that only the user can apply."""

import anyio
import pytest
from mcp.client import Client
from starlette.testclient import TestClient

from hotel.config import Paths
from hotel.core import Hotel, init_home
from hotel.gateway import ToolResult, ToolSpec
from hotel.mcp_server import build_server, http_app

DOC = "QUERY: q\n\n### [1] fake:1 (external) Doc\n\nExa supports a startPublishedDate filter on search.\n"


@pytest.fixture
def hotel(tmp_path):
    init_home(tmp_path / "home")
    h = Hotel(Paths(tmp_path / "home"), new_session=True, label="mcp")
    h.gateway.register(ToolSpec("agent.search", "retrieval", "fake", lambda **_: ToolResult(b"{}", DOC, [], "fake")))
    return h


def test_gate_annotation_and_drafts_through_mcp(hotel):
    from hotel.models import AppendOp
    bounty = hotel.apply([AppendOp(op="append", room="exa", type="bounty", body="Date filter on Exa?",
                                   refs=[], tags=[])]).created[0]
    before = hotel.text()

    async def run():
        async with Client(build_server(hotel)) as c:
            async def call(name, **args):
                r = await c.call_tool(name, args)
                return r.is_error, r.content[0].text

            err, out = await call("web_search", queries=["exa published date filter"])
            assert not err and out.startswith("RETRIEVAL R:")
            rid = int(out.split()[1][2:])

            # gate: nothing but annotate works until R:rid is annotated
            for name, args in [("lobby_search", {"query": "exa"}), ("read_entries", {"ids": [bounty]}),
                               ("propose", {"ops": [], "why": "x"}),
                               ("web_search", {"queries": ["exa crawl date range options"]})]:
                err, out = await call(name, **args)
                assert err and f"R:{rid}" in out, name

            claim = {"text": "Exa can filter by date", "relation": "supports", "lobby_refs": [bounty]}
            err, out = await call("annotate", target=rid, caused="answers", caused_refs=[bounty],
                                  claims=[{**claim, "quote": "Exa supports an endPublishedDate filter"}])
            assert err and "rejected" in out
            err, out = await call("annotate", target=rid, caused="answers", caused_refs=[bounty],
                                  claims=[{**claim, "quote": "supports a startPublishedDate filter"}])
            assert not err, out
            # the query refused by the gate was not burnt: it runs now
            err, out = await call("web_search", queries=["exa crawl date range options"])
            assert not err and out.startswith("RETRIEVAL R:")
            rid2 = int(out.split()[1][2:])
            err, out = await call("annotate", target=rid2, caused="nothing: same doc", claims=[])
            assert not err, out

            err, out = await call("propose", why="worth keeping", ops=[
                {"op": "append", "room": "exa", "type": "idea", "body": "Use startPublishedDate.",
                 "refs": [f"R:{rid}"], "tags": []}])
            assert not err and "R:" in out
            err, again = await call("propose", why="dup", ops=[
                {"op": "append", "room": "exa", "type": "idea", "body": "Use startPublishedDate.",
                 "refs": [f"R:{rid}"], "tags": []}])
            assert err and "already queued" in again

            # reads are Reg rows too, resources included
            await c.read_resource("hotel://lobby")
            await call("read_entries", ids=[bounty])
            tools = {r.tool for r in hotel.reg.list(session_id=hotel.session_id, kind="internal")}
            assert {"mcp.resource:lobby", "mcp.read_entries"} <= tools
            return int(out.rsplit("R:", 1)[1])

    review = anyio.run(run)
    assert hotel.text() == before  # the client never writes the lobby
    assert [r.id for r in hotel.reg.pending_reviews()] == [review]
    assert hotel.reg.get(review).actor == "mcp"
    assert hotel.decide(review, approve=True).route == "applied"
    assert "origin=approved" in hotel.text().split("Use startPublishedDate.")[0].rsplit("@L", 1)[1]


def test_http_requires_token_off_loopback(hotel):
    with pytest.raises(SystemExit):
        http_app(hotel, "0.0.0.0", None)
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    hdrs = {"accept": "application/json, text/event-stream", "host": "lobby.example.com"}
    with TestClient(http_app(hotel, "0.0.0.0", "s3cret"), base_url="https://lobby.example.com") as tc:
        assert tc.get("/healthz").status_code == 200
        assert tc.post("/mcp", json=init, headers=hdrs).status_code == 401
        assert tc.post("/mcp", json=init, headers={**hdrs, "authorization": "Bearer wrong"}).status_code == 401
        ok = tc.post("/mcp", json=init, headers={**hdrs, "authorization": "Bearer s3cret"})
        assert ok.status_code == 200 and "serverInfo" in ok.text
