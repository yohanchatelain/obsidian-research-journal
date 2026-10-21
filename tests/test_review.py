from unittest.mock import patch

from research_journal.storage import read


def test_existing_task_can_be_linked_without_moving_it(journal):
    task = journal.search_tasks("Read paper")[0]
    result = journal.link_task(task, "baseline")
    linked = journal.search_tasks("Read paper")[0]
    assert linked["path"] == task["path"]
    assert linked["line"] == task["line"]
    assert linked["block_id"] == result["block_id"]
    assert "Baseline research" in linked["text"]
    assert "Read paper" in journal._project_path("baseline").read_text()
    home = journal._note("Home.md").read_text()
    assert "|Read paper [[" not in home


def test_session_assignment_resolves_inbox_and_updates_context(journal):
    result = journal.record_progress("Unassigned reading", {"findings": "New method"}, session_id="unassigned")
    assert "Unassigned-manual" in journal._note("Home.md").read_text()
    journal.assign_session(result["path"], "baseline")
    assert "Unassigned-manual" not in journal._note("Home.md").read_text()
    assert "New method" in journal.get_context(project="baseline")["recent_sessions"][0]["content"]


def test_pending_event_can_be_inspected_and_discarded(journal):
    with patch("research_journal.journal.atomic_write", side_effect=OSError("unavailable")):
        journal.add_task("Pending action", event_id="pending")
    detail = journal.inspect_event("pending")
    assert detail["payload"]["text"] == "Pending action"
    assert detail["writes"][0]["after_text"]
    journal.discard_event("pending")
    assert journal.status()["pending_writes"] == []
    assert journal.retry() == []


def test_failed_view_refresh_does_not_report_saved_work_as_pending(journal):
    home = journal._note("Home.md")
    original = read(home)
    home.write_text(original.replace("<!-- research-journal:end -->", ""))
    result = journal.add_task("Durable task", event_id="durable")
    assert result["status"] == "applied"
    assert result["view_error"]
    assert journal.search_tasks("Durable task")
    assert journal.status()["view_error"]
    home.write_text(original)
    journal.retry()
    assert "view_error" not in journal.status()


def test_renamed_session_is_still_read_from_markdown(journal):
    result = journal.record_progress("Reading", {"findings": "A finding"}, project="baseline")
    path = journal.config.path(result["path"])
    path.rename(path.with_name("My renamed session.md"))
    journal.refresh()
    assert "My renamed session" in journal._note("Home.md").read_text()
    assert "A finding" in journal.get_context(project="baseline")["recent_sessions"][0]["content"]
    journal.record_progress("Follow-up", {"findings": "Second finding"}, session_id=
                            journal._progress()[0]["session_id"], project="baseline")
    assert len(journal._progress()) == 1


def test_resume_does_not_hide_a_previous_capture_gap(journal):
    from research_journal.hooks import handle
    payload = {"session_id": "s", "turn_id": "first", "prompt": "First task"}
    handle(journal, "codex", "SessionStart", payload)
    handle(journal, "codex", "UserPromptSubmit", payload)
    handle(journal, "codex", "Interrupt", payload)
    handle(journal, "codex", "UserPromptSubmit", {**payload, "turn_id": "second"})
    journal.record_progress("New work", {"findings": "New result"}, client="codex", session_id="s", turn=2)
    assert journal.status()["incomplete_sessions"] == []
    assert journal.status()["capture_gaps"][0]["turn"] == 1
    journal.record_progress("Recovered earlier work", {"findings": "Earlier result"},
                            client="codex", session_id="s", turn=1)
    assert journal.status()["capture_gaps"] == []
