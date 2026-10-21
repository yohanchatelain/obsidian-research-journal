from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from research_journal.config import Config
from research_journal.journal import Journal


@pytest.fixture
def journal(tmp_path):
    config = Config(tmp_path / "vault", tmp_path / "state", projects={
        "baseline": {"name": "Baseline research", "workspaces": [str(tmp_path / "work")],
                     "notes": ["Existing project.md"]}})
    config.vault.mkdir()
    (config.vault / "TODO list.md").write_text(
        "## 2026-10-21\n- [ ] Check results\n- [ ] Read paper\n"
        "## 2026-10-19\n- [ ] Check results\n- [x] Launch baseline\n"
        "- [ ] Parent experiment\n\t- [ ] Analyze permutation tests\n")
    (config.vault / "Existing project.md").write_text("# Existing project\nManual research context.\n")
    result = Journal(config)
    result.initialize()
    return result
