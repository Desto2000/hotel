# hotel

A personal information harness that replaces "chat with AI" as note-taking. It is not a chat
product. Everything goes through a logged **Reg**. The durable output is one living plain-text
document, the **lobby**.

## Install (system-wide, reproducible)

```
git clone <this repo> hotel && cd hotel
scripts/install.sh  [~/lobby]          # Git Bash / macOS / Linux
scripts\install.ps1 [-LobbyDir ~\lobby] # PowerShell
```

What makes it reproducible: `.python-version` pins the interpreter (uv downloads it),
`constraints.txt` pins every dependency (exported from `uv.lock`; regenerate with
`uv export --frozen --no-hashes --no-emit-project --no-dev -o constraints.txt`), and the CLI is
installed as an isolated `uv tool`, so `hotel` is on PATH and independent of any project venv.
The lobby itself is reproducible from `hotel backup` (below). After pulling new code, rerun the
install script; the tool is a pinned copy, not a live checkout.

`hotel` finds its home in this order: `--home`, `HOTEL_HOME`, the nearest parent directory
containing `.hotel/`, then the per-user default in `~/.config/hotel/home` (set by `hotel init`
or `hotel home <dir>`). So `hotel` works from any directory. `hotel doctor` checks the whole chain.

Development:

```
uv sync && uv run pytest -q
```

### API keys

Run `hotel setup` (also offered at the end of `hotel init`). It walks through each provider, tells
you where to get a key, reads it hidden, checks it against the provider, and stores it in
`~/.config/hotel/keys.json` (outside the lobby and its backups). A key rejected with 401/403 is
refused and re-asked; network trouble or provider errors save the key with a note. For Mercury it
also asks you to confirm the training opt-out (see Data safety) and records that in the home config.

| key | used for |
|---|---|
| `EXA_API_KEY` (alias `MCP_EXA_API_KEY`) | web search (required) |
| `INCEPTION_API_KEY` | Mercury, the default model profile (required) |
| `PARALLEL_API_KEY` | optional search backend |
| `GROQ_API_KEY` (alias `HERMES_CUSTOM_GROQ_API_KEY`) | optional `groq` profile |

Environment variables always win over stored keys. Scripted use: `echo $KEY | hotel keys set EXA_API_KEY`,
`hotel keys list` (never prints values), `hotel keys rm <ENV>`; `hotel setup --only EXA_API_KEY` for one key,
`--no-check` to skip the validation request (the Exa check is one 1-result search). `hotel doctor`
reports where each key comes from. Custom `llm_profiles` with their own `key_env` read the same store.

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
| sqlite-vec embeddings, talk/solve agents, concierge, custodian dedup, hotel renderer | **not built** | — |
| Guest agent (drafts → review → origin=approved), custodian staleness, move op, forget, backup/restore, verifier, usage logging, system install | done (v0.2) | `guest.py`, `core.py`, `backup.py`, `verify.py`, `scripts/` |
| MCP server: the lobby as tools for Claude / Amp / Cursor (guest rights: gated retrieval, drafts → review); stdio + streamable HTTP with bearer token | done | `mcp_server.py` |

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

## Guest mode: the note-taking loop

`/guest` in the TUI (or `hotel guest "…" --note`). Every line you type is appended to the lobby as
your monologue (origin=user), so your side of the conversation is the note. The guest replies in
the Guest tab. It can read the lobby, search the web (gated, same rules as every agent), and
`propose` edits: an idea, a bounty, a supersede, a tag, a move. Each proposal is dry-run against
the validator, deduplicated, and parked in Review; `/approve R:n` writes it with origin=approved.
The guest's reply itself never enters the lobby. Start a line with `>` to talk without writing it
down ("> yes, draft it"). Messages queue: a second one waits for the first turn to finish.

## MCP: the lobby inside other AI clients

`hotel mcp` serves the lobby over the Model Context Protocol, so Claude (Desktop, Code), Amp,
Cursor or any MCP client can read and research in it. The client is a model, so it gets the
guest's rights, enforced by the server:

- tools: `lobby_directory`, `lobby_search`, `read_entries`, `web_search`, `fetch`, `annotate`,
  `propose`, `review_queue`; resources `hotel://lobby` and `hotel://directory`
- every call is a Reg row in its own `mcp` session; after `web_search`/`fetch` every other tool
  is refused until the client annotates (the gate is global, so the TUI waits for it too)
- the client never writes the lobby: `propose` dry-runs ops against the validator and parks
  them in Review, where you `/approve R:n` (origin=approved)

Local clients launch it over stdio:

```
claude mcp add hotel -- hotel mcp                         # Claude Code
"amp.mcpServers": {"hotel": {"command": "hotel", "args": ["mcp"]}}   # Amp settings.json
{"mcpServers": {"hotel": {"command": "hotel", "args": ["mcp"]}}}     # Claude Desktop / Cursor
```

Remote clients use streamable HTTP at `/mcp` (`GET /healthz` is the unauthenticated health check):

```
HOTEL_MCP_TOKEN=$(openssl rand -hex 32) hotel mcp --http --host 0.0.0.0 --port 8765
claude mcp add --transport http hotel https://<host>/mcp --header "Authorization: Bearer $HOTEL_MCP_TOKEN"
"amp.mcpServers": {"hotel": {"url": "https://<host>/mcp", "headers": {"Authorization": "Bearer …"}}}
```

Without a token it binds to loopback only (with the SDK's DNS-rebinding checks); off loopback
it refuses to start without one.

### Hosting

The lobby is a directory (git repo + SQLite + blobs) behind a cross-process lock, so host
`hotel mcp --http` on the one machine that holds that directory. It needs a persistent disk and
`git`; serverless platforms (Lambda, Vercel, Workers) don't fit.

- **Your own machine + a tunnel** (recommended: the lobby stays where the TUI is).
  Tailscale: `tailscale serve --bg 8765` gives HTTPS inside your tailnet only, or
  `tailscale funnel --bg 8765` for a public URL. Cloudflare: `cloudflared tunnel --url
  http://localhost:8765` (quick) or a named tunnel with Cloudflare Access in front. Run it
  bound to `127.0.0.1` with a token; the tunnel supplies TLS.
- **A small always-on box** (VPS, Fly.io/Railway with a volume, a home server): run
  `scripts/install.sh /data/lobby`, then `hotel mcp --http --host 0.0.0.0` under systemd
  behind Caddy/nginx for TLS. The lobby then lives there: use the TUI over `ssh`, and point
  `hotel backup` at a second disk.

Whatever the client is, its model sees the lobby text it reads, and its provider's data
policy applies to that.

## Data safety

- **Concurrent writers:** every lobby write holds a cross-process lock for read → validate →
  write → commit. Uncommitted editor changes are validated and committed before the write. Tested
  with two OS processes writing 30 entries at the same time.
- **Backup:** `hotel backup [dir]` (default `~/hotel-backups`) is incremental. Blobs are copied
  once (content-addressed); each snapshot holds SQLite online-backup copies of the Reg and RAG, a
  `git bundle` of the lobby's full history, and a manifest. `hotel restore <dir> <empty-target>`
  verifies the hash chain, every referenced blob and the lobby before reporting success.
- **Forgetting:** `hotel forget R:n --reason …` deletes the content behind a row: result, text,
  arguments (queries and prompts), and the annotation quotes. The rows and hash chain stay; only
  the hash-addressed bytes are gone. Not covered: lobby text (it lives in git history; use
  `git filter-repo`), rows written before v0.2 (they held args and annotations inline), and
  existing backups (prune them).

## Lifecycle

- **Staleness:** closing a bounty adds `+ revalidate: <date>` (`bounty_revalidate_days`, default
  90). `hotel custodian` / `/custodian` tags stale bounties and logs that they need rechecking.
  It never re-answers; you re-run `/research` and supersede.
- **Rooms drift:** `/move L0004 embeddings [why]` copies a live entry (same type, origin, body,
  tags, refs) into the new room with `moved-from`, and supersedes the original. Closed or contested
  history stays where it happened. Resident moves go to review.

## Model checks and cost

- **Entailment verifier:** set `"verifier": "<llm profile>"` in `.hotel/config.json`. Every
  model-written claim is then judged against its own quote (entailed / overreach / unsupported)
  before the annotation is accepted; overreach goes back to the agent with the reason. Use a
  different model from the agent: a model grading itself shares its blind spots.
- **Usage:** every model row records `tokens_in`, `tokens_out` and `ms`; with
  `"llm_profiles": {"mercury": {"price": {"in": …, "out": …}}}` (USD per 1M tokens) it also records
  `cost_usd`. No prices are built in; set the ones you pay.

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
/guest [new|off] guest mode               > line = talk only   /talk <msg>
/move L room     /custodian               /backup [dir]
/lib [R:n]       (ctrl+l) run librarian    /approve R:n   /reject R:n
/blob R:n        /check   /verify   /theme (ctrl+t)
```

The theme is read at runtime from `hotel/themes/calculus.json`, i.e. your Zed theme file
unchanged. UI roles map to Zed keys, and lobby syntax maps to Zed syntax scopes.

## CLI

`hotel add|search|annotate <json>|lib <rid>|research <L>|guest "<msg>" [--note]|accept <rid>|move <L> <room>|custodian|backup [dir]|restore <src> <dst>|forget <rid> --reason|home [dir]|setup|keys list\|set\|rm|doctor|version|timings|render|snapshot|review list|approve|reject|check|verify|rebuild|reg|replay <session>|rag-import|sessions|schema|mcp [--http --host --port --token]`
