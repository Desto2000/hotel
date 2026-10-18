"""MCP server — the lobby as tools for any MCP client (Claude, Amp, Cursor, …).

The client is a model, so it gets the guest's rights and nothing more:
  * read: directory, lobby search, entries, the review queue;
  * retrieve: web search / fetch through the gateway, every call logged;
  * annotate: required after each retrieval — until then every other tool is
    refused (the gate is global, so an unannotated MCP search also blocks the TUI);
  * propose: edits are dry-run against the validator and parked in review.
    Only the user applies them (`/approve R:n`, `hotel review approve n`).

Transports: stdio (the client launches `hotel mcp`) or streamable HTTP
(`hotel mcp --http`) for remote clients. HTTP off loopback requires a bearer
token (`HOTEL_MCP_TOKEN` or `--token`).
"""

from __future__ import annotations

import hmac
import os
import threading
from typing import Literal

from pydantic import BaseModel, Field

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .agent import model_annotate
from .gateway import GateBlocked
from .guest import draft_ops
from .search import clean_queries

INSTRUCTIONS = """hotel is the user's personal notes harness. The lobby is one living plain-text file: entries like L0007 live in rooms (topics); types are monologue, bounty (open question), goal, reasoning, idea, log; status [ ] open, [x] closed, [~] superseded, [!] contested, [.] static.

Rules (enforced by the server, not optional):
- Every call is logged in the user's append-only Reg.
- After web_search or fetch you must call annotate on that retrieval R:<n> before any other tool works. Quote contiguous source text; 'supports'/'contradicts' need lobby_refs. A useless retrieval: claims=[] and caused="nothing: <why>".
- You cannot write the lobby. propose drafts edits; the user approves them. Propose sparingly, only what is worth keeping, and never copies of what the user already wrote.
- Cite lobby entries by id (L0007) and web evidence by retrieval id [R:12]."""


class ClaimIn(BaseModel):
    text: str = Field(description="the claim in your words")
    quote: str = Field(description="exact supporting text copied from the retrieval")
    relation: Literal["supports", "contradicts", "new", "unrelated"]
    lobby_refs: list[str] = Field(default_factory=list, description="lobby ids, e.g. L0005")


def build_server(hotel) -> MCPServer:
    """One server = one Hotel session (label 'mcp'). Tool bodies run in worker threads;
    a lock serializes them so the gate check and the call it guards stay atomic."""
    mcp = MCPServer("hotel", instructions=INSTRUCTIONS, version=__version__)
    gw = hotel.gateway
    lock = threading.Lock()
    thread = gw.log("internal", "mcp", tool="mcp.thread").id
    seen_q: list[set[str]] = []

    def gate(what: str) -> None:
        try:
            gw._gate(what, "mcp", "internal", force=True)
        except GateBlocked as ex:
            raise ToolError(f"{ex}. Call annotate on it now.") from None

    def directory() -> str:
        p = hotel.paths.directory
        return p.read_text(encoding="utf-8") if p.exists() else "(empty)"

    @mcp.resource("hotel://lobby", mime_type="text/plain", description="The full lobby text.")
    def lobby_text() -> str:
        return hotel.text()

    @mcp.resource("hotel://directory", mime_type="text/plain", description="Open lobby entries by room.")
    def lobby_directory_resource() -> str:
        return directory()

    @mcp.tool(description="Open lobby entries grouped by room. Start here.")
    def lobby_directory() -> str:
        with lock:
            gate("lobby_directory")
            return directory()

    @mcp.tool(description="Full-text search over the user's lobby (their own notes, not external evidence).")
    def lobby_search(query: str) -> str:
        with lock:
            try:
                _, res = gw.call("lobby.search", actor="mcp", gate_all=True, query=query)
            except GateBlocked as ex:
                raise ToolError(f"{ex}. Call annotate on it now.") from None
            return res.text

    @mcp.tool(description="Read lobby entries by id (e.g. ['L0007']), with their metadata lines.")
    def read_entries(ids: list[str]) -> str:
        with lock:
            gate("read_entries")
            lob = hotel.lobby()
            blocks = ["\n".join(lob.lines[e.start:e.end + 1]) for i in ids if (e := lob.entries.get(i))]
            return "\n\n".join(blocks) or "no such entries"

    @mcp.tool(description="Web (+ imported RAG) search with 1-3 distinct queries. "
                          "Returns retrieval R:<n>; annotate it before any other call.")
    def web_search(queries: list[str]) -> str:
        with lock:
            gate("web_search")
            seen = list(seen_q)  # a query only counts as asked once its search succeeded
            qs, rejected = clean_queries(queries, seen, 3)
            if not qs:
                raise ToolError("no usable queries: " + "; ".join(rejected))
            try:
                e, res = gw.call("agent.search", actor="mcp", gate_all=True, queries=qs,
                                 objective=" | ".join(qs), skip_urls=[])
            except Exception as ex:
                raise ToolError(f"search failed: {type(ex).__name__}: {str(ex)[:300]}") from None
            seen_q[:] = seen
            return f"RETRIEVAL R:{e.id} — annotate it next.\n\n{res.text}"

    @mcp.tool(description="Fetch the full text of one URL. Returns retrieval R:<n>; annotate it next.")
    def fetch(url: str) -> str:
        with lock:
            try:
                e, res = gw.call("exa.fetch", actor="mcp", gate_all=True, url=url)
            except GateBlocked as ex:
                raise ToolError(f"{ex}. Call annotate on it now.") from None
            except Exception as ex:
                raise ToolError(f"fetch failed: {type(ex).__name__}: {str(ex)[:300]}") from None
            return f"RETRIEVAL R:{e.id} — annotate it next.\n\n{res.text}"

    @mcp.tool(description="Record what retrieval R:<target> says. Required after every web_search/fetch. "
                          "Quotes are checked against the stored text; rejections say how to fix them.")
    def annotate(target: int, claims: list[ClaimIn], caused: str, caused_refs: list[str] | None = None) -> str:
        with lock:
            args = {"target": target, "claims": [c.model_dump() for c in claims], "caused": caused,
                    "caused_refs": caused_refs or []}
            try:
                msg, row, _ = model_annotate(hotel, args, "mcp")
            except Exception as ex:
                raise ToolError(f"annotation rejected: {str(ex)[:400]}") from None
            if row is None:
                raise ToolError(msg)
            return msg

    @mcp.tool(description="Draft lobby edits for the user's approval (they land in the review queue, never "
                          "directly in the lobby). ops use the edit-op schema: append {op,room,type,body,refs,tags}; "
                          "supersede {op,id,type,body,reason}; tag {op,id,tag}; link {op,id,target}; "
                          "ref {op,id,ref}; move {op,id,room,reason}; contest/reopen {op,id,reason}.")
    def propose(ops: list[dict], why: str) -> str:
        with lock:
            gate("propose")
            ids, msg = draft_ops(hotel, ops, why, "mcp", thread)
            if not ids and "rejected" in msg:
                raise ToolError(msg)
            return msg

    @mcp.tool(description="Drafts waiting for the user's decision (from any agent).")
    def review_queue() -> str:
        with lock:
            rows = hotel.reg.pending_reviews()
            return "\n".join(f"{r.rid} [{r.actor}] {r.payload.get('reason', '')}: {r.payload.get('op')}"
                             for r in rows) or "(empty)"

    return mcp


class BearerAuth:
    """ASGI wrapper: every HTTP request needs `Authorization: Bearer <token>`,
    except GET /healthz (for host health checks). Lifespan passes through."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope["path"] == "/healthz":
                return await _plain(send, 200, b"ok")
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if not (auth.startswith(b"Bearer ") and hmac.compare_digest(auth[7:].strip(), self.token)):
                return await _plain(send, 401, b"unauthorized", [(b"www-authenticate", b"Bearer")])
        await self.app(scope, receive, send)


async def _plain(send, status: int, body: bytes, headers: list | None = None) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain"), *(headers or [])]})
    await send({"type": "http.response.body", "body": body})


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def http_app(hotel, host: str, token: str | None):
    if not token and host not in LOOPBACK:
        raise SystemExit("refusing to serve the lobby off loopback without a token: "
                         "set HOTEL_MCP_TOKEN or pass --token")
    mcp = build_server(hotel)
    if not token:  # loopback only: the SDK's DNS-rebinding protection (Host/Origin checks) applies
        return mcp.streamable_http_app(host=host)
    from mcp.server.transport_security import TransportSecuritySettings
    # Behind a tunnel or proxy the Host header is the public name; the token is the check.
    app = mcp.streamable_http_app(host=host, transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False))
    return BearerAuth(app, token)


def serve(hotel, http: bool = False, host: str = "127.0.0.1", port: int = 8765, token: str | None = None) -> None:
    if not http:
        build_server(hotel).run()  # stdio
        return
    import uvicorn
    token = token or os.environ.get("HOTEL_MCP_TOKEN") or None
    uvicorn.run(http_app(hotel, host, token), host=host, port=port, log_level="warning")
