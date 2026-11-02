# ChatGPT integration (private Secure MCP Tunnel)

The bridge already speaks tools-only MCP over **stdio**. OpenAI's [Secure MCP
Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
can connect a private local stdio server to supported ChatGPT workspaces
without exposing an Obsidian vault to the internet. The existing Codex,
Claude Code, and Antigravity connections remain unchanged.

**Availability:** Check the current [ChatGPT custom MCP requirements](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)
before starting. Full custom MCP connections are documented for Business and
Enterprise/Edu workspaces; personal-plan access and write permissions may differ.
A working local server does **not** imply that your ChatGPT account can install it.

## 1. Preflight locally

Run these on the **machine that has the actual configured vault** (not on
ChatGPT's servers). Keep this checkout at its installed path.

```bash
python3 bin/research-journal doctor
python3 bin/research-journal mcp --client chatgpt
```

The second command waits for MCP JSON-RPC on stdin; it prints no success
message. Stop it with Ctrl-C. If you use a non-default configuration,
put `--config /absolute/path/to/config.json` **before** `mcp`.

Unlike a coding assistant, browser ChatGPT has no Research Journal lifecycle
hooks, no local current-working-directory, and no automatic turn capture.
Ask it explicitly to call `list_projects`, retrieve context
(`get_context(project="...")` with a returned project key),
record an idea, or save a progress checkpoint when permitted.

## 2. Create a private tunnel

1. In your OpenAI Platform account, open the [tunnel setup
   guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
   and create a tunnel linked to the **same eligible ChatGPT workspace**.
2. Install the supported `tunnel-client` on the vault host, using the
   download/link provided in Platform. See `tunnel-client help quickstart`
   for the version's supported options.
3. Configure a named **stdio** profile. The command below is illustrative;
   substitute **absolute paths** and your Platform tunnel ID:

```bash
# Set CONTROL_PLANE_API_KEY using secure local environment/secrets management;
# never put a key into a checked-in file, shell argument, or chat.
tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile research-journal \
  --tunnel-id tunnel_YOUR_ID \
  --mcp-command "python3 /absolute/path/obsidian-research-journal/bin/research-journal --config /absolute/path/config.json mcp --client chatgpt"

tunnel-client doctor --profile research-journal --explain
tunnel-client run --profile research-journal
```

The local server is started by the tunnel client. You do **not** need to run
a listening HTTP port or install a new Obsidian plugin. The tunnel only works
while its client is running on the vault host. Keep the tunnel credentials
private; use the platform's access controls and dedicated workspace.

## 3. Connect in ChatGPT (if eligible)

From **ChatGPT web**, go to **Plugins → Add custom MCP server**, choose
**Tunnel**, then select the matching tunnel (or enter its ID). Review
the requested access and finish creating the private plugin. Ask for the
`status` or `get_context` tool and check that it accesses the *right vault*.
If the tunnel does not appear, check organization/workspace association
and tunnel permissions before debugging the bridge.

In the default ChatGPT mode the server advertises only:

- `list_projects` (discover configured projects)
- `get_context` (use a project key from `list_projects`)
- `search_tasks`
- `status`
- `review_get_packet` and `review_status` (require the review's participant credentials)

Other tools are *not just hidden*; a call to them is rejected server-side.
The default is deliberately **read-only**. No notes or checkboxes are
modified by ChatGPT in that mode.

## 4. Enable writes only if needed

For an eligible workspace with write-capable custom MCP support, explicitly
opt into write tools by adding `--allow-writes` to the stdio command:

```bash
python3 /absolute/path/obsidian-research-journal/bin/research-journal \
  --config /absolute/path/config.json mcp --client chatgpt --allow-writes
```

Then update/restart the tunnel profile and re-check ChatGPT's tool permissions.
With write access enabled, ChatGPT can call `record_progress`,
`capture_idea`, `add_task`, `update_task`, `link_task`, `assign_session`,
and `refresh`. **Review execution, review participant mutations, and hook-only
checkpointing remain unavailable** even with the write flag.
These affect your **real Obsidian vault**. Ask for a summary and approve
significant edits rather than assuming automatic capture. Existing task
completion rules still require evidence and a reason.

**Security:** A tunnel is a private network path, not a replacement for
workspace authentication, permission review, or good vault hygiene. Only
connect a trusted workspace and never expose the stdio server through an
unauthenticated public HTTP proxy. Do not paste credentials into chats.
The bridge does not upload whole conversations or synchronize the vault;
its normal MCP calls read/write the files already available on the host.

## 5. Verify

- `tunnel-client doctor --profile research-journal --explain` reports ready.
- ChatGPT discovers the expected read-only tools.
- Calling `get_context` returns a known project or TODO reference from the
  configured vault.
- Unless you deliberately selected `--allow-writes`, `add_task` and
  `record_progress` are absent. The local tests also verify server-side rejection.
- After opting into writes, create an explicitly approved **test** idea and
  check the corresponding Markdown file inside Obsidian.

If an account cannot create a custom MCP connection, you can still use this
bridge through Codex locally. ChatGPT cannot access the local vault merely
because the repository is connected through GitHub.
