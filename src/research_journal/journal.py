"""Notebook operations, exact task updates, and rebuildable Obsidian views."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import Config
from .storage import Conflict, atomic_write, locked, read
from .tasks import BLOCK, CHECKBOX, add_under_date, parse_tasks

BEGIN = "<!-- research-journal:begin -->"
END = "<!-- research-journal:end -->"
SECTIONS = ("goals", "methods", "findings", "failed_attempts", "decisions", "evidence",
            "blockers", "next_steps")


def slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")[:70] or "note"


def short_id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def link(path: str, label: str | None = None) -> str:
    clean = path.removesuffix(".md").replace("|", "").replace("]]", "")
    if label:
        label = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m[2] or m[1], label)
        label = label.replace("|", "").replace("[", "").replace("]", "").replace("\n", " ")
        return f"[[{clean}|{label}]]"
    return f"[[{clean}]]"


def metadata(**values) -> str:
    return "---\n" + "".join(f"{key}: {json.dumps(value, ensure_ascii=False)}\n"
                              for key, value in values.items()) + "---\n\n"


def _within(recorded: str, workspace: str) -> bool:
    """A session belongs to a workspace when it ran there or below it (e.g. a git worktree)."""
    return bool(recorded) and Path(recorded).is_relative_to(Path(workspace))


class Journal:
    def __init__(self, config: Config):
        self.config = config

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.config.timezone))

    def _note(self, relative: str) -> Path:
        return self.config.path(f"{self.config.folder}/{relative}")

    def _relative(self, path: Path) -> str:
        return str(path.relative_to(self.config.vault))

    def _files(self):
        # Do not follow symlink directories, hidden application data, or Trash.
        import os
        for root, dirs, files in os.walk(self.config.vault, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                             and d.casefold() not in {s.casefold() for s in self.config.exclude}
                             and not (Path(root) / d).is_symlink())
            for filename in sorted(files):
                path = Path(root) / filename
                if filename.endswith(".md") and not path.is_symlink():
                    yield path

    def _tasks(self, query="", include_done=False):
        tasks = []
        for path in self._files():
            if path.is_relative_to(self._note("Sessions")) or path.is_relative_to(self._note("Templates")):
                continue
            for task in parse_tasks(read(path) or "", self._relative(path)):
                if (include_done or not task.done) and query.casefold() in task.text.casefold():
                    tasks.append(task.to_dict())
        return tasks

    def search_tasks(self, query="", include_done=False):
        with locked(self.config.state):
            return self._tasks(query, include_done)

    def _stage(self, db, event_id: str, path: Path, after: str):
        before = read(path)
        if before != after:
            db.execute("INSERT INTO writes(event_id,path,before_text,after_text) VALUES(?,?,?,?)",
                       (event_id, self._relative(path), before, after))

    def _apply(self, db, event_id: str):
        db.commit()  # Persist the plan before touching any Markdown.
        for row in db.execute("SELECT * FROM writes WHERE event_id=? AND status='pending' ORDER BY rowid",
                              (event_id,)).fetchall():
            path = self.config.path(row["path"])
            current = read(path)
            if current == row["after_text"]:
                pass  # File survived a crash after replacement but before ledger commit.
            elif current == row["before_text"]:
                atomic_write(path, row["after_text"], current)
            else:
                raise Conflict(f"Edited since queued write: {row['path']}")
            db.execute("UPDATE writes SET status='applied' WHERE event_id=? AND path=?",
                       (event_id, row["path"]))
            db.commit()
        db.execute("UPDATE events SET status='applied', error=NULL WHERE id=?", (event_id,))
        db.commit()

    def _event(self, event_id: str | None, payload: dict, prepare):
        event_id = event_id or uuid4().hex
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        with locked(self.config.state) as db:
            previous = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if previous:
                if previous["payload"] != encoded:
                    raise ValueError("Event ID already used with different content")
                if previous["status"] == "applied" and payload["kind"] == "progress":
                    self._mark_checkpoint(db, payload["client"], payload["session_id"], payload.get("turn"))
                view_error = self._refresh_safely(db)
                return {"event_id": event_id, "status": previous["status"],
                        "duplicate": True, "error": previous["error"], "view_error": view_error}
            db.execute("INSERT INTO events(id,payload,status,created) VALUES(?,?,'pending',?)",
                       (event_id, encoded, self.now().isoformat()))
            try:
                result = prepare(db, event_id) or {}
            except Exception:
                db.rollback()
                raise
            try:
                self._apply(db, event_id)
            except (OSError, Conflict) as error:
                db.execute("UPDATE events SET error=? WHERE id=?", (str(error), event_id))
                db.commit()
                return {"event_id": event_id, "status": "pending", "error": str(error)}
            if result.get("checkpoint"):
                self._mark_checkpoint(db, **result["checkpoint"])
            view_error = self._refresh_safely(db)
            return {"event_id": event_id, "status": "applied", **result,
                    **({"view_error": view_error} if view_error else {})}

    def _validate_project(self, project):
        if project is not None and project not in self.config.projects:
            raise ValueError(f"Unknown project: {project}; register it first or omit project")

    def _session_path(self, client, session_id):
        for session in self._progress():
            if session["client"] == client and session["session_id"] == session_id:
                return self.config.path(session["path"])
        return self._note(f"Sessions/{slug(client)}-{short_id(session_id)}.md")

    def record_progress(self, title: str, sections: dict, *, client="manual", session_id="",
                        project=None, workspace="", source="", event_id=None, turn=None):
        if not title.strip() or not any(sections.values()):
            raise ValueError("A title and at least one notebook section are required")
        if set(sections) - set(SECTIONS) or not all(isinstance(v, str) for v in sections.values()):
            raise ValueError(f"Sections must be strings from: {', '.join(SECTIONS)}")
        project = project or self.config.project_for(workspace)
        self._validate_project(project)
        session_id = session_id or ("capture-" + short_id(event_id) if event_id else uuid4().hex)
        payload = dict(kind="progress", title=title, sections=sections, client=client,
                       session_id=session_id, project=project, workspace=workspace,
                       source=source, turn=turn)

        def prepare(db, eid):
            now = self.now()
            path = self._session_path(client, session_id)
            content = read(path) or (metadata(type="research-session", client=client,
                        session_id=session_id, project=project, workspace=workspace,
                        created=now.isoformat()) + f"# {title}\n")
            content += f"\n## {now:%Y-%m-%d %H:%M} — {title}\n\n"
            for section in SECTIONS:
                if sections.get(section):
                    content += f"### {section.replace('_', ' ').title()}\n\n{sections[section].strip()}\n\n"
            if source:
                content += f"Source: {source}\n\n"
            content += f"<!-- rj-event:{eid} -->\n"
            self._stage(db, eid, path, content)
            day = self._note(f"Daily/{now:%Y-%m-%d}.md")
            daily = read(day) or f"# {now:%Y-%m-%d}\n\n## Research sessions\n"
            session_link = link(self._relative(path))
            target_prefix = f"[[{self._relative(path).removesuffix('.md')}"
            if target_prefix not in daily:
                daily += f"\n- {link(self._relative(path), title)}\n"
                self._stage(db, eid, day, daily)
            if not project:
                inbox = self._note(f"Inbox/Unassigned-{slug(client)}-{short_id(session_id)}.md")
                if not inbox.exists():
                    self._stage(db, eid, inbox, metadata(type="unassigned-session") +
                                f"# Unassigned session\n\n{session_link}\n\nWorkspace: {workspace or 'General'}\n")
            return {"path": self._relative(path), "checkpoint":
                    {"client": client, "session_id": session_id, "turn": turn}}
        return self._event(event_id, payload, prepare)

    def capture_idea(self, title: str, motivation: str, next_steps="", *, project=None,
                     source="", event_id=None):
        if not title.strip() or not motivation.strip():
            raise ValueError("An idea needs a title and motivation")
        self._validate_project(project)
        payload = dict(kind="idea", title=title, motivation=motivation, next_steps=next_steps,
                       project=project, source=source)

        def prepare(db, eid):
            path = self._note(f"Ideas/{slug(title)}-{short_id(eid)}.md")
            body = metadata(type="idea", project=project, created=self.now().isoformat())
            body += f"# {title}\n\n## Motivation\n\n{motivation}\n"
            if next_steps:
                body += f"\n## Possible next steps\n\n{next_steps}\n"
            if source:
                body += f"\nSource: {source}\n"
            self._stage(db, eid, path, body)
            return {"path": self._relative(path)}
        return self._event(event_id, payload, prepare)

    def add_task(self, text: str, *, project=None, event_id=None):
        if not text.strip() or "\n" in text or "\r" in text or BLOCK.search(text):
            raise ValueError("Task must be one nonempty line without a block ID")
        self._validate_project(project)

        def prepare(db, eid):
            path = self.config.path(self.config.todo)
            task_text = text
            if project:
                task_text += " " + link(self._relative(self._project_path(project)),
                                       self.config.projects[project]["name"])
            block_id = "rj-" + short_id(eid)
            self._stage(db, eid, path, add_under_date(read(path) or "", f"{self.now():%Y-%m-%d}",
                                                    task_text, block_id))
            return {"path": self.config.todo, "block_id": block_id}
        return self._event(event_id, dict(kind="task", text=text, project=project), prepare)

    def _proposal(self, db, eid, reason, details):
        path = self._note(f"Inbox/Task-update-{short_id(eid)}.md")
        self._stage(db, eid, path, metadata(type="task-proposal") +
                    f"# Task update needs review\n\n{reason}\n\n```json\n" +
                    json.dumps(details, indent=2, ensure_ascii=False) + "\n```\n")
        return {"review_required": True, "reason": reason, "path": self._relative(path)}

    def update_task(self, task: dict, *, done=True, evidence="", reason="", event_id=None):
        """Use a task returned by search_tasks; never match on wording alone."""
        relative = task.get("path", "")
        path = self.config.path(relative)
        if path.suffix != ".md" or path.is_relative_to(self._note("Templates")):
            raise ValueError("Task target must be a Markdown note")
        payload = dict(kind="task-update", task=task, done=done, evidence=evidence, reason=reason)

        def prepare(db, eid):
            content = read(path)
            candidates = parse_tasks(content or "", relative)
            if task.get("block_id"):
                matches = [t for t in candidates if t.block_id == task["block_id"]]
            else:
                matches = [t for t in candidates if t.line == task.get("line")
                           and t.fingerprint == task.get("fingerprint")]
            if len(matches) != 1 or matches[0].text != task.get("text"):
                return self._proposal(db, eid, "Task moved, changed, or cannot be identified uniquely.", payload)
            found = matches[0]
            if found.done == done:
                return {"already_done": done, "path": relative}
            if (done and (not evidence.strip() or not reason.strip() or found.open_children)):
                return self._proposal(db, eid,
                                      "Completion needs evidence, an explanation, and no open subtasks.", payload)
            lines = content.splitlines(keepends=True)
            line = lines[found.line - 1]
            newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            original = line.rstrip("\r\n")
            block_id = found.block_id or "rj-" + short_id(eid)
            match = CHECKBOX.match(original)
            start, stop = match.span("state")
            updated = original[:start] + ("x" if done else " ") + original[stop:]
            if not found.block_id:
                updated += f" ^{block_id}"
            lines[found.line - 1] = updated + newline
            self._stage(db, eid, path, "".join(lines))
            audit = self._note(f"Sessions/Task-update-{short_id(eid)}.md")
            self._stage(db, eid, audit, metadata(type="task-update", created=self.now().isoformat()) +
                        f"# Task {'completed' if done else 'reopened'}\n\n" +
                        link(relative.removesuffix(".md") + f"#^{block_id}", found.text) +
                        f"\n\nReason: {reason}\n\nEvidence: {evidence}\n")
            return {"path": relative, "block_id": block_id}
        return self._event(event_id, payload, prepare)

    def _project_path(self, project):
        return self._note(f"Projects/{slug(project)}-{short_id(project)[:6]}.md")

    def link_task(self, task: dict, project: str, *, event_id=None):
        """Associate an existing checkbox with a project without moving it."""
        self._validate_project(project)
        path = self.config.path(task.get("path", ""))
        if path.suffix != ".md":
            raise ValueError("Task target must be Markdown")
        payload = {"kind": "task-link", "task": task, "project": project}

        def prepare(db, eid):
            content = read(path) or ""
            candidates = parse_tasks(content, task["path"])
            matches = [candidate for candidate in candidates if
                (candidate.block_id == task["block_id"] if task.get("block_id") else
                 candidate.line == task.get("line") and candidate.fingerprint == task.get("fingerprint"))
                and candidate.text == task.get("text")]
            if len(matches) != 1:
                return self._proposal(db, eid, "Task changed before project assignment.", payload)
            found = matches[0]
            project_link = link(self._relative(self._project_path(project)), self.config.projects[project]["name"])
            if project_link in found.text:
                return {"path": task["path"], "already_linked": True}
            lines = content.splitlines(keepends=True)
            line = lines[found.line - 1]
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            block_id = found.block_id or "rj-" + short_id(eid)
            lines[found.line - 1] = BLOCK.sub("", line.rstrip("\r\n")) + f" {project_link} ^{block_id}{ending}"
            self._stage(db, eid, path, "".join(lines))
            return {"path": task["path"], "block_id": block_id}
        return self._event(event_id, payload, prepare)

    def assign_session(self, path: str, project: str, *, event_id=None):
        self._validate_project(project)
        target = self.config.path(path)
        if not target.is_relative_to(self._note("Sessions")) or target.suffix != ".md":
            raise ValueError("Choose a session note inside Research Journal/Sessions")
        payload = {"kind": "session-assignment", "path": path, "project": project}

        def prepare(db, eid):
            current = read(target) or ""
            if not current.startswith("---\n") or "\nproject:" not in current.split("---\n", 2)[1]:
                raise ValueError("Session note lacks project metadata")
            updated = re.sub(r"(?m)^project:.*$", "project: " + json.dumps(project), current, count=1)
            self._stage(db, eid, target, updated)
            for inbox in self._note("Inbox").glob("Unassigned-*.md"):
                if inbox.is_symlink():
                    continue
                inbox_content = read(inbox) or ""
                if link(path) in inbox_content and "status:" not in inbox_content.split("---\n", 2)[1]:
                    self._stage(db, eid, inbox, inbox_content.replace("---\n", '---\nstatus: "resolved"\n', 1))
            return {"path": path, "project": project}
        return self._event(event_id, payload, prepare)

    def _managed(self, path: Path, title: str, body: str):
        current = read(path)
        block = f"{BEGIN}\n{body.rstrip()}\n{END}"
        if current is None:
            updated = f"# {title}\n\n{block}\n"
        elif BEGIN in current and END in current:
            start = current.index(BEGIN)
            end = current.index(END, start) + len(END)
            updated = current[:start] + block + current[end:]
        elif BEGIN in current or END in current:
            raise Conflict(f"Broken generated-section markers: {self._relative(path)}")
        else:
            updated = current.rstrip() + "\n\n" + block + "\n"
        if updated != current:
            atomic_write(path, updated, current)

    def _refresh(self, db):
        tasks = self._tasks()
        progress = self._progress()
        body = "## Open tasks\n\n"
        for task in tasks:
            anchor = f"#^{task['block_id']}" if task["block_id"] else f"#{task['heading']}" if task["heading"] else ""
            label = task["text"] + (f" — {task['heading']}" if task["heading"] else "")
            body += "- " + link(task["path"].removesuffix(".md") + anchor, label) + "\n"
        body += "\n## Projects\n\n"
        for key, project in sorted(self.config.projects.items()):
            path = self._project_path(key)
            body += "- " + link(self._relative(path), project["name"]) + "\n"
            recent = [p for p in progress if p.get("project") == key]
            project_body = "## Current state\n\n"
            if recent:
                p = recent[0]
                project_body += f"{p['title']}\n\n"
                for section in ("findings", "blockers", "next_steps"):
                    if p["sections"].get(section):
                        project_body += f"**{section.replace('_', ' ').title()}:** {p['sections'][section]}\n\n"
            else:
                project_body += "No recorded work yet.\n\n"
            project_body += "## Existing notes\n\n" + "\n".join(
                "- " + link(note) for note in project.get("notes", []))
            project_body += "\n\n## Open tasks\n\n" + "\n".join(
                "- " + link(t["path"], t["text"]) for t in tasks if link(self._relative(path)) in t["text"]
                or f"[[{self._relative(path).removesuffix('.md')}|" in t["text"])
            project_body += "\n\n## Recent progress\n\n" + "\n".join(
                "- " + link(p["path"], p["title"])
                for p in recent[:10])
            self._managed(path, project["name"], project_body)
        body += "\n## Recent progress\n\n" + "\n".join(
            "- " + link(p["path"], p["title"])
            for p in progress[:20])
        for directory, heading in (("Ideas", "Ideas"), ("Inbox", "Needs review")):
            body += f"\n\n## {heading}\n\n"
            body += "\n".join("- " + link(self._relative(p)) for p in sorted(self._note(directory).glob("*.md"))
                              if not p.is_symlink() and not re.search(
                                  r'(?m)^status: ["\x27]?resolved["\x27]?\s*$', read(p) or ""))
        pending = db.execute("SELECT id,error FROM events WHERE status='pending'").fetchall()
        gaps = db.execute("SELECT client,id,state FROM sessions WHERE saved<turn").fetchall()
        historical_gaps = db.execute("SELECT * FROM gaps ORDER BY created DESC").fetchall()
        body += "\n\n## Agent reviews\n\n" + link(f"{self.config.folder}/Reviews/Index.md", "Review rounds and decisions") + "\n"
        body += "\n\n## Capture status\n\n"
        body += f"Pending writes: {len(pending)}. Incomplete sessions: {len(gaps)}.\n"
        body += f"Unresolved capture gaps: {len(historical_gaps)}.\n"
        body += "\n".join(f"- {r['id']}: {r['error'] or 'pending'}" for r in pending)
        body += "\n".join(f"\n- {r['client']} session {r['id']}: {r['state']}" for r in gaps)
        body += "\n".join(f"\n- {r['client']} session {r['id']}, turn {r['turn']}: {r['reason']}"
                          for r in historical_gaps)
        self._managed(self._note("Home.md"), "Research journal", body)

    def refresh(self):
        with locked(self.config.state) as db:
            error = self._refresh_safely(db)
            if error:
                raise Conflict(error)
        return {"path": f"{self.config.folder}/Home.md"}

    def _refresh_safely(self, db):
        try:
            self._refresh(db)
            db.execute("DELETE FROM health WHERE key='view_error'")
            return None
        except (OSError, Conflict) as error:
            db.execute("INSERT OR REPLACE INTO health(key,value) VALUES('view_error',?)", (str(error),))
            db.commit()
            return str(error)

    def initialize(self):
        self.config.vault.mkdir(parents=True, exist_ok=True)
        with locked(self.config.state) as db:
            for directory in ("Daily", "Sessions", "Projects", "Ideas", "Inbox", "Templates", "Reviews"):
                self._note(directory).mkdir(parents=True, exist_ok=True)
            templates = {"Research session": "## Goals\n\n## Methods\n\n## Findings\n\n## Failed attempts\n\n## Decisions\n\n## Evidence\n\n## Blockers\n\n## Next steps\n",
                         "Idea": "## Motivation\n\n## Possible next steps\n\n## Source\n",
                         "Daily": "## Plan\n\n## Research sessions\n\n## Reflections\n"}
            for name, body in templates.items():
                path = self._note(f"Templates/{name}.md")
                if not path.exists():
                    atomic_write(path, f"# {name}\n\n{body}", None)
            self._refresh(db)
        return {"path": f"{self.config.folder}/Home.md"}

    def get_context(self, workspace="", project=None):
        project = project or self.config.project_for(workspace)
        self._validate_project(project)
        with locked(self.config.state) as db:
            recent = []
            for payload in self._progress():
                if (payload.get("project") == project if project
                        else bool(workspace) and _within(payload.get("workspace", ""), workspace)):
                    path = self.config.path(payload["path"])
                    if str(path) not in [r["absolute_path"] for r in recent]:
                        recent.append({"absolute_path": str(path), "path": self._relative(path),
                                       "content": (read(path) or "")[-14000:]})
                    if len(recent) == 5:
                        break
            project_note = read(self._project_path(project)) if project else None
            notes = []
            if project:
                for relative in self.config.projects[project].get("notes", [])[:5]:
                    notes.append({"path": relative, "content": (read(self.config.path(relative)) or "")[:8000]})
            return {"project": project, "project_note": project_note, "existing_notes": notes,
                    "recent_sessions": recent, "tasks": self._tasks()[:100],
                    "instructions": "Retrieve an exact task before updating it. Record evidence and next steps."}

    def _progress(self):
        """Rebuild views from the current Markdown, including handwritten changes."""
        result = []
        for path in self._note("Sessions").glob("*.md"):
            if path.is_symlink():
                continue
            content = (read(path) or "").replace("\r\n", "\n")
            if not content.startswith("---\n"):
                continue
            pieces = content.split("---\n", 2)
            if len(pieces) < 3:
                continue
            data = {}
            for line in pieces[1].splitlines():
                key, separator, value = line.partition(":")
                if separator:
                    try:
                        data[key] = json.loads(value.strip())
                    except json.JSONDecodeError:
                        data[key] = value.strip().strip("'\"")
            if data.get("type") != "research-session" or not all(
                    isinstance(data.get(k), str) and data[k] for k in ("client", "session_id")):
                continue
            body = pieces[2]
            checkpoints = re.split(r"(?m)^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) — (.*)\n", body)
            if len(checkpoints) < 4:
                continue
            date, title, latest = checkpoints[-3:]
            sections = {}
            for section in SECTIONS:
                heading = re.escape(section.replace("_", " ").title())
                match = re.search(r"(?ms)^### " + heading + r"\s*\n(.*?)(?=^### |\Z)", latest)
                if match:
                    sections[section] = re.sub(r"(?m)^<!-- rj-event:.*?-->\s*$", "", match[1]).strip()
            result.append({**data, "title": title, "sections": sections, "updated": date,
                           "path": self._relative(path)})
        return sorted(result, key=lambda p: (p["updated"], p["path"]), reverse=True)

    def _mark_checkpoint(self, db, client, session_id, turn=None):
        row = db.execute("SELECT turn FROM sessions WHERE client=? AND id=?", (client, session_id)).fetchone()
        if row is not None and turn is not None:
            db.execute("UPDATE sessions SET saved=MAX(saved,?),updated=? WHERE client=? AND id=?",
                       (min(int(turn), row[0]), self.now().isoformat(), client, session_id))
            db.execute("DELETE FROM gaps WHERE client=? AND id=? AND turn=?", (client, session_id, turn))
            db.commit()

    def checkpoint(self, client, session_id, turn: int, nothing_to_record=False):
        with locked(self.config.state) as db:
            if not nothing_to_record:
                raise ValueError("Use record_progress to save work, or acknowledge nothing_to_record")
            row = db.execute("SELECT turn FROM sessions WHERE client=? AND id=?", (client, session_id)).fetchone()
            if row is None or row[0] != turn:
                raise ValueError("Unknown or stale journal turn; use the current hook context")
            self._mark_checkpoint(db, client, session_id, turn)
        return {"status": "acknowledged", "turn": turn}

    def status(self):
        with locked(self.config.state) as db:
            pending = [dict(r) for r in db.execute("SELECT id,created,error FROM events WHERE status='pending'")]
            sessions = [dict(r) for r in db.execute("SELECT * FROM sessions WHERE saved<turn ORDER BY updated DESC")]
            health = dict(db.execute("SELECT key,value FROM health").fetchall())
            gaps = [dict(r) for r in db.execute("SELECT * FROM gaps ORDER BY created DESC")]
            return {"pending_writes": pending, "incomplete_sessions": sessions,
                    "capture_gaps": gaps, **health}

    def inspect_event(self, event_id):
        with locked(self.config.state) as db:
            row = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown event ID")
            return {**dict(row), "payload": json.loads(row["payload"]), "writes": [dict(r) for r in
                db.execute("SELECT path,before_text,after_text,status FROM writes WHERE event_id=?", (event_id,))]}

    def discard_event(self, event_id):
        with locked(self.config.state) as db:
            row = db.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
            if row is None or row[0] != "pending":
                raise ValueError("Only pending events can be discarded")
            db.execute("UPDATE events SET status='discarded' WHERE id=?", (event_id,))
            self._refresh_safely(db)
        return {"event_id": event_id, "status": "discarded", "note": "Already applied writes were retained."}

    def retry(self):
        results = []
        with locked(self.config.state) as db:
            for row in db.execute("SELECT * FROM events WHERE status='pending' ORDER BY rowid").fetchall():
                try:
                    self._apply(db, row["id"])
                    payload = json.loads(row["payload"])
                    if payload["kind"] == "progress":
                        self._mark_checkpoint(db, payload["client"], payload["session_id"], payload.get("turn"))
                    results.append({"id": row["id"], "status": "applied"})
                except (OSError, Conflict, ValueError) as error:
                    db.execute("UPDATE events SET error=? WHERE id=?", (str(error), row["id"]))
                    # Durable conflict proposal; original pending write remains available for review.
                    if isinstance(error, Conflict):
                        path = self._note(f"Inbox/Write-conflict-{short_id(row['id'])}.md")
                        if not path.exists():
                            atomic_write(path, f"# Write conflict\n\nEvent: {row['id']}\n\n{error}\n\n"
                                         "The current note was preserved. Inspect the pending event before retrying.\n", None)
                    results.append({"id": row["id"], "status": "pending", "error": str(error)})
            db.commit()
            self._refresh_safely(db)
        return results
