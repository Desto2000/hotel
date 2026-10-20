# Hotel tutorial: your first research session

This walks through one complete loop, from asking a question to closing it with evidence, in about
15 minutes. The [README](README.md) is the reference; this is the guided path. Every step below was run
against a fresh lobby, and the output shown is what Hotel prints.

You will:

1. create a lobby and add your API keys
2. write a question (a *bounty*) and some notes
3. search the web, and annotate the result (the step that confuses everyone at first)
4. close the bounty with that evidence
5. let the research agent do steps 3–4 for you

## The one idea to hold on to

Hotel won't let a web result into your notes until you say what it showed and quote the passage that
shows it. When a search or fetch returns, the **annotation gate** locks. While it is locked:

- **still works:** writing notes, `/ls` (search your own lobby), `/help`, reading anything
- **refused:** the next `/s`, `/exa`, `/deep`, `/fetch`, and `/research`

The status bar always shows the state: `gate open` or `gate LOCKED R:3 (ctrl+a)`. When it says LOCKED,
press **Ctrl+A** (or F2). That's how you unlock it.

`R:3` is a **Reg id**: a numbered row in the log of everything Hotel did. `L0001` is a **lobby id**: one
entry in your notes. Most commands take one or the other.

## 1. Set up

Install Hotel (see [Installation](README.md#installation)), then create a lobby. Any empty directory works:

```
hotel init ~/lobby
hotel doctor
```

`doctor` should report `[ok  ]` for git, the hotel home, and the lobby. It reports `warn` for missing keys
and backups. Now open the TUI:

```
hotel
```

The first launch shows the onboarding screen. Paste your `EXA_API_KEY` (search) and `INCEPTION_API_KEY`
(the Mercury model). Each key is checked live. You can skip it with Escape and come back with `/setup`.

The screen has four parts:

```diagram
┌──────────────────────────── status: session · room · gate · librarian ─┐
├───────────┬───────────────────────────────┬────────────────────────────┤
│ directory │ lobby (your notes)            │ Reg │ Results │ Review │   │
│ (rooms)   │                               │ Guest │ Blob               │
│           │                               │                            │
├───────────┴───────────────────────────────┴────────────────────────────┤
│ messages                                                               │
│ > command line: type notes or /commands here                           │
└────────────────────────────────────────────────────────────────────────┘
```

Type `/help` at any time for the command list.

## 2. Write a question and some notes

Notes live in **rooms**. The TUI starts in `inbox` every time you open it, so switch rooms first:

```
/room harness
/bounty Does Exa's search API support filtering by published date?
I think Exa has date filters but I am not sure.
```

The message line confirms each one (`+ L0002 monologue in harness`). The lobby pane now shows:

```
== ROOM: harness ==

@L0001 [ ] bounty origin=user ts=2026-10-20T23:16:01Z
  | Does Exa's search API support filtering by published date?

@L0002 [.] monologue origin=user ts=2026-10-20T23:16:01Z
  | I think Exa has date filters but I am not sure.
```

- A line without a `/` becomes a *monologue*, a plain note.
- `/bounty` is a question you want answered. `[ ]` means open. Also available: `/goal`, `/idea`,
  `/reason`, `/log`.
- Entries can't be edited or deleted. To change one, replace it: `/supersede L0002 <new text>`. The
  validator enforces this, which is what makes the lobby trustworthy later.

## 3. Search, then annotate

```
/s exa published date filter
```

The Results tab fills in, and the message line says:

```
R:3 stored (… hits, text f50d07e697). gate locked — ctrl+a to annotate.
```

If you try another search now, you get `⛔ annotation gate: annotate R:3 before the next call`. That's
expected. Press **Ctrl+A**. The annotation window opens with the stored text on the left and a form on
the right:

| Field | What to put | Example |
|---|---|---|
| claim | What the source says, in your words | `Exa filters search by published date` |
| quote | A passage copied from the left pane. Spacing doesn't have to match exactly | `Use startPublishedDate and endPublishedDate` |
| relation | How it relates to your notes | `supports` |
| lobby refs | Entries it bears on | `L0001` |

Click **+ add claim**. Add more claims if the result says several useful things. Then fill in:

| Field | Example |
|---|---|
| caused | `answers L0001` |
| caused refs | `L0001` |

and click **submit**. The gate opens. If the result was useless, click **nothing useful**. That records
it and also opens the gate.

If you see `quote not found in the stored data`, the quote isn't in the left pane. Copy it from there,
not from a browser. You don't need to compute character positions; Hotel does that from the quote.

## 4. Close the bounty

Close the question with the retrieval you annotated:

```
/close L0001 R:3
```

```
@L0001 [x] bounty origin=user ts=2026-10-20T23:16:01Z
  | Does Exa's search API support filtering by published date?
  + closed-by: R:3
  + revalidate: 2027-01-18
```

`[x]` means closed. Hotel refuses to close a bounty with an `R:` id that hasn't been annotated, so every
closed question points to quoted evidence. The `revalidate` date is when `/custodian` will flag it as
possibly stale.

Check that everything is consistent:

```
/check      → lobby valid ✓
/verify     → reg hash chain intact ✓
```

## 5. Let the agent do it

Steps 3–4 can be done for you. Add another bounty and hand it to the research agent:

```
/bounty Does Exa support excluding domains from search?
/research L0003
```

The message line streams `agent · …` progress. The agent searches, annotates each result itself (the
gate applies to it too), and writes an answer that cites Reg ids like `[R:12]`. It never writes to your
lobby directly. Instead:

- The answer appears in the Blob tab with `/accept R:n to keep it`. `/accept R:n` saves it as a
  `reasoning` entry marked `origin=approved`.
- The model, not you, wrote the annotations, so closing the bounty goes to the **Review** tab. There,
  `/approve R:n` applies it and `/reject R:n` drops it.

## Where to go next

| You want to… | Use |
|---|---|
| Chat about your notes and have the model draft ideas and bounties for review | `/guest` (see [Guest mode](README.md#guest-mode)) |
| Use the lobby from Claude Code, Amp, or Cursor | `hotel mcp` (see [MCP server](README.md#mcp-server)) |
| Search only your own notes (never gated) | `/ls <query>` |
| Look at the full text behind any Reg row | `/blob R:n`, or select the row in the Reg tab |
| Fix a wrong entry | `/supersede L <text>`, `/contest L <why>` |
| Back up everything | `/backup` or `hotel backup` |

## When something goes wrong

| Message | Meaning and fix |
|---|---|
| `⛔ annotation gate: annotate R:n before the next call` | A retrieval is waiting. Press Ctrl+A. Starting a new session doesn't clear it. |
| `quote not found in the stored data` | Copy the quote from the annotation window's left pane. |
| `validator rejected: …` | The edit would break lobby rules, such as closing with an unannotated `R:`, changing an entry's body, or an illegal status change. The message names the rule. |
| `no hotel home found` | Run `hotel init <dir>`, or `hotel home <dir>` to point at an existing lobby. |
| Search or research fails right away | Run `hotel doctor` to see which keys are missing, then `/setup`. |
