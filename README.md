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

Env: `EXA_API_KEY` (falls back to `MCP_EXA_API_KEY`), `INCEPTION_API_KEY` (Mercury, the default model
profile), optionally `PARALLEL_API_KEY`, or `GROQ_API_KEY` for the `groq` profile.

## What is built (spec build order)

| layer | status | where |
|---|---|---|
| Reg: append-only SQLite, triggers block UPDATE/DELETE, sha256 hash chain, blobs by hash | done | `reg.py`, `blobs.py` |
| Gateway: wraps every tool, logs before returning, failures + blocked calls logged | done | `gateway.py` |
| Annotation gate: global (a new session can't escape it); pointers re-verified against the stored text blob | done | `gateway.py` |
| Exa search/fetch | done | `search.py` |
| Lobby grammar + parser + validator (static + old→new transition) | done | `grammar.py`, `validator.py` |
| Edit ops as Pydantic union, pure text application, git commit per edit | done | `models.py`, `ops.py`, `core.py` |
| Librarian interface: `RuleLibrarian` (deterministic) + `ModelLibrarian` (any router profile, json_schema + one repair turn) | done (Mercury untested: no key; same code path tested via scripted transport) | `librarian.py` |
| Model router: OpenAI-compatible profiles (mercury default, groq), `reasoning_effort`, retry/Retry-After, training opt-out guard | done | `llm.py` |
| Research agent (chat agent): hand-written tool-calling loop, one bounty per run, never writes the lobby | done, live-tested on Exa + groq | `agent.py` |
| Resident policy: log-only appends, rate cap, deterministic bounty close, review queue | done | `core.py` |
| Unified search: lobby (FTS5) + RAG (FTS5) + Exa → one gated retrieval, normalized hits | done | `search.py` |
| Parallel search backend (agent fan-out) | written to the Inception cookbook's shape, untested (no key) | `search.py` |
| Feedback-loop tooling: `render`, headless `snapshot`, `timings`, scripted model transport | done | `snapshot.py`, `cli.py`, `llm.py` |
| RAG with one-way refs, logged promotion with backlink | done | `rag.py`, `PromoteOp` |
| Replay harness: re-run a session with another librarian, diff lobbies | done | `replay.py` |
| Directory layer (regenerated, open entries only) | done | `derived.py` |
| sqlite-vec embeddings, talk/solve agents, guests, concierge/custodian, hotel renderer | **not built** | — |

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

## Research agent

`/research L0005` (or `hotel research L0005`) runs one task: answer that bounty from web
evidence. The loop is an LLM, tools and a loop, no framework:

- tools: `search` (1–3 queries, fanned out in parallel, de-duplicated, small highlights),
  `fetch`, `lobby_search`, `annotate`
- every tool call goes through the gateway with the gate on; while a retrieval is
  unannotated the model is offered **only** `annotate`, forced via `tool_choice`
- rejections (bad quote, gate, provider `tool_use_failed`) come back as tool results with
  a concrete fix, e.g. *"the first 12 of 20 words match, then the source continues: …"*
- citations are Reg ids (`[R:67]`); the final answer is a gated `output` row
- over budget, old annotated retrievals are replaced by their annotation (the distilled form)
- after the run the librarian files refs; closing the bounty goes to **review**, because a
  model wrote the annotation. `/accept R:n` keeps the answer as `origin=approved` with backlinks.

## Verifying changes

```
uv run pytest -q                         # offline; agent loop driven by ScriptedTransport
hotel render --plain | --directory       # lobby as text
hotel snapshot --cmd "/bounty x?" --svg out.svg   # headless TUI state as text (+ pixels)
hotel timings                            # model vs tool latency per agent run
hotel replay <session> --librarian rule  # re-run a session with another librarian, diff
```

## TUI

Plain text appends a monologue to the current room. Commands:

```
/room <slug>     /bounty /goal /idea /reason /log <text>
/s <q>           unified search (gated)   /exa <q>   /fetch <url>   /ls <q> lobby only (ungated)
/annotate        (ctrl+a / F2) claim + quote → pointer computed and checked against the blob
/close L R:n     /supersede L <text>   /contest L <why>   /reopen L <why>
/tag L t         /link L L   /ref L R:n   /promote L
/research L      run the research agent    /accept R:n keep its answer
/lib [R:n]       (ctrl+l) run librarian    /approve R:n   /reject R:n
/blob R:n        /check   /verify   /theme (ctrl+t)
```

The theme is read at runtime from `hotel/themes/calculus.json`, i.e. your Zed theme file
unchanged. UI roles map to Zed keys, and lobby syntax maps to Zed syntax scopes.

## CLI

`hotel add|search|annotate <json>|lib <rid>|research <L>|accept <rid>|timings|render|snapshot|review list|approve|reject|check|verify|rebuild|reg|replay <session>|rag-import|sessions|schema`
