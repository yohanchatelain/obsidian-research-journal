from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from multiprocessing import get_context
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from research_journal.config import Config
from research_journal.journal import Journal
from research_journal.storage import Conflict, atomic_write, locked, read
from research_journal.tasks import parse_tasks


def test_exact_dated_task_and_evidence(journal):
    tasks = journal.search_tasks("Check results")
    assert len(tasks) == 2
    first, second = tasks
    result = journal.update_task(first, evidence="results/report.csv: AUC 0.81",
                                 reason="Compared completed baseline against the reference.")
    assert result["status"] == "applied"
    assert result["block_id"].startswith("rj-")
    all_tasks = journal.search_tasks("Check results", include_done=True)
    assert all_tasks[0]["done"]
    assert not all_tasks[1]["done"]
    assert all_tasks[1]["heading"] == second["heading"]
    assert "Evidence: results/report.csv" in next(journal._note("Sessions").glob("Task-update-*.md")).read_text()


@pytest.mark.parametrize("evidence,reason", [("", "Checked results"), ("report.csv", "")])
def test_missing_evidence_creates_proposal(journal, evidence, reason):
    task = journal.search_tasks("Check results")[0]
    result = journal.update_task(task, evidence=evidence, reason=reason)
    assert result["review_required"]
    assert not journal.search_tasks("Check results")[0]["done"]
    assert journal.config.path(result["path"]).exists()


def test_open_subtasks_prevent_parent_completion(journal):
    task = journal.search_tasks("Parent")[0]
    assert task["open_children"]
    result = journal.update_task(task, evidence="job submitted", reason="Launched experiment")
    assert result["review_required"]


def test_manual_edit_and_line_move_do_not_target_another_task(journal):
    task = journal.search_tasks("Read paper")[0]
    todo = journal.config.path(journal.config.todo)
    before = todo.read_text()
    todo.write_text(before.replace("- [ ] Read paper", "- [ ] New urgent task\n- [ ] Read paper"))
    result = journal.update_task(task, evidence="paper.pdf", reason="Read and summarized")
    assert result["review_required"]
    assert "- [ ] New urgent task" in todo.read_text()
    assert "- [ ] Read paper" in todo.read_text()


def test_stable_block_id_survives_line_move(journal):
    journal.add_task("Review exact result", event_id="exact")
    task = journal.search_tasks("Review exact")[0]
    path = journal.config.path(task["path"])
    path.write_text("A handwritten preamble.\n" + path.read_text())
    result = journal.update_task(task, evidence="report.csv", reason="Validated report")
    assert not result.get("review_required")
    assert journal.search_tasks("Review exact", True)[0]["done"]


def test_manual_completion_is_respected(journal):
    task = journal.search_tasks("Read paper")[0]
    path = journal.config.path(task["path"])
    path.write_text(path.read_text().replace("[ ] Read paper", "[x] Read paper"))
    result = journal.update_task(task, evidence="paper.pdf", reason="Read it")
    assert result["review_required"]  # Unanchored stale snapshot is preserved for review.
    assert "[x] Read paper" in path.read_text()


def test_duplicate_ids_are_ambiguous(journal):
    path = journal.config.path("Duplicate.md")
    path.write_text("- [ ] Same ^duplicate\n- [ ] Same ^duplicate\n")
    task = journal.search_tasks("Same")[0]
    assert journal.update_task(task, evidence="x", reason="x")["review_required"]
    assert "[x]" not in path.read_text()


def test_crlf_and_no_final_newline_preserved(journal):
    path = journal.config.path("Windows.md")
    path.write_bytes(b"## Test\r\n- [ ] Task\r\nHandwritten tail")
    task = journal.search_tasks("Task")[0]
    journal.update_task(task, evidence="output", reason="Verified output")
    body = path.read_bytes()
    assert body.startswith(b"## Test\r\n- [x] Task ^rj-")
    assert body.endswith(b"\r\nHandwritten tail")


def test_progress_idea_and_project_context(journal, tmp_path):
    result = journal.record_progress("Baseline launched", {
        "goals": "Compare two methods", "methods": "Run permutation tests",
        "findings": "Job running; no results yet", "failed_attempts": "First job timed out",
        "evidence": "scheduler job 123", "next_steps": "Inspect results"},
        client="codex", session_id="session-1", workspace=str(tmp_path / "work"), source="session-1")
    assert result["status"] == "applied"
    context = journal.get_context(str(tmp_path / "work"))
    assert context["project"] == "baseline"
    assert "Job running" in context["recent_sessions"][0]["content"]
    assert "Manual research context" in context["existing_notes"][0]["content"]
    assert "First job timed out" in context["recent_sessions"][0]["content"]
    assert len(journal.search_tasks("Check results")) == 2
    idea = journal.capture_idea("Compare stability", "Potential research question", "Try a small pilot",
                               project="baseline", source="session-1")
    assert "Potential research question" in journal.config.path(idea["path"]).read_text()
    assert journal.search_tasks("Compare stability") == []


def test_general_sessions_land_in_inbox(journal):
    result = journal.record_progress("Planning", {"decisions": "Explore a new research direction"},
                                     client="claude", session_id="general")
    assert journal.config.path(result["path"]).exists()
    assert list(journal._note("Inbox").glob("Unassigned-*.md"))


def test_duplicate_delivery_is_idempotent(journal):
    journal.add_task("One task", event_id="delivery-1")
    result = journal.add_task("One task", event_id="delivery-1")
    assert result["duplicate"]
    assert len(journal.search_tasks("One task")) == 1
    with pytest.raises(ValueError, match="different content"):
        journal.add_task("Other task", event_id="delivery-1")


def test_manual_progress_event_is_idempotent(journal):
    journal.record_progress("Reading", {"findings": "A useful method"}, event_id="manual-capture")
    result = journal.record_progress("Reading", {"findings": "A useful method"}, event_id="manual-capture")
    assert result["duplicate"]


def test_recovery_after_replace_before_ledger_update(journal):
    from research_journal import journal as module
    actual = module.atomic_write
    count = 0

    def interrupted(path, content, expected):
        nonlocal count
        actual(path, content, expected)
        count += 1
        if count == 1:
            raise OSError("simulated process failure after replacement")

    with patch.object(module, "atomic_write", interrupted):
        result = journal.add_task("Recover once", event_id="interrupted")
    assert result["status"] == "pending"
    assert len(journal.search_tasks("Recover once")) == 1
    assert journal.retry()[0]["status"] == "applied"
    assert len(journal.search_tasks("Recover once")) == 1


def test_retry_preserves_edits_since_queued_write(journal):
    with patch("research_journal.journal.atomic_write", side_effect=OSError("disk unavailable")):
        assert journal.add_task("Pending task", event_id="pending")["status"] == "pending"
    path = journal.config.path(journal.config.todo)
    path.write_text(path.read_text() + "\nHandwritten while unavailable.\n")
    assert journal.retry()[0]["status"] == "pending"
    assert "Handwritten while unavailable" in path.read_text()
    assert list(journal._note("Inbox").glob("Write-conflict-*.md"))


def _concurrent_add(data, number):
    data = dict(data)
    data["vault"], data["state"] = __import__("pathlib").Path(data["vault"]), __import__("pathlib").Path(data["state"])
    return Journal(Config(**data)).add_task(f"Concurrent {number}", event_id=f"parallel-{number}")


def test_multiple_assistant_processes_do_not_lose_updates(journal):
    with ProcessPoolExecutor(max_workers=3, mp_context=get_context("fork")) as executor:
        futures = [executor.submit(_concurrent_add, journal.config.to_dict(), n) for n in range(9)]
        assert all(f.result()["status"] == "applied" for f in futures)
    assert len(journal.search_tasks("Concurrent")) == 9


def test_views_preserve_manual_notes_and_rebuild_from_markdown(journal):
    result = journal.record_progress("Experiment", {"findings": "Original finding"},
                                     session_id="s", project="baseline")
    session = journal.config.path(result["path"])
    session.write_text(session.read_text().replace("Original finding", "Hand-corrected finding"))
    home = journal._note("Home.md")
    home.write_text(home.read_text() + "\nMy daily reflection.\n")
    with locked(journal.config.state) as db:
        db.execute("DELETE FROM events")
    journal.refresh()
    assert "My daily reflection" in home.read_text()
    assert "Hand-corrected finding" in journal._project_path("baseline").read_text()
    assert "Experiment" in home.read_text()


def test_date_uses_configured_timezone(journal):
    fixed = datetime(2026, 10, 20, 23, 55, tzinfo=ZoneInfo("America/Toronto"))
    with patch.object(journal, "now", return_value=fixed):
        journal.add_task("Late task")
    assert "## 2026-10-20\n- [ ] Late task" in journal.config.path(journal.config.todo).read_text()


def test_parser_ignores_frontmatter_and_code_samples():
    content = "---\nexample: |\n  - [ ] metadata\n---\n```md\n- [ ] example\n```\n- [ ] Real\n"
    assert [t.text for t in parse_tasks(content, "note.md")] == ["Real"]


def test_paths_and_symlinks_cannot_escape_vault(journal, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("untouched")
    (journal.config.vault / "linked.md").symlink_to(outside)
    for relative in ("../outside.md", str(outside), ".obsidian/app.json", "linked.md"):
        with pytest.raises(ValueError):
            journal.config.path(relative)
    assert journal.search_tasks("untouched") == []


def test_atomic_write_rejects_stale_content(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("Manual edit")
    with pytest.raises(Conflict):
        atomic_write(path, "Replacement", "Old note")
    assert read(path) == "Manual edit"


def test_unknown_projects_are_rejected(journal):
    with pytest.raises(ValueError, match="Unknown project"):
        journal.capture_idea("Idea", "Why", project="missing")


def test_project_links_and_existing_date_are_reused(journal):
    journal.add_task("Project work", project="baseline")
    task = journal.search_tasks("Project work")[0]
    assert "Baseline research" in task["text"]
    assert "Project work" in journal._project_path("baseline").read_text()
    assert journal.config.path(journal.config.todo).read_text().count(f"## {journal.now():%Y-%m-%d}") == 1


def test_new_date_goes_below_frontmatter():
    from research_journal.tasks import add_under_date
    content = "---\ntags: [todo]\n---\n## 2026-10-21\n- [ ] a\n"
    updated = add_under_date(content, "2026-10-26", "new", "rj-x")
    assert updated.startswith("---\ntags: [todo]\n---\n## 2026-10-26\n- [ ] new ^rj-x\n")
    assert updated.endswith("## 2026-10-21\n- [ ] a\n")


def test_tags_are_not_headings():
    tasks = parse_tasks("## Week\n#urgent\n- [ ] a\n####\n- [ ] b\n", "f.md")
    assert [t.heading for t in tasks] == ["Week", ""]


def test_context_includes_worktree_sessions(journal, tmp_path):
    work = tmp_path / "general"
    journal.record_progress("In a worktree", {"findings": "x"}, client="claude", session_id="w",
                            workspace=str(work / ".claude/worktrees/fix"))
    journal.record_progress("Elsewhere", {"findings": "y"}, client="claude", session_id="o",
                            workspace=str(tmp_path / "general-other"))
    paths = [r["content"] for r in journal.get_context(str(work))["recent_sessions"]]
    assert len(paths) == 1 and "In a worktree" in paths[0]
