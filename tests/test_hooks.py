import pytest

from research_journal.hooks import handle


@pytest.mark.parametrize("client", ["codex", "claude", "antigravity"])
def test_each_client_requests_one_checkpoint(journal, client):
    payload = {"session_id": "s", "conversationId": "s", "cwd": "/work",
               "workspacePaths": ["/work"], "executionNum": 1}
    started = handle(journal, client, "SessionStart", payload)
    assert "session_id=s" in str(started)
    first = handle(journal, client, "Stop", payload)
    assert first["decision"] == ("continue" if client == "antigravity" else "block")
    second = handle(journal, client, "Stop", payload)
    assert second.get("decision") not in {"continue", "block"}
    assert journal.status()["incomplete_sessions"][0]["state"] == "incomplete"


@pytest.mark.parametrize("client", ["codex", "claude", "antigravity"])
def test_checkpoint_allows_stop(journal, client):
    payload = {"session_id": "s", "conversationId": "s"}
    handle(journal, client, "SessionStart", payload)
    journal.record_progress("Work", {"findings": "Verified result"}, client=client, session_id="s", turn=0)
    assert handle(journal, client, "Stop", payload).get("decision") not in {"continue", "block"}
    assert journal.status()["incomplete_sessions"] == []


def test_nothing_to_record_and_new_prompt(journal):
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
    payload = {"session_id": "s"}
    handle(journal, "codex", "SessionStart", payload)
    assert handle(journal, "codex", "Stop", {**payload, "stop_hook_active": True}) == {}
    handle(journal, "codex", "Interrupt", payload)
    assert journal.status()["incomplete_sessions"][0]["state"] == "interrupted"


def test_antigravity_new_turn_after_idle(journal):
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
