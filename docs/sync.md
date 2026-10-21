# Synchronize notes over SSH

`research-journal sync` optionally runs rclone bisync from a workstation to remote
Markdown vaults. The core journal remains dependency-free; this optional command
requires a separately installed rclone version supporting bisync, backups and recovery
(tested with v1.75.2). SSH host aliases, jump hosts, host-key checking and normal SSH
authentication are handled by the existing OpenSSH client. No credentials are copied.

Use the workstation as the hub. Remote hosts need the journal and SFTP, but no rclone
process or synchronization daemon. The workstation must be awake and able to authenticate
to each host. A disconnected host remains stale until a successful later sync; manually
sync before relying on recent edits made elsewhere. An expired MFA/control connection
may require reconnecting with ordinary `ssh HOST` first.

## Configure once

Create empty remote vault directories and a `RCLONE_TEST` file in each. Put the same
access-marker filename in the local vault. These markers protect against interpreting
an unavailable filesystem as a notebook full of deleted files. Do not delete them.

Copy [examples/sync.json](../examples/sync.json) beside the journal configuration,
usually `~/.config/research-journal/sync.json`, and edit every executable and path.
Use a dedicated remote backup directory outside the vault. Do not put tokens or keys
in the configuration. Initialize an empty remote mirror from the workstation:

```bash
research-journal sync --initialize --dry-run
research-journal sync --initialize
```

Initialization prefers the workstation copy for a same-path difference, saving the
replaced remote copy in backups. Initialize only after inspecting both sides. Each
pair can be initialized once; the command refuses to silently recreate an existing
baseline. Never add `--resync` to an automatic timer or use it to hide a failed run.

On the remote host, install with the selected mirror as its vault:

```bash
python3 bin/research-journal setup --vault /path/to/remote/vault --apply
```

Keep journal state outside the vault. The workstation and each remote host use their
own SQLite databases, locks, review assignments and worker directories. Synchronizing
rendered review notes does not join independent review coordinators; use review bundles
when transferring protocol state. Local workspace/project registrations remain specific
to each host.

## Run and inspect

```bash
research-journal sync
research-journal sync --host my-cluster
research-journal sync --status
```

Only visible Markdown notes and saved Markdown conflict copies transfer. Hidden
application/credential files, attachments, datasets, databases and worker outputs do
not transfer. This is a note mirror, not a full Obsidian application-data backup.
Generated Home/project dashboards and the review index stay local and are refreshed
from the synchronized notes. Notes written outside generated sections of those excluded
dashboards also remain local; put shared annotations in an ordinary note instead.

If a note changed on both sides, the workstation version stays at the original path
and the other version is preserved as `note.md.rj-conflict-remote1` (or a later number).
Both versions remain recoverable; this is not a textual merge or empirical verification.
Conflict copies use a non-Markdown extension so they cannot become duplicate task sources.
The workstation adds an inbox notice listing conflicts. Open the saved copy in an editor,
merge the intended changes into the original note, and remove the saved copy only after
verifying the result. Human decisions and TODO completion still require source evidence.

Replacement/deletion backups live outside the vault, in each host's configured backup
location and the workstation's `state/sync/backups`. Large deletion batches abort. File
transfers use temporary destination files; simultaneous editing can still cause a retry
or a conflict. Read the [rclone bisync limitations](https://rclone.org/bisync/#limitations)
and keep independent backups. Sync does not hold the journal write lock during network
transfers, so it does not block journal hooks for the duration of an SSH transfer.

The last run, errors and conflicts are stored in `state/sync/last-run.json`. A failed
pair does not prevent attempting the other hosts, and the CLI exits nonzero if any pair
fails. Transient failures can be retried normally. If rclone requires baseline recovery,
inspect its listing state and both vaults first; the wrapper never automatically resyncs.

## Optional workstation timer

On a Linux workstation, a user systemd timer can run the command every few minutes.
Use absolute paths to Python, the checkout launcher and the journal configuration in
the service, and allow enough time for all configured hosts. Keep the checkout stable.
The timer uses the user's existing SSH agent/credentials and does not bypass MFA.

```bash
systemctl --user status research-journal-sync.timer
systemctl --user start research-journal-sync.service
journalctl --user -u research-journal-sync.service -n 30
systemctl --user disable --now research-journal-sync.timer
```

A global sync lock prevents a manual run from overlapping the timer. Logs contain
operational errors and filenames; keep them private. No notebook contents or personal
sync settings belong in the public source repository.

Implementation references: [bisync](https://rclone.org/bisync/),
[conflict handling](https://rclone.org/bisync/#conflict-resolve-choice),
[external SSH for SFTP](https://rclone.org/sftp/#sftp-ssh).
