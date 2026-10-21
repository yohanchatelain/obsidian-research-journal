"""Portable configuration; importing this module never creates files."""

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo


def default_config() -> Path:
    return Path(os.environ.get("RJ_CONFIG", "~/.config/research-journal/config.json")).expanduser()


@dataclass
class Config:
    vault: Path
    state: Path
    folder: str = "Research Journal"
    todo: str = "TODO list.md"
    timezone: str = "America/Toronto"
    projects: dict = field(default_factory=dict)
    exclude: list[str] = field(default_factory=lambda: ["Trash", "Templates"])
    review: dict = field(default_factory=dict)

    def __post_init__(self):
        self.vault = self.vault.expanduser().resolve()
        self.state = self.state.expanduser().resolve()
        if not isinstance(self.review, dict):
            raise ValueError("review must be an object")
        if not isinstance(self.review.get("reviewers", {}), dict) or not isinstance(self.review.get("executors", {}), dict):
            raise ValueError("Review reviewers and executors must be named objects")
        ZoneInfo(self.timezone)
        self.path(self.folder)
        self.path(self.todo)
        for key, project in self.projects.items():
            if not isinstance(project.get("name"), str) or not project["name"].strip():
                raise ValueError(f"Project {key} needs a name")
            for note in project.get("notes", []):
                self.path(note)

    def path(self, relative: str) -> Path:
        """Confine note operations to the vault, including symlink resolution."""
        raw = Path(relative)
        target = (self.vault / raw).resolve()
        if raw.is_absolute() or not target.is_relative_to(self.vault) or target == self.vault:
            raise ValueError(f"Path must be inside the vault: {relative}")
        if any(part.startswith(".") for part in raw.parts):
            raise ValueError("Hidden vault files cannot be journal targets")
        return target

    def project_for(self, workspace: str) -> str | None:
        if not workspace:
            return None
        workspace_path = Path(workspace).expanduser().resolve()
        matches = []
        for key, project in self.projects.items():
            for root in project.get("workspaces", []):
                root_path = Path(root).expanduser().resolve()
                if workspace_path.is_relative_to(root_path):
                    matches.append((len(root_path.parts), key))
        return max(matches)[1] if matches else None

    def to_dict(self) -> dict:
        return {"vault": str(self.vault), "state": str(self.state), "folder": self.folder,
                "todo": self.todo, "timezone": self.timezone, "projects": self.projects,
                "exclude": self.exclude, "review": self.review}

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        config_path = path or default_config()
        data = json.loads(config_path.read_text())
        vault = Path(data["vault"]).expanduser().resolve()
        vault_key = hashlib.sha256(str(vault).encode()).hexdigest()[:12]
        state = Path(data.get("state", f"~/.local/state/research-journal/{vault_key}"))
        return cls(vault=vault, state=state, **{k: v for k, v in data.items()
                   if k in {"folder", "todo", "timezone", "projects", "exclude", "review"}})
