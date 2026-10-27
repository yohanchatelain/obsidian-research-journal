# Obsidian Research Journal Bridge

A local research notebook shared by Codex, Claude Code, and Google Antigravity.
The assistants read recent project context, record meaningful progress, capture ideas,
and update exact TODO checkboxes with evidence. Your existing Obsidian notes stay in place.

It also coordinates [agent review rounds](docs/reviews.md): two independent critiques,
human-approved executable checks, an author response and revision, a follow-up from both
reviewers, and a human decision saved in Obsidian. Checks can run locally, over SSH, or
through Slurm with durable pause and recovery.

The runtime uses Python 3.11+ and its standard library. It works without a model API key,
package downloads, an Obsidian community plugin, or an open Obsidian window.
Automatic reviews use your native assistant authentication and may incur model costs.
Linux and macOS are supported; file locking uses Unix `flock`.

Generate an isolated example vault with `python3 scripts/create_demo.py`, then open
`local/demo/vault` as a vault in Obsidian. Its fictional examples show all three assistants,
an idea, and a task completion held for review.

## Install

From this checkout, preview the changes first:

```bash
python3 bin/research-journal setup --vault "$HOME/Documents/Obsidian Vault"
```

Install the configuration, command, global assistant instructions, MCP connections, and hooks:

```bash
python3 bin/research-journal setup --vault "$HOME/Documents/Obsidian Vault" --apply
```

Setup preflights write access, preserves existing settings and hooks, and saves private
backups before changing settings. The preview contains only bridge-owned additions,
not other settings or credentials. The checkout must stay at this location because
the installed command and clients reference its absolute path.

Restart your assistants. Inspect `/mcp` and `/hooks` in each client and enable or trust
the added integration if the client requests it. Hooks require a client version supporting
the documented lifecycle events; the installer does not bypass client trust or policy.
For Antigravity IDE, use its MCP and Customizations panels.

```bash
research-journal doctor
```

`doctor` checks the configuration files; client loading and hook trust must also be checked
inside each assistant. All local sessions are included, including general planning.
Browser-only chats need an explicit remote connection; see the optional
[ChatGPT integration via Secure MCP Tunnel](docs/chatgpt.md).

If this workspace is opened in a restricted agent session, your vault or home settings
may be mounted read-only. Run the install command in your normal terminal. The tool
does not change permissions or request elevated privileges.

## How it works

1. At the start of work, the assistant retrieves the project's recent sessions and open tasks.
2. During work, it records goals, methods, findings, failed attempts, decisions, evidence,
   blockers, and next steps. Planned and running work are distinguished from verified results.
3. New ideas get individual notes. Intended actions go into today's section of `TODO list.md`.
4. Before the assistant finishes, a hook requests a missing checkpoint once. Uncertain
   task updates go to the inbox instead of changing a checkbox.

The active assistant writes summaries using its existing session. There is no separate
background model service. Abrupt process termination, disconnected MCP servers, denied
tool calls, or clients that do not load the hooks can leave gaps. Check `status` to see
uncheckpointed sessions and pending writes. Full transcripts are not copied or backfilled.
Previously detected capture gaps remain visible even after later turns are recorded.

```text
Your vault/
├── TODO list.md                       Existing dated checkboxes
├── Existing project folders/          Existing research notes
└── Research Journal/
    ├── Home.md                       Open tasks, projects, progress, ideas, inbox
    ├── Daily/YYYY-MM-DD.md            Links to that day's research sessions
    ├── Sessions/                      Detailed notebooks and task completion evidence
    ├── Projects/                      Current state, notes, tasks, recent sessions
    ├── Reviews/                       Agent critiques, checks, responses, decisions
    ├── Ideas/                         Future project ideas
    ├── Inbox/                         Unassigned sessions and uncertain updates
    └── Templates/                     Research session, idea, daily templates
```

Home and project views are generated between clearly marked comments. Write your own
notes outside those sections. Session and idea notes are ordinary editable Markdown;
views rebuild from session notes, including handwritten corrections and renamed files.
Dates use `America/Toronto` by default.

## ChatGPT (optional private connection)

ChatGPT cannot launch this bridge's local stdio process directly. The
[Secure MCP Tunnel instructions](docs/chatgpt.md) describe how eligible
ChatGPT workspaces can connect to the existing stdio server without
publishing the vault. Use `mcp --client chatgpt` for read-only tools by
default, and only add `--allow-writes` after reviewing the security and
permission implications. ChatGPT does not provide these clients' lifecycle
hooks, so capture is explicitly requested rather than automatic.

## Agent review rounds

```bash
python3 scripts/create_review_demo.py                 # isolated example; no models or SSH
research-journal review create examples/review.json  # edit the source and criteria first
research-journal review doctor                       # local CLI preflight only
```

Use manual fresh conversations, automatic native CLI reviewers, or a mix. New commands
need human approval in the CLI; participant MCP tools cannot approve checks or close
reviews. First-round critiques remain hidden until both reviewers finish. Long jobs
can be polled later without a coordinator daemon. See the [complete workflow and
configuration](docs/reviews.md) before launching real checks or model calls.

## Connect projects

Register a project's local repositories and relevant existing notes:

```bash
research-journal project baseline --name "Baseline research" \
  --workspace "$HOME/Work/baseline-research" \
  --note "Baseline research/Overview.md"
```

Repeat `--workspace` and `--note` for multiple paths. Nested workspace mappings use the
most specific registered path. Re-registering a project replaces its mappings and note list.
Configuration lives at `~/.config/research-journal/config.json`; see
[examples/config.json](examples/config.json). Pass `--config PATH` **before** the subcommand
to use a different configuration. All clients must use the same configuration and state directory.

For installations on rorqual or slashbin, see [remote installation](docs/remote-install.md).
Each host keeps its own state. Optional [SSH note synchronization](docs/sync.md)
keeps Markdown vault mirrors current using rclone bisync from your workstation.

Unmapped sessions go to the inbox. Assign them with:

```bash
research-journal assign-session "Research Journal/Sessions/manual-SESSION.md" --project baseline
```

Ask an assistant to link an existing task to a registered project. It uses `search_tasks`
followed by `link_task`, preserving the checkbox's original note and date. Task lists in
the dashboard are links to original checkboxes, so there is no second task list to maintain.

## Quick capture outside an assistant

```bash
research-journal idea "Compare registration stability" \
  --why "Could explain differences across software versions" \
  --next "Try a pilot on a small sample" --project baseline

research-journal task "Compare the completed baseline against the reference" --project baseline

research-journal progress "Pilot analysis" \
  --finding "Two subjects timed out; the remaining results need inspection" \
  --next "Rerun those subjects and inspect effect sizes" --project baseline

research-journal tasks "baseline"
research-journal context --project baseline
research-journal refresh
```

You can also write notes and tick tasks normally in Obsidian. Run `refresh` after manual
changes to update dashboard links and counts; assistant writes refresh them automatically.
Templates can be inserted manually without changing your existing Daily Notes configuration.

For a detailed manual checkpoint, `progress --json` reads a JSON object from stdin:

```json
{
  "title": "Permutation test analysis",
  "project": "baseline",
  "sections": {
    "goals": "Assess baseline significance",
    "methods": "Compare median and best permutation scores",
    "findings": "The current output contains an arbitrary permutation",
    "decisions": "Rerun before interpreting significance",
    "evidence": "results/permutation-summary.csv",
    "next_steps": "Generate median and best-score summaries"
  }
}
```

## Task completion and recovery

Completion uses an exact object returned by `search_tasks`, with the note path and a
stable block ID or an unchanged line fingerprint. It requires an evidence reference and
an explanation of how the evidence satisfies the task. Open subtasks prevent automatic
parent completion. The assistant evaluates the evidence; the bridge does not independently
establish the scientific validity of a result.

The `update-task` CLI reads a single task object on stdin and accepts `--evidence`,
`--reason`, and `--reopen`. `link-task --project KEY` accepts the same object.
Changed, moved unanchored, or ambiguous task references create inbox proposals.

```bash
research-journal status
research-journal retry
research-journal event EVENT_ID
research-journal discard EVENT_ID
```

Writes are serialized across assistant processes and planned in SQLite before replacing
files atomically. A retry recognizes already written content and avoids duplicating it.
If you edited a queued target, it is preserved and the write stays pending with an inbox
conflict note. Inspect the event, discard its remaining writes if appropriate, and issue
a new update against the current task. Discarding does not undo already applied writes.
Use `--event-id` on capture commands when retrying a request; reuse with different content
is rejected.

Uninstall assistant integrations while keeping notes, tasks, configuration, state, and backups:

```bash
research-journal uninstall
```

Uninstall removes only matching bridge-owned entries. It preserves unrelated and later
settings, and retains bridge sections that were edited manually for review.

## Develop and verify

```bash
python3 -m pytest -q
```

Tests cover exact dated/nested tasks, manual edits, duplicate delivery, process concurrency,
interrupted writes, recovery, installation/removal, all client hook adapters, and an actual
MCP stdio subprocess. Review tests also cover two-round revision, embargo and authority,
local workers, fake native reviewers, SSH transport, fake Slurm contracts, and paused
bundle recovery. Live model calls and remote environments require separate verification. The protocol server implements the tools-only stdio subset, negotiating
versions `2024-11-05`, `2025-03-26`, and `2025-06-18`. It advertises no remote transport,
resources, sampling, or elicitation capabilities.

Sources: [MCP stdio](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports),
[MCP lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle),
[Codex hooks](https://developers.openai.com/codex/hooks),
[Claude Code MCP](https://code.claude.com/docs/en/mcp),
[Claude Code hooks](https://code.claude.com/docs/en/hooks),
[Antigravity hooks](https://antigravity.google/docs/hooks), and
[Antigravity rules](https://antigravity.google/docs/rules).

## License

[MIT](LICENSE), copyright 2026 Yohan Chatelain.
