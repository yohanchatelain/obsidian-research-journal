# Durable agent review rounds

A review freezes an artifact, collects two independent critiques, lets the author respond
and optionally revise once, then gives **both reviewers** a follow-up pass. The human
closes the review with a decision. Findings and executable check requests are separate:
requesting a check never grants permission to run it.

This is a local protocol implemented over the journal's MCP tools and CLI. It is not an
A2A implementation or a general agent message bus. Python 3.11+ and Unix file locking
are required. There is no coordinator daemon.

## Try a complete example

```bash
python3 scripts/create_review_demo.py
```

Open `local/review-demo/vault` in Obsidian. The fictional example runs a small local
Python check, records two manual review rounds using test session receipts, freezes a
revision, and closes with explicit evidence. It never invokes a model or SSH. Do not use
its synthetic receipt technique to join real reviews.

For a real review, use the installed `research-journal` command, or replace it below
with `python3 bin/research-journal`. Pass `--config PATH` before `review` when needed.
Existing journal configuration remains valid without a `review` section.

## Freeze and start

Edit [examples/review.json](../examples/review.json) and run:

```bash
research-journal review create examples/review.json
research-journal review status REVIEW_ID
```

A source can be one file or a Git directory. Directory snapshots copy current contents
of tracked files, including uncommitted edits; deleted tracked files are absent. Use
`"include": ["untracked-notes.md"]` to include selected untracked files. Other untracked
files are excluded. Symlinks, submodules, traversal paths, `.git`, `.env`, common credential
locations, and the reserved `.rj-control` directory are rejected. This is not a complete
secret detector: select review inputs deliberately. Packets are limited to 64 MiB and
10,000 files, and are identified by a manifest hash plus per-file SHA-256 hashes.
`context` and `criteria` are frozen with the files. The original source is never a job's
working directory. Omit `project` for an inbox review, or set a registered project key.

Baseline checks run before collection:

```bash
research-journal review check propose REVIEW_ID examples/check.json
research-journal review check approve REVIEW_ID CHECK_ID
research-journal review check run REVIEW_ID CHECK_ID
research-journal review watch REVIEW_ID --timeout 60
research-journal review start REVIEW_ID
```

The example check needs a packet containing the repository and its tests, not just the
README used by the example review specification. Set `source` to your Git repository
when running that check. Commands execute with the installed environment; dependencies
are not installed automatically.

`start` seals baseline evidence and enters `collecting`. Pending or unknown attempts
prevent sealing. Failed checks remain evidence of failure and do not become success.

## Manual reviewers

Exactly two distinct slots are required. Each slot names a client (`codex`, `claude`,
`antigravity`) and a mode (`manual`, `automatic`). Defaults are manual Codex and Claude.
Antigravity can replace either slot. Make separate invitations:

```bash
research-journal review invite REVIEW_ID correctness
research-journal review invite REVIEW_ID skeptic
```

Send each invitation only to its intended reviewer. Start a **new assistant conversation**
after the review is created. Its `SessionStart` journal hook must have run, with a fresh
startup receipt. A resumed session, an older receipt, a missing hook, a different client,
or reuse of one conversation for both slots is rejected. Each follow-up invitation also
requires a receipt created after that follow-up assignment. The assistant explicitly
declares fresh context when joining; hooks cannot prove that a person has not pasted
prior conversation into a new chat.

Give the reviewer this workflow:

1. `review_join(review_id, token, session_id, fresh_context=true)` using its hook context.
2. `review_claim(review_id, token)`; retain the returned attempt ID and expiry.
3. `review_get_packet(review_id, token)`; assess only those frozen inputs and evidence.
4. Optionally `review_request_check(review_id, token, spec, message_id)`.
5. `review_submit(review_id, token, attempt_id, version, result, message_id)`.

The connection binds the client. A result has this shape:

```json
{
  "summary": "What was assessed and what remains uncertain",
  "findings": [{
    "id": "F1",
    "claim": "The exact claim under review",
    "concern": "Why the claim may be wrong or incomplete",
    "evidence": "artifact.txt:12; packet version 1",
    "recommendation": "A concrete correction or discriminating check"
  }]
}
```

An empty findings array is valid. Use a unique nonempty message ID per submission;
redelivery with the same content is idempotent, and reusing an ID for different content
is rejected. Leases default to 600 seconds. Submission against an expired lease, an old
packet version, a superseded attempt, or a finished round is rejected.

First-round findings and newly proposed checks are hidden from peers, the author,
and Obsidian until both slots are terminal. The human CLI can inspect coordinator state.
If someone cannot finish, use `review missing REVIEW_ID SLOT --reason "..."` to make that
absence explicit. Do this deliberately: revealing a round prevents retrying it. A live
or submitted assignment cannot be retried; an expired one may be reset with
`review retry REVIEW_ID SLOT`, followed by a new invitation. Old capabilities are invalidated.

The CLI also supplies `join`, `claim`, `packet`, and `submit`. Put invitations in the
`RJ_REVIEW_TOKEN` environment variable rather than shell history; `--token` is available
for controlled local scripting. These are capabilities for trusted local clients, not
an authentication boundary against another program running as your operating-system user.

## Automatic reviewers

Merge [examples/review-config.json](../examples/review-config.json) into the `review`
property of your existing configuration. `reviewers` is a mapping of named profiles;
`participants` selects the two default slots. A review specification may override these.
Profiles support `client`, `mode`, `binary`, `model`, and `timeout` (default 600 seconds,
maximum 3600). Do not put credentials in profiles or check specifications.

```bash
research-journal review doctor
research-journal review start REVIEW_ID
research-journal review run REVIEW_ID
research-journal review watch REVIEW_ID --timeout 60
```

`run` launches at most two fresh native CLI sessions in parallel. Each slot gets one
attempt, with bounded output and a timeout. Repeating `run` does not launch already
reserved slots. No continuation or resume flags are used. Hooks are suppressed with
`RJ_REVIEW_RUN=1`; each child receives the frozen packet and sealed check evidence.

Adapters check installed `--help` before launch and fail rather than silently dropping
required flags. Codex uses an ephemeral read-only execution, ignores user config/rules,
and writes a structured final result. Claude uses safe/restricted mode, disables built-in
tools, customizations and external MCP configuration, and requests structured JSON.
Antigravity uses a new project in plan mode with terminal sandbox restrictions and
structured JSON. Its prompt is bounded to fit a command argument. Native authentication
remains the client's responsibility. In particular, installed CLI versions and provider
authentication may make an automatic profile unavailable; manual mode remains usable.

Automatic outputs additionally have a `check_requests` array with `argv`, integer
`timeout`, executor name, and `reason`; use an empty array if none. Requests are embargoed
with the findings, then appear as unapproved checks. They are never executed by the
reviewer. Follow-up evidence is sealed, so follow-up outputs must request no new checks.

Provenance includes the executable, CLI version/help hash, requested model, returned
model/usage where the client reports it, packet version and worker Python/platform.
A missing returned model remains unknown. The bridge retains structured findings rather
than native conversation transcripts; worker staging files should be kept private.

Fresh contexts and bridge visibility rules provide independence from other review
submissions. They do not provide operating-system isolation from a hostile local client,
a model's complete runtime environment, or arbitrary approved check code. Antigravity's
sandbox is a terminal restriction, not a general tool-isolation guarantee.

## Author response, revision and follow-up

Once both initial slots finish, the phase becomes `responding`. Create an author invitation:

```bash
research-journal review invite REVIEW_ID --role author
```

Using that token, call `review_respond` once with a mapping from every initial
`slot:finding-id` to `{"disposition":"accepted|rejected|unresolved","reason":"..."}`.
An empty object is required when neither reviewer found an issue. The CLI equivalent is
`review respond REVIEW_ID responses.json --message-id RESPONSE_ID`.

Optionally change the source and freeze one revision:

```bash
research-journal review revise REVIEW_ID /path/to/source
```

This keeps the phase at `responding`, permitting approved checks on version 2. A changed
command, executor, resources, environment, setup script or explicitly invoked `.py`/`.sh`
script requires approval again. Changing the reviewed data alone permits reusing an
approved command; each packet version gets a distinct attempt. A confirmed failed,
cancelled or timed-out check needs an explicit `review check retry REVIEW_ID JOB_ID`.
An unknown submission cannot be retried until reconciled.

When the response and check evidence are ready:

```bash
research-journal review follow-up REVIEW_ID
research-journal review run REVIEW_ID
```

For manual slots, issue new invitations and repeat join/claim/packet/submit in fresh
conversations. Both slots receive the same revision, initial findings, author responses
and sealed evidence. There is one follow-up round; no further revision or new checks.

## Human decision

After follow-up, `awaiting_closure` waits for the human CLI:

```bash
research-journal review close REVIEW_ID --outcome completed \
  --rationale "Both reviewers examined the revised artifact" --resolutions resolutions.json
```

Resolution keys are `round:slot:finding-id`. Each is `open`, `addressed`, or `verified`.
Verification requires an evidence reference, an explanation of how that evidence meets
the finding, and the current packet `version`. Omitted findings stay open. Completing
the protocol does not imply that every scientific claim or finding has been verified,
and it never completes TODO checkboxes automatically.

Other outcomes are `incomplete`, `failed`, and `cancelled`, each with a rationale.
`completed` requires both reviewers to have submitted in both rounds and no pending jobs.
For an interrupted review use `review cancel REVIEW_ID --reason "..."`; it requests
cancellation of matching active job handles before closing. Pending cancellations and
late results remain in history and cannot reopen a closed decision. Use `review poll`
after closure to collect final cancellation evidence. `review cancel REVIEW_ID --job JOB_ID`
cancels only that attempt.

Approvals, invitation creation, missing/retry decisions, import/export, reconciliation,
and final closure are deliberately absent from the participant MCP surface. The CLI
is an owner interface; it assumes a trusted person controls its use.

## SSH, Slurm and long jobs

The remote executor examples require existing SSH host aliases, batch authentication,
a writable dedicated staging directory and Python 3 on the remote host. Slurm also needs
`sbatch`, `squeue`, `sacct`, `scancel`, scheduler accounting and an explicit `resources.time`.
Allowed resources are `time`, `mem`, `cpus-per-task`, `partition`, `account`, and `gres`.
Remote Python/environment selection is part of the executor approval. Use a frozen
`setup` script to source a virtual environment or load modules when needed.

The coordinator sends JSON and file contents over SSH; approved argv values are not
interpolated into a remote shell command. Each attempt gets a unique staging directory
and a durable process/scheduler handle. Scheduler submission alone is never success:
completion requires matching accounting and collected result evidence. Accounting lag
or lost transport leaves the attempt `unknown`. Ambiguous `sbatch` submissions are
looked up by unique attempt name and never automatically resubmitted.

`review poll` observes once. `review watch --timeout SECONDS` polls in the foreground
and returns on terminal jobs or its deadline. Closing the watcher leaves work durable;
run either command later, even after restarting the coordinator. There is no background
polling service. Time limits apply in the worker and in Slurm resources; scheduler queue
time is separate. Stdout/stderr are bounded to 1 MiB each, selected artifacts to 16 MiB.

For an unresolved attempt, first inspect the real worker or scheduler. Then supply
`review reconcile REVIEW_ID --decisions decisions.json`, for example:

```json
{
  "ATTEMPT_ID": {
    "state": "failed",
    "evidence": "Scheduler search for rj-ATTEMPT_ID returned no job; staging host confirmed no worker",
    "reason": "Verified that the interrupted launch did not start work"
  }
}
```

Only `failed` or `cancelled` can be assigned this way; human reconciliation cannot
manufacture successful evidence. Cancel only verified handles, and never declare a
possibly running job absent just to permit a retry.

## Obsidian and recovery

`Research Journal/Reviews/Index.md` links reviews and decisions from the Home dashboard.
Individual notes show packet hashes, phase, revealed findings, responses, check approvals,
bounded evidence excerpts, artifacts and human resolutions. Unassigned reviews also get
an inbox link. Write annotations outside the generated section; refresh preserves them.
SQLite and hashed input objects are authoritative. Editing a note cannot approve a
command, submit findings, change protocol state, or close a review.

```bash
research-journal review export REVIEW_ID /private/path/review-bundle.json
research-journal --config recovered-config.json review import /private/path/review-bundle.json
research-journal --config recovered-config.json review poll REVIEW_ID
research-journal --config recovered-config.json review reconcile REVIEW_ID --resume
```

Bundles contain versioned review state, events, manifests, input objects and bounded
job evidence, with an integrity hash. Invitation capabilities and session receipts are
excluded. A bundle is not encrypted or signed; treat it as private research data and
import only trusted bundles. The destination must not contain a different review with
the same ID. Active imports are paused; existing remote handles can be polled, and
unresolved work must be settled before `--resume`. Import never launches work. A local
attempt copied to another machine generally requires checking the original host and
reconciliation. Manual capabilities must be reissued after recovery. Bundle size is
limited to 128 MiB.

If an Obsidian projection fails, the accepted state transition remains committed and
the CLI reports `projection_error`. Repair the generated markers and run
`review status` to inspect state; `review refresh REVIEW_ID` rebuilds the projection.
Keep the state directory along with your vault backups. Staging cleanup is manual;
do not remove active attempt directories.

## Validation scope

The automated suite exercises manual and fake automatic reviewers, both rounds,
authority and embargo, approvals and script changes, duplicates and stale attempts,
concurrent claims, local processes, cancellation/timeouts/output bounds, SSH argv
transport, fake Slurm submission/accounting/cancellation, and bundle recovery.
`review doctor` probes local CLI help without inference or remote calls. Real model
outputs, remote Python environments, SSH connectivity and your scheduler configuration
must be verified in your own environment; passing mocked contracts does not establish
that those integrations have run successfully.
