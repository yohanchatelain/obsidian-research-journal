import pytest

from research_journal.hooks import INSTRUCTIONS, handle


@pytest.mark.parametrize("client", ["codex", "claude", "antigravity"])
def test_default_stop_is_nonblocking_and_concise(journal, client):
    payload = {"session_id": "s", "conversationId": "s", "cwd": "/work",
               "workspacePaths": ["/work"], "executionNum": 1}
    started = handle(journal, client, "SessionStart", payload)
    assert "session_id=s" in str(started)
    assert INSTRUCTIONS.strip() not in str(started)
    if client == "claude":
        handle(journal, client, "UserPromptSubmit", {**payload, "prompt": "Hello"})
    stopped = handle(journal, client, "Stop", payload)
    assert stopped.get("decision") not in {"continue", "block"}
    assert journal.status()["incomplete_sessions"][0]["state"] == "incomplete"


@pytest.mark.parametrize("client", ["codex", "claude", "antigravity"])
def test_each_client_requests_one_checkpoint_when_enabled(journal, client):
    journal.config.stop_reminder = True
    payload = {"session_id": "s", "conversationId": "s", "cwd": "/work",
               "workspacePaths": ["/work"], "executionNum": 1}
    started = handle(journal, client, "SessionStart", payload)
    assert "session_id=s" in str(started)
    if client == "claude":  # Claude turns always start with a prompt.
        handle(journal, client, "UserPromptSubmit", {**payload, "prompt": "Hello"})
    first = handle(journal, client, "Stop", payload)
    assert first["decision"] == ("continue" if client == "antigravity" else "block")
    assert INSTRUCTIONS.strip() not in first["reason"]
    second = handle(journal, client, "Stop", payload)
    assert second.get("decision") not in {"continue", "block"}
    assert journal.status()["incomplete_sessions"][0]["state"] == "incomplete"


def test_verbosity_modes(journal):
    payload = {"session_id": "s", "cwd": "/work"}
    journal.config.verbosity = "verbose"
    journal.config.stop_reminder = True
    started = handle(journal, "codex", "SessionStart", payload)
    assert INSTRUCTIONS.strip() in str(started)
    stopped = handle(journal, "codex", "Stop", payload)
    assert INSTRUCTIONS.strip() in stopped["reason"]

    journal.config.verbosity = "off"
    assert handle(journal, "codex", "SessionStart", {"session_id": "s2"}) == {}
    assert handle(journal, "codex", "Stop", {"session_id": "s2"}) == {}


@pytest.mark.parametrize("client", ["codex", "claude", "antigravity"])
def test_checkpoint_allows_stop(journal, client):
    journal.config.stop_reminder = True
    payload = {"session_id": "s", "conversationId": "s"}
    handle(journal, client, "SessionStart", payload)
    journal.record_progress("Work", {"findings": "Verified result"}, client=client, session_id="s", turn=0)
    assert handle(journal, client, "Stop", payload).get("decision") not in {"continue", "block"}
    assert journal.status()["incomplete_sessions"] == []


def test_nothing_to_record_and_new_prompt(journal):
    journal.config.stop_reminder = True
    payload = {"session_id": "s", "prompt": "Hello", "turn_id": "turn-a"}
    handle(journal, "claude", "SessionStart", payload)
    handle(journal, "claude", "UserPromptSubmit", payload)
    journal.checkpoint("claude", "s", 1, nothing_to_record=True)
    assert handle(journal, "claude", "Stop", payload) == {}
    handle(journal, "claude", "UserPromptSubmit", {**payload, "turn_id": "turn-b"})
    assert handle(journal, "claude", "Stop", payload)["decision"] == "block"


def test_noop_rejects_stale_or_unknown_turn(journal):
    with pytest.raises(ValueError):
        journal.checkpoint("claude", "s", 5, nothing_to_record=True)
    handle(journal, "claude", "SessionStart", {"session_id": "s"})
    with pytest.raises(ValueError):
        journal.checkpoint("claude", "s", 5, nothing_to_record=True)


def test_stop_hook_guard_and_interrupt(journal):
    journal.config.stop_reminder = True
    payload = {"session_id": "s"}
    handle(journal, "codex", "SessionStart", payload)
    assert handle(journal, "codex", "Stop", {**payload, "stop_hook_active": True}) == {}
    handle(journal, "codex", "Interrupt", payload)
    assert journal.status()["incomplete_sessions"][0]["state"] == "interrupted"


def test_antigravity_new_turn_after_idle(journal):
    journal.config.stop_reminder = True
    payload = {"conversationId": "s", "workspacePaths": ["/work"]}
    handle(journal, "antigravity", "SessionStart", payload)
    journal.checkpoint("antigravity", "s", 0, True)
    handle(journal, "antigravity", "Stop", payload)
    started = handle(journal, "antigravity", "PreInvocation", payload)
    assert "turn=1" in str(started)
    assert handle(journal, "antigravity", "Stop", payload)["decision"] == "continue"
    # The continuation invocation must not start another turn.
    assert "turn=1" in str(handle(journal, "antigravity", "PreInvocation", payload))
    assert handle(journal, "antigravity", "Stop", payload)["decision"] == "allow"


def test_malformed_unknown_payload_never_blocks(journal):
    assert handle(journal, "codex", "Stop", {}) == {}


def test_background_notifications_do_not_start_turns(journal):
    payload = {"session_id": "s"}
    handle(journal, "claude", "SessionStart", payload)
    handle(journal, "claude", "UserPromptSubmit", {**payload, "prompt": "Run jobs", "turn_id": "a"})
    for job in ("one", "two"):
        notice = {**payload, "prompt": f"<task-notification>\n<task-id>{job}</task-id>", "turn_id": job}
        assert "turn=1" in str(handle(journal, "claude", "UserPromptSubmit", notice))
    assert journal.status()["capture_gaps"] == []
    journal.checkpoint("claude", "s", 1, nothing_to_record=True)
    assert handle(journal, "claude", "Stop", payload) == {}


def test_claude_session_without_prompt_is_not_a_gap(journal):
    payload = {"session_id": "s"}
    handle(journal, "claude", "SessionStart", payload)
    handle(journal, "claude", "SessionEnd", payload)
    status = journal.status()
    assert status["incomplete_sessions"] == [] and status["capture_gaps"] == []
