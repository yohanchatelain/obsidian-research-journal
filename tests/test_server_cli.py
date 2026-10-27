import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from research_journal.cli import main, parser
from research_journal.server import Server, READ_ONLY_TOOLS

RUNNER = Path(__file__).resolve().parents[1] / "bin/research-journal"


def request(server, method, params=None, identifier=1):
    return server.dispatch({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {}})


def connect(server):
    assert request(server, "initialize", {"protocolVersion": "2025-06-18"})["result"]["protocolVersion"] == "2025-06-18"
    server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})


@pytest.mark.parametrize("client", ["codex", "claude", "antigravity"])
def test_mcp_each_client_notebook_workflow(journal, client):
    server = Server(journal, client)
    connect(server)
    tools = request(server, "tools/list")["result"]["tools"]
    assert {"record_progress", "update_task", "capture_idea"} <= {t["name"] for t in tools}

    def call(name, arguments):
        result = request(server, "tools/call", {"name": name, "arguments": arguments})["result"]
        assert not result["isError"], result
        return json.loads(result["content"][0]["text"])

    call("get_context", {})
    task = call("search_tasks", {"query": "Read paper"})[0]
    assert call("update_task", {"task": task, "evidence": "paper notes", "reason": "Read and summarized"})["status"] == "applied"
    assert call("record_progress", {"title": "Reading", "sections": {"findings": "Useful method"},
                                  "session_id": client + "-session"})["status"] == "applied"
    assert call("capture_idea", {"title": "Follow-up", "motivation": "Test the method"})["status"] == "applied"


def test_mcp_rejects_invalid_input_and_unknown_tools(journal):
    server = Server(journal)
    assert request(server, "tools/list")["error"]["code"] == -32000
    connect(server)
    assert request(server, "tools/call", {"name": "missing"})["error"]["code"] == -32602
    result = request(server, "tools/call", {"name": "update_task", "arguments": {"task": "a string"}})
    assert result["result"]["isError"]
    result = request(server, "tools/call", {"name": "add_task", "arguments": {"text": "x", "unknown": 2}})
    assert result["result"]["isError"]
    result = request(server, "tools/call", {"name": "record_progress", "arguments": {
        "title": "x", "sections": {"findings": "x"}, "client": "spoofed"}})
    assert result["result"]["isError"]


def test_stdio_framing_invalid_json_and_notifications(journal):
    server = Server(journal)
    stdin = io.StringIO('bad json\n' + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) +
                        '\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n' +
                        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
    stdout = io.StringIO()
    server.run(stdin, stdout)
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(responses) == 3
    assert responses[0]["error"]["code"] == -32700
    assert responses[-1]["result"]["tools"]


def test_real_stdio_process_can_capture_without_dependencies(journal, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(journal.config.to_dict()))
    process = subprocess.Popen([sys.executable, str(RUNNER), "--config", str(config), "mcp", "--client", "codex"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        def send(message, reply=True):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            return json.loads(process.stdout.readline()) if reply else None
        response = send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-03-26"}})
        assert response["result"]["protocolVersion"] == "2025-03-26"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"}, False)
        result = send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "add_task", "arguments": {"text": "Captured over stdio"}}})
        assert not result["result"]["isError"]
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert journal.search_tasks("Captured over stdio")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_cli_init_and_quick_capture(tmp_path, capsys):
    config = tmp_path / "config.json"
    prefix = ["--config", str(config)]
    assert main([*prefix, "init", "--vault", str(tmp_path / "vault")]) == 0
    assert main([*prefix, "task", "Read a paper"]) == 0
    assert main([*prefix, "idea", "New method", "--why", "Might improve robustness"]) == 0
    assert main([*prefix, "progress", "Reading", "--finding", "Found a useful method"]) == 0
    assert main([*prefix, "status"]) == 0
    assert main([*prefix, "doctor"]) == 0
    assert "Research Journal/Ideas/New-method" in capsys.readouterr().out
    assert "Might improve robustness" in next((tmp_path / "vault/Research Journal/Ideas").glob("*.md")).read_text()


def test_unavailable_hook_is_nonblocking(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id":"s"}'))
    assert main(["--config", str(tmp_path / "missing.json"), "hook", "--client", "codex", "--event", "Stop"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {}
    assert "unavailable" in captured.err


def test_chatgpt_default_read_only_tools_and_enforced_denial(journal):
    server = Server(journal, "chatgpt")
    connect(server)
    listed = {tool["name"] for tool in request(server, "tools/list")["result"]["tools"]}
    assert listed == READ_ONLY_TOOLS
    response = request(server, "tools/call", {
        "name": "add_task", "arguments": {"text": "Should not appear in the vault"}})
    assert response["error"]["code"] == -32602
    assert journal.search_tasks("Should not appear in the vault") == []
    response = request(server, "tools/call", {"name": "status", "arguments": {}})
    assert response["result"]["isError"] is False


def test_chatgpt_explicit_opt_in_enables_writes(journal):
    server = Server(journal, "chatgpt", allow_writes=True)
    connect(server)
    listed = {tool["name"] for tool in request(server, "tools/list")["result"]["tools"]}
    assert "add_task" in listed
    response = request(server, "tools/call", {
        "name": "add_task", "arguments": {"text": "Approved ChatGPT test task"}})
    assert response["result"]["isError"] is False
    assert journal.search_tasks("Approved ChatGPT test task")


def test_chatgpt_cli_flags_and_no_implicit_write_permission():
    args = parser().parse_args(["mcp", "--client", "chatgpt"])
    assert args.allow_writes is False
    args = parser().parse_args(["mcp", "--client", "chatgpt", "--allow-writes"])
    assert args.allow_writes is True
