# Hotel

Hotel is a command-line and terminal-UI tool for research notes. Your notes live in one plain-text
document (the *lobby*). Every tool call is recorded in an append-only log (the *Reg*). When Hotel gets
a result from the web, you must annotate it with a quote before you can use it.

You need Python 3.12 or later and `git`.

New to Hotel? Start with the [tutorial](TUTORIAL.md). It walks through one full session in about 15 minutes.

## Contents

- [Concepts](#concepts)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Lobby grammar](#lobby-grammar)
- [Agents](#agents)
- [RAG and the Concierge](#rag-and-the-concierge)
- [MCP server](#mcp-server)
- [Data safety](#data-safety)
- [Lifecycle and model checks](#lifecycle-and-model-checks)
- [Development](#development)
- [Status](#status)

## Concepts

| Term | Meaning |
|---|---|
| **Lobby** | A single text document (`lobby.txt`) of typed entries grouped into rooms, versioned in git. |
| **Reg** | Append-only SQLite log of every tool call. Triggers block `UPDATE`/`DELETE`; rows form a sha256 hash chain; large payloads are stored as blobs addressed by hash. |
| **Gateway** | Wraps every tool. It logs each call before returning, including failures and blocked calls. |
| **Annotation gate** | After a search or fetch, other tools are refused until the result is annotated with a claim and a quote. Pointers are re-verified against the stored text. The gate is global, so starting a new session does not bypass it. |
| **Librarian** | Files references between entries. `RuleLibrarian` is deterministic; `ModelLibrarian` uses any configured model profile. |

## Installation

```
git clone https://github.com/Desto2000/hotel.git && cd hotel
scripts/install.sh [~/lobby]              # Git Bash / macOS / Linux
./scripts\install.ps1 [-LobbyDir ~\lobby] # PowerShell
```

The installed tool is a pinned copy, not a live checkout. Re-run the install script after pulling new code.

`hotel` locates its home in this order:

1. `--home`
2. `HOTEL_HOME`
3. the nearest parent directory containing `.hotel/`
4. the per-user default in `~/.config/hotel/home` (set by `hotel init` or `hotel home <dir>`)

`hotel doctor` checks this chain and reports where each API key comes from.

## Configuration

### API keys

Just run `hotel`. If no lobby exists yet, it offers to create one at `~/lobby`. The first time the TUI
opens it walks you through onboarding: welcome, each API key (checked live, with a link to the provider's
sign-up page; `ctrl+o` opens it), the Mercury training opt-out, a theme preview, and a three-page tour of
the basics. `/setup` reopens it, `/tutorial` replays just the tour, and `hotel setup` sets the keys on the
command line. Each key is read hidden, checked against its provider, and stored in
`~/.config/hotel/keys.json`, which is outside the lobby and its backups. A key rejected with 401/403 is
refused and re-requested; on network or provider errors the key is saved with a note.

| Variable | Used for |
|---|---|
| `EXA_API_KEY` (alias `MCP_EXA_API_KEY`) | Web search (required) |
| `INCEPTION_API_KEY` | Mercury, the default model profile (required) |
| `PARALLEL_API_KEY` | Optional; web searches then query Exa and Parallel together (`search_backend`) |
| `GROQ_API_KEY` (alias `HERMES_CUSTOM_GROQ_API_KEY`) | Optional `groq` profile |

Environment variables take precedence over stored keys. Custom `llm_profiles` with their own `key_env`
read from the same store.

Scripted use:

```
echo $KEY | hotel keys set EXA_API_KEY
hotel keys list                    # never prints values
hotel keys rm <ENV>
hotel setup --only EXA_API_KEY     # configure one key
hotel setup --no-check             # skip the validation request
```

### Project config

`.hotel/config.json` in the lobby holds options such as `verifier`, `llm_profiles` (including optional
per-profile `price`), and `bounty_revalidate_days`.

## Usage

In the TUI, plain text is appended to the current room as a monologue. Commands:

```
/room <slug>     /bounty /goal /idea /reason /log <text>
/s <q>           unified search (gated)   /exa <q>   /deep <q>   /fetch <url>   /ls <q> lobby only (ungated)
/annotate        (ctrl+a / F2) claim + quote; pointer is computed and checked against the blob
/close L R:n     /supersede L <text>   /contest L <why>   /reopen L <why>
/tag L t         /link L L   /ref L R:n   /promote L
/research L      run the research agent    /accept R:n  keep its answer
/guest [new|off] guest mode               > line = talk only   /talk <msg>
/move L room     /custodian               /backup [dir]
/lib [R:n]       (ctrl+l) run librarian    /approve R:n   /reject R:n
/blob R:n        /check   /verify   /theme (ctrl+t)
```

The theme is read at runtime from `hotel/themes/calculus.json`, which is a Zed theme file used unchanged.
UI roles map to Zed keys, and lobby syntax maps to Zed syntax scopes.

### CLI

```
hotel add | search | annotate <json> | lib <rid> | research <L> | guest "<msg>" [--note]
      | accept <rid> | move <L> <room> | custodian | backup [dir] | restore <src> <dst>
      | forget <rid> --reason | home [dir] | setup | keys list|set|rm | doctor | version
      | timings | log | render | snapshot | review list|approve|reject | check | verify | rebuild
      | reg | replay <session> | rag import|list|forget | labels | sessions | schema
      | mcp [--http --host --port --token]
```

### Inspecting state

```
hotel render --plain | --directory                  # lobby as text
hotel snapshot --cmd "/bounty x?" --svg out.svg     # headless TUI state as text (+ SVG)
hotel timings                                       # model vs. tool latency per agent run
hotel log [-n 50] [--no-follow]                     # live timeline of every process (TUI, MCP, CLI)
hotel replay <session> --librarian rule             # re-run a session with another librarian, diff
```

## Lobby grammar

```
#! hotel-lobby v1

== ROOM: harness ==

@L0001 [x] bounty origin=user ts=2026-10-18T21:13:29Z
  | Does Exa's search API support filtering by published date?
  + ref: R:4
  + closed-by: R:4
```

- **Status sigils:** `[ ]` open · `[x]` closed · `[~]` superseded · `[!]` contested · `[.]` static
- **Types:** `bounty`, `goal`, `idea` use `{ ,x,~,!}`; `monologue`, `reasoning`, `log` use `{.,~}`
- **Origin:** `user`, `approved` (a model draft approved by the user), or `resident`
- **Meta keys:** `tag ref link supersedes superseded-by closed-by promoted note`
- **Allowed transitions:** `' '` → `x`, `~`, `!`; `!` → `' '`, `x`, `~`; `x` → `~`; `.` → `~`; `~` is terminal

The validator rejects:

- body edits and deletions
- changes to id, room, type, origin, or timestamp
- removed or altered meta lines
- insertion anywhere other than the end of a room
- illegal status transitions
- one-sided supersession
- a bounty closed without an annotated `R:<n>`
- resident entries of any type other than `log`

Hand edits to `lobby.txt` are allowed. They are validated against git `HEAD` when the TUI starts (and by
`hotel check`), then committed as user edits.

## Agents

### Research agent

`/research L0005` (or `hotel research L0005`) answers one bounty from web evidence. It is a plain
tool-calling loop with no framework, and it never writes to the lobby directly.

- The agent has four tools: `search` (1–3 queries, run in parallel and de-duplicated), `fetch`,
  `lobby_search`, and `annotate`.
- While a retrieval has no annotation, the model can use only `annotate` (forced with `tool_choice`).
- When the agent makes a mistake, such as a bad quote, a gate block, or a provider `tool_use_failed`, it gets
  the error as a tool result with a specific correction.
- Citations are Reg ids, for example `[R:67]`. The final answer is a gated `output` row.
- If the context is too large, old annotated retrievals are replaced by their annotations.
- After the run, the librarian files references. A model wrote the annotation, so closing the bounty goes to
  review. `/accept R:n` keeps the answer as `origin=approved` with backlinks.

### Guest mode

`/guest` in the TUI (or `hotel guest "…" --note`) turns the session into a note-taking conversation.

- Each line you type is appended to the lobby as a monologue (`origin=user`).
- The guest replies in the Guest tab. It can read the lobby, search the web (gated), and `propose` edits:
  an idea, bounty, supersede, tag, or move.
- Each proposal is dry-run against the validator, de-duplicated, and placed in Review. `/approve R:n` writes
  it with `origin=approved`.
- The guest's replies are not written to the lobby.
- Start a line with `>` to talk without recording it. Messages sent while a turn is running are queued.

## RAG and the Concierge

### RAG

```
hotel rag import notes/ paper.pdf page.html   # files or directories (md, txt, rst, org, html, pdf)
hotel rag list
hotel rag forget 3 --reason "private"          # text, chunks and vectors go; RAG:3 still resolves
```

- Each document's text is a content-addressed blob, so `forget` and backups cover it, and an identical
  import is de-duplicated.
- Documents are split into chunks that follow their structure: headings, paragraphs, and code fences, which
  are never split. Chunks are exact spans of the stored text, cited as `RAG:<n>#<k>`. Each chunk is indexed
  with its heading path (`Guide > Install > Windows`).
- Search is hybrid. bm25 (FTS5) and dense vectors from a local embedding model are fused with reciprocal
  rank fusion. The lobby uses the same hybrid search.
- Annotations referencing `RAG:<n>` or `RAG:<n>#<k>` that do not exist are rejected.
- The embedder runs locally through ONNX (`"embedder": "fastembed:google/embeddinggemma-300m"`, multilingual).
  The first use downloads it (about 1.2 GB) to `~/.cache/hotel/models` in the background; until then search
  is lexical. Use `"embedder": "none"` or `HOTEL_EMBEDDER=none` to turn it off. Vectors are cached in
  `.hotel/vectors.sqlite` by text hash, which can be deleted at any time and is not backed up.

### Next-edit suggestions

While you write a note, Mercury Edit 2 proposes how the line continues as ghost text (Tab or ? accepts). Its
context is filled locally first: the top lobby entries and RAG chunks for your draft, the room's latest
entries, and the notes you wrote this session. Nothing from the web is used. Suggestions only ever extend
the draft, because the lobby is append-only. Each request is a `model` row in the Reg; each shown
suggestion's outcome (accepted, partial or ignored) is a `suggest.outcome` row holding only ids and lengths.
Turn it off with `"suggest": false`. It needs `INCEPTION_API_KEY` and the training opt-out, and it stays
silent without them.

### The Concierge

The Concierge is Hotel's own model stack beside Mercury. Mercury writes; the Concierge is meant to decide
what is relevant, what a source says and whether a claim holds, locally and trained on your own Reg. What
exists today:

- `hotel labels` mines training and evaluation data from the Reg: quote spans of accepted annotations
  (pointer), which hits of each retrieval were quoted (relevance, with the bm25, dense and fused ranks they
  had), lobby entry and evidence pairs, suggestion outcomes and review decisions. It also reports retrieval
  quality: hit@1, hit@3, MRR, and MRR per local ranking.
- `hotel labels --export` writes `.hotel/labels/*.jsonl` and a manifest with the Reg head it was built from.
  `forget` deletes the export, since it may hold copies of the forgotten content.
- When a model's quote is rejected, the correction now names the closest real sentence in the source.

Trained heads (embedding, span pointer, verifier, structure) and the graph layer are the next step; they plug
in behind the embedder and pointer interfaces in `hotel/concierge/`.

## MCP server

`hotel mcp` exposes the lobby over the Model Context Protocol so that clients such as Claude, Amp, and
Cursor can read and research in it. Clients get the same rights as the guest agent, enforced by the server:

- **Tools:** `lobby_directory`, `lobby_search`, `read_entries`, `web_search`, `deep_research`, `fetch`, `annotate`,
  `propose`, `review_queue`. **Resources:** `hotel://lobby`, `hotel://directory`.
- Every call is a Reg row in its own `mcp` session. After `web_search` or `fetch`, all other tools are
  refused until the client annotates.
- Clients never write the lobby. `propose` dry-runs ops against the validator and places them in Review,
  where you approve them with `/approve R:n`.

### Local clients (stdio)

```
claude mcp add hotel -- hotel mcp                                    # Claude Code
"amp.mcpServers": {"hotel": {"command": "hotel", "args": ["mcp"]}}   # Amp settings.json
{"mcpServers": {"hotel": {"command": "hotel", "args": ["mcp"]}}}     # Claude Desktop / Cursor
```

### Remote clients (streamable HTTP)

The endpoint is `/mcp`; `GET /healthz` is an unauthenticated health check.

```
HOTEL_MCP_TOKEN=$(openssl rand -hex 32) hotel mcp --http --host 0.0.0.0 --port 8765
claude mcp add --transport http hotel https://<host>/mcp --header "Authorization: Bearer $HOTEL_MCP_TOKEN"
"amp.mcpServers": {"hotel": {"url": "https://<host>/mcp", "headers": {"Authorization": "Bearer …"}}}
```

Without a token the server binds to loopback only, with the SDK's DNS-rebinding checks. Off loopback it
refuses to start without one.

### Hosting

The lobby is a directory (git repository, SQLite, blobs) protected by a cross-process lock, so
`hotel mcp --http` must run on the machine that holds that directory. It needs a persistent disk and `git`;
serverless platforms (Lambda, Vercel, Workers) are not suitable.

- Your own machine with a tunnel. The lobby stays next to the TUI. Bind to `127.0.0.1`, set a token, and let
  the tunnel provide TLS. Examples: `tailscale serve --bg 8765` (tailnet only), `tailscale funnel --bg 8765`
  (public URL), or `cloudflared tunnel --url http://localhost:8765` (optionally with Cloudflare Access).
- An always-on server (VPS, Fly.io or Railway with a volume, home server). Run `scripts/install.sh /data/lobby`.
  Then run `hotel mcp --http --host 0.0.0.0` under systemd, behind a proxy such as Caddy or nginx that handles
  TLS. Use the TUI over `ssh`, and point `hotel backup` at a second disk.

The client's model sees any lobby text that it reads. The data policy of its provider applies to that text.

## Data safety

Concurrent writers: every lobby write holds a cross-process lock for read, validate, write, and commit.
Hotel validates and commits uncommitted editor changes first. Two OS processes that wrote 30 entries at the
same time tested this.

Backup: `hotel backup [dir]` (default `~/hotel-backups`) is incremental. It copies each blob once, because
blobs are addressed by content. Each snapshot has SQLite online-backup copies of the Reg and RAG store, a
`git bundle` of the full lobby history, and a manifest. `hotel restore <dir> <empty-target>` checks the hash
chain, every referenced blob, and the lobby before it reports success.

Forgetting: `hotel forget R:n --reason …` deletes the content behind a row. This includes the result, the text,
the arguments (queries and prompts), and the annotation quotes. The rows and the hash chain stay. Only the
hash-addressed bytes are removed. It does not cover:

- lobby text, which is in git history (use `git filter-repo`)
- rows written before v0.2, which stored arguments and annotations inline
- existing backups (prune them)
- the sha256 of each deleted blob, which stays in the row. Anyone with the Reg can confirm a guess at short,
  predictable content such as a search query by hashing it.

Forgetting an unannotated retrieval also releases the annotation gate for it.

Model providers: the model router has a guard that requires you to confirm the Mercury training opt-out.

## Lifecycle and model checks

- **Staleness.** Closing a bounty adds `+ revalidate: <date>` (`bounty_revalidate_days`, default 90).
  `hotel custodian` / `/custodian` tags stale bounties and logs that they need rechecking. It does not
  re-answer them; re-run `/research` and supersede.
- **Room drift.** `/move L0004 embeddings [why]` copies a live entry (same type, origin, body, tags, refs)
  into the new room with `moved-from` and supersedes the original. Closed or contested entries stay where they
  happened. Moves by resident entries go to review.
- **Entailment verifier.** Set `"verifier": "<llm profile>"` in `.hotel/config.json`. Each model-written claim
  is judged against its own quote (entailed / overreach / unsupported) before the annotation is accepted;
  overreach is returned to the agent with the reason. Use a different model from the agent, since a model
  grading its own output shares its blind spots.
- **Usage and cost.** Every model row records `tokens_in`, `tokens_out`, and `ms`. If
  `"llm_profiles": {"mercury": {"price": {"in": …, "out": …}}}` is set (USD per 1M tokens), it also records
  `cost_usd`. No prices are built in.

## Development

```
uv sync
uv run pytest -q     # offline; the agent loop is driven by ScriptedTransport
```

CI (`.github/workflows/ci.yml`) runs the tests on Linux and Windows and fails if `uv.lock` or `constraints.txt` is
out of date. After changing dependencies, run `uv lock` and
`uv export --frozen --no-hashes --no-emit-project --no-dev -o constraints.txt`.

## Status

| Component | State | Location |
|---|---|---|
| Reg (append-only SQLite, hash chain, blobs) | Implemented | `reg.py`, `blobs.py` |
| Gateway and annotation gate | Implemented | `gateway.py` |
| Exa search and fetch | Implemented | `search.py` |
| Lobby grammar, parser, validator | Implemented | `grammar.py`, `validator.py` |
| Edit ops, git commit per edit | Implemented | `models.py`, `ops.py`, `core.py` |
| Librarian (`RuleLibrarian`, `ModelLibrarian`) | Implemented; Mercury path not tested against the live API (tested via scripted transport) | `librarian.py` |
| Model router (provider SDKs: `inceptionai`, `groq`, `openai`-compatible; retries, opt-out guard) | Implemented | `llm.py` |
| Deep research (Exa Deep Search + Exa Agent runs, gated like any retrieval) | Implemented | `search.py` |
| Research agent | Implemented; tested live with Exa and groq | `agent.py` |
| Resident policy (log-only appends, rate cap, bounty close, review queue) | Implemented | `core.py` |
| Unified search (lobby FTS5 + RAG FTS5 + Exa + Parallel) | Implemented | `search.py` |
| Exa + Parallel web search (concurrent, de-duplicated by URL, fused by RRF; either one alone via `search_backend`) | Implemented; tested live | `search.py` |
| RAG with one-way refs and logged promotion | Implemented | `rag.py`, `PromoteOp` |
| Directory layer | Implemented | `derived.py` |
| Replay harness | Implemented | `replay.py` |
| Feedback tooling (`render`, `snapshot`, `timings`, scripted transport) | Implemented | `snapshot.py`, `cli.py`, `llm.py` |
| Guest agent, custodian, move, forget, backup/restore, verifier, usage logging, install scripts | Implemented (v0.2) | `guest.py`, `core.py`, `backup.py`, `verify.py`, `scripts/` |
| MCP server (stdio and HTTP with bearer token) | Implemented | `mcp_server.py` |
| sqlite-vec embeddings, talk/solve agents, concierge, custodian dedup, hotel renderer | Not implemented | — |

## Deep research

`/deep <q>`, the research agent's `deep_research` tool (once per task) and the MCP `deep_research` tool run
Exa's research products through the `exa-py` SDK. `depth: "deep"` is Deep Search (`type=deep`: Exa iterates
its own searches, optional `angles` seed distinct sub-questions); `depth: "agent"` is an Exa Agent run
(async, minutes, billed per run). Both land as one retrieval that must be annotated like any other: the
selected pages are `external` with real highlights to quote, while Exa's synthesis is flagged `secondhand`.
Agent runs are configured in `.hotel/config.json`: `exa_agent_effort` (default `medium`, fixed price per run),
`exa_agent_max_cost` (USD cap, only for `auto`/`ultra`) and `exa_agent_timeout_s`.
