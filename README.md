# hotel

A personal information harness that replaces "chat with AI" as note-taking. It is not a chat
product. Everything goes through a logged **Reg**. The durable output is one living plain-text
document, the **lobby**.

```
uv sync
uv run hotel init ~/lobby          # creates lobby.txt (git) + .hotel/ (Reg, blobs, indexes)
export HOTEL_HOME=~/lobby
uv run hotel                       # TUI (Calculus theme, ctrl+t toggles dark/light)
uv run pytest -q
```

Env: `EXA_API_KEY` (falls back to `MCP_EXA_API_KEY`), `INCEPTION_API_KEY` (Mercury librarian only).

## What is built (spec build order)

| layer | status | where |
|---|---|---|
| Reg: append-only SQLite, triggers block UPDATE/DELETE, sha256 hash chain, blobs by hash | done | `reg.py`, `blobs.py` |
| Gateway: wraps every tool, logs before returning, failures + blocked calls logged | done | `gateway.py` |
| Annotation gate: global (a new session can't escape it); pointers re-verified against the stored text blob | done | `gateway.py` |
| Exa search/fetch | done | `search.py` |
| Lobby grammar + parser + validator (static + old→new transition) | done | `grammar.py`, `validator.py` |
| Edit ops as Pydantic union, pure text application, git commit per edit | done | `models.py`, `ops.py`, `core.py` |
| Librarian interface: `RuleLibrarian` (deterministic) + `MercuryLibrarian` (mercury-2.5, json_schema) | done (Mercury untested: no key) | `librarian.py` |
| Resident policy: log-only appends, rate cap, deterministic bounty close, review queue | done | `core.py` |
| Unified search: lobby (FTS5) + RAG (FTS5) + Exa → one gated retrieval, normalized hits | done | `search.py` |
| RAG with one-way refs, logged promotion with backlink | done | `rag.py`, `PromoteOp` |
| Replay harness: re-run a session with another librarian, diff lobbies | done | `replay.py` |
| Directory layer (regenerated, open entries only) | done | `derived.py` |
| Parallel provider, sqlite-vec embeddings, guests/chat agents, concierge/custodian, hotel renderer | **not built** | — |

## Lobby grammar

```
#! hotel-lobby v1

== ROOM: harness ==

@L0001 [x] bounty origin=user ts=2026-10-18T21:13:29Z
  | Does Exa's search API support filtering by published date?
  + ref: R:4
  + closed-by: R:4
```

- sigils: `[ ]` open · `[x]` closed · `[~]` superseded · `[!]` contested · `[.]` static
- `bounty|goal|idea` live in `{ ,x,~,!}`; `monologue|reasoning|log` live in `{.,~}`
- origin: `user` · `approved` (user-approved model draft) · `resident`
- meta keys: `tag ref link supersedes superseded-by closed-by promoted note`
- transitions: `' '→x|~|!`, `!→' '|x|~`, `x→~`, `.→~`, `~` is terminal

The validator rejects body edits, deletions, id/room/type/origin/ts changes, removed or altered
meta lines, insertion anywhere except the end of a room, illegal status moves, one-sided
supersession, a bounty closed without an annotated `R:<n>`, and resident entries of any type
other than `log`. Hand edits to `lobby.txt` are allowed. They are validated against git HEAD
when the TUI starts (and by `hotel check`), then committed as user edits.

## TUI

Plain text appends a monologue to the current room. Commands:

```
/room <slug>     /bounty /goal /idea /reason /log <text>
/s <q>           unified search (gated)   /exa <q>   /fetch <url>   /ls <q> lobby only (ungated)
/annotate        (ctrl+a / F2) claim + quote → pointer computed and checked against the blob
/close L R:n     /supersede L <text>   /contest L <why>   /reopen L <why>
/tag L t         /link L L   /ref L R:n   /promote L
/lib [R:n]       (ctrl+l) run librarian    /approve R:n   /reject R:n
/blob R:n        /check   /verify   /theme (ctrl+t)
```

The theme is read at runtime from `hotel/themes/calculus.json`, i.e. your Zed theme file
unchanged. UI roles map to Zed keys, and lobby syntax maps to Zed syntax scopes.

## CLI

`hotel add|search|annotate <json>|lib <rid>|review list|approve|reject|check|verify|rebuild|reg|replay <session>|rag-import|sessions|schema`
