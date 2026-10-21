# Install on rorqual and slashbin

Install the source checkout in your own account. Python 3.11 or newer is required;
the runtime has no package dependencies. On a cluster, load an available Python
module first if the default interpreter is older. Do not run the installer as root.

On each host:

```bash
git clone https://github.com/yohanchatelain/obsidian-research-journal.git \
  "$HOME/.local/share/obsidian-research-journal"
cd "$HOME/.local/share/obsidian-research-journal"
python3 --version
```

If research-journal already has a configuration on that host, preview and apply it:

```bash
python3 bin/research-journal setup
python3 bin/research-journal setup --apply
```

For a new configuration, select the vault path that is actually available on that
host. It must be a shared/synchronized copy of the notebook if you want the same
notes and TODOs across machines:

```bash
python3 bin/research-journal setup --vault /path/to/remote/Obsidian-vault
python3 bin/research-journal setup --vault /path/to/remote/Obsidian-vault --apply
```

Installing this tool does not copy or synchronize the vault. An independent empty
remote vault will be an independent notebook. Use separate state directories on each
machine; do not synchronize live SQLite databases or worker staging directories.
For workstation-driven Markdown mirroring, see [SSH synchronization](sync.md).
Concurrent note synchronization must be handled by the chosen filesystem/sync tool.

Setup preserves unrelated assistant settings and keeps private backups. A conflicting
existing bridge command causes setup to stop for review. Keep the source checkout at
the installation path because the command and assistant configurations reference it.

Then verify:

```bash
"$HOME/.local/bin/research-journal" doctor
"$HOME/.local/bin/research-journal" review doctor
```

Restart Codex, Claude, or Antigravity on the remote host and check that the journal
MCP server and lifecycle hooks load. The remote assistant's normal authentication
still applies. Installation does not launch inference or submit scheduler jobs.

For an isolated check without touching the notebook:

```bash
python3 scripts/create_review_demo.py --output /tmp/research-journal-review-test
```

Choose a new output directory when repeating the demo. To update the installation,
pull changes in the same checkout, run the tests where pytest is available, and
restart the assistants. If installation settings change, preview setup before
applying it again.
