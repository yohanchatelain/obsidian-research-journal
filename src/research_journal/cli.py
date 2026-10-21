"""Command line entrypoint for setup, quick capture, and assistant integrations."""

import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tomllib

from .config import Config, default_config
from .journal import Journal
from .storage import Conflict, atomic_write, read


def parser():
    root = argparse.ArgumentParser(description="Connect Obsidian to Codex, Claude Code, and Antigravity")
    root.add_argument("--config", type=Path, default=default_config())
    commands = root.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Preview settings; add --apply to install")
    setup.add_argument("--vault", type=Path)
    setup.add_argument("--state", type=Path)
    setup.add_argument("--home", type=Path, default=Path.home())
    setup.add_argument("--apply", action="store_true")
    setup.add_argument("--output", type=Path, help="Save a credential-free installation preview")
    init = commands.add_parser("init", help="Initialize a vault without changing assistant settings")
    init.add_argument("--vault", type=Path)
    init.add_argument("--state", type=Path)
    for name in ("refresh", "retry", "status", "doctor", "uninstall"):
        commands.add_parser(name)
    for name in ("event", "discard"):
        command = commands.add_parser(name, help="Inspect a queued event or discard its remaining writes")
        command.add_argument("event_id")
    project = commands.add_parser("project", help="Register or update a project's workspace mappings")
    project.add_argument("key")
    project.add_argument("--name", required=True)
    project.add_argument("--workspace", action="append", default=[])
    project.add_argument("--note", action="append", default=[])
    tasks = commands.add_parser("tasks")
    tasks.add_argument("query", nargs="?", default="")
    tasks.add_argument("--all", action="store_true")
    add = commands.add_parser("task", help="Quick-capture a task")
    add.add_argument("text")
    add.add_argument("--project")
    add.add_argument("--event-id")
    idea = commands.add_parser("idea", help="Quick-capture a future project idea")
    idea.add_argument("title")
    idea.add_argument("--why", required=True)
    idea.add_argument("--next", default="")
    idea.add_argument("--project")
    idea.add_argument("--source", default="")
    idea.add_argument("--event-id")
    progress = commands.add_parser("progress", help="Quick-capture progress or read structured JSON from stdin")
    progress.add_argument("title", nargs="?")
    progress.add_argument("--finding", default="")
    progress.add_argument("--next", default="")
    progress.add_argument("--project")
    progress.add_argument("--source", default="")
    progress.add_argument("--workspace", default=str(Path.cwd()))
    progress.add_argument("--event-id")
    progress.add_argument("--json", action="store_true")
    update = commands.add_parser("update-task", help="Read a search_tasks reference from stdin")
    update.add_argument("--evidence", default="")
    update.add_argument("--reason", default="")
    update.add_argument("--reopen", action="store_true")
    update.add_argument("--event-id")
    assign = commands.add_parser("assign-session", help="Assign an existing session note to a project")
    assign.add_argument("path")
    assign.add_argument("--project", required=True)
    associate = commands.add_parser("link-task", help="Read a task reference from stdin and link it to a project")
    associate.add_argument("--project", required=True)
    context = commands.add_parser("context")
    context.add_argument("--workspace", default=str(Path.cwd()))
    context.add_argument("--project")
    hook = commands.add_parser("hook", help="Lifecycle hook: consumes a client JSON payload on stdin")
    hook.add_argument("--client", choices=["codex", "claude", "antigravity"], required=True)
    hook.add_argument("--event", required=True)
    mcp = commands.add_parser("mcp", help="Start the local MCP stdio server")
    mcp.add_argument("--client", choices=["codex", "claude", "antigravity", "manual"], default="manual")
    from .reviews.cli import add_parser
    add_parser(commands)
    return root


def _configured_toml(actual, expected, key=""):
    """Match owned settings while allowing other servers and additional hook entries."""
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and actual.get("enabled") is not False
                and all(name in actual and _configured_toml(actual[name], value, name)
                        for name, value in expected.items()))
    if isinstance(expected, list) and key != "args":
        return (isinstance(actual, list)
                and all(any(_configured_toml(item, value) for item in actual) for value in expected))
    return actual == expected


def doctor(config, config_path):
    from .install import writable
    checks = {"config": str(config_path), "vault": str(config.vault),
              "vault_exists": config.vault.is_dir(), "vault_writable": writable(config.vault),
              "state_writable": writable(config.state), "python": sys.version.split()[0],
              "assistants": {name: shutil.which(name) for name in ("codex", "claude", "agy", "antigravity")},
              "dashboard_exists": config.path(f"{config.folder}/Home.md").exists()}
    manifest = config.state / "installation.json"
    checks["installed"] = manifest.exists()
    if manifest.exists():
        files = []
        for entry in json.loads(manifest.read_text())["files"]:
            content = read(Path(entry["path"]))
            configured = content is not None
            for operation in entry["operations"]:
                if operation["kind"] == "text-block" and Path(entry["path"]).suffix == ".toml":
                    try:
                        configured = configured and _configured_toml(
                            tomllib.loads(content or ""), tomllib.loads(operation["value"]))
                    except tomllib.TOMLDecodeError:
                        configured = False
                elif operation["kind"] in {"owned-file", "text-block"}:
                    configured = configured and operation["value"] in (content or "")
                elif operation["kind"].startswith("json-"):
                    try:
                        value = json.loads(content or "{}")
                        for key in operation["keys"]:
                            value = value[key]
                        configured = configured and (operation["value"] in value if
                            operation["kind"] == "json-append" else operation["value"] == value)
                    except (KeyError, TypeError, ValueError):
                        configured = False
            files.append({"path": entry["path"], "exists": content is not None, "configured": configured})
        checks["integration_files"] = files
    checks["client_verification"] = "Open /mcp and /hooks in each assistant to verify loading and hook trust."
    return checks


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in {"setup", "init"}:
            if args.config.exists():
                config = Config.load(args.config)
                if args.vault and args.vault.expanduser().resolve() != config.vault:
                    raise ValueError("--vault differs from the existing configuration")
                if args.state and args.state.expanduser().resolve() != config.state:
                    raise ValueError("--state differs from the existing configuration")
            else:
                if not args.vault:
                    raise ValueError("--vault is required for first-time setup")
                config = Config(args.vault, args.state or args.config.parent / "state")
            if args.command == "setup":
                from .install import apply, plan
                runner = Path(__file__).resolve().parents[2] / "bin/research-journal"
                if not runner.exists():
                    # Installed wheel: run the module through Python rather than locating a checkout.
                    runner = Path(shutil.which("research-journal") or sys.argv[0]).resolve()
                changes = plan(config, args.config, args.home, runner)
                result = {"changes": [change.review() for change in changes], "applied": False}
                if args.output:
                    atomic_write(args.output, json.dumps(result, indent=2) + "\n", read(args.output))
                if args.apply:
                    result = {"applied": True, **apply(changes, config)}
            else:
                if not args.config.exists():
                    atomic_write(args.config, json.dumps(config.to_dict(), indent=2) + "\n", None)
                result = Journal(config).initialize()
        else:
            config = Config.load(args.config)
            journal = Journal(config)
            match args.command:
                case "review":
                    from .reviews.cli import run
                    result = run(config, args)
                case "mcp":
                    from .server import Server
                    Server(journal, args.client).run()
                    return 0
                case "hook":
                    from .hooks import handle
                    result = handle(journal, args.client, args.event, json.load(sys.stdin))
                case "doctor":
                    result = doctor(config, args.config)
                case "uninstall":
                    from .install import uninstall
                    result = uninstall(config)
                case "refresh" | "retry" | "status":
                    result = getattr(journal, args.command)()
                case "event":
                    result = journal.inspect_event(args.event_id)
                case "discard":
                    result = journal.discard_event(args.event_id)
                case "project":
                    config.projects[args.key] = {"name": args.name,
                        "workspaces": [str(Path(p).expanduser().resolve()) for p in args.workspace],
                        "notes": args.note}
                    for note in args.note:
                        config.path(note)
                    original = read(args.config)
                    atomic_write(args.config, json.dumps(config.to_dict(), indent=2) + "\n", original)
                    result = journal.refresh()
                case "tasks":
                    result = journal.search_tasks(args.query, args.all)
                case "task":
                    result = journal.add_task(args.text, project=args.project, event_id=args.event_id)
                case "idea":
                    result = journal.capture_idea(args.title, args.why, args.next, project=args.project,
                                                 source=args.source, event_id=args.event_id)
                case "progress":
                    if args.json:
                        result = journal.record_progress(**json.load(sys.stdin))
                    else:
                        result = journal.record_progress(args.title or "Progress update",
                            {"findings": args.finding, "next_steps": args.next},
                            project=args.project, workspace=args.workspace, source=args.source,
                            event_id=args.event_id)
                case "update-task":
                    result = journal.update_task(json.load(sys.stdin), done=not args.reopen,
                        evidence=args.evidence, reason=args.reason, event_id=args.event_id)
                case "link-task":
                    result = journal.link_task(json.load(sys.stdin), args.project)
                case "assign-session":
                    result = journal.assign_session(args.path, args.project)
                case "context":
                    result = journal.get_context(args.workspace, args.project)
        print(json.dumps(result, indent=None if args.command == "hook" else 2, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, Conflict) as error:
        if args.command == "hook":
            print("{}")
            print(f"research-journal hook unavailable: {error}", file=sys.stderr)
            return 0  # Journaling must never prevent the user's work.
        print(f"research-journal: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
