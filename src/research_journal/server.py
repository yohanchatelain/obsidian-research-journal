"""A tools-only MCP stdio server using newline-delimited JSON-RPC.

Keeping this small transport dependency-free allows hooks and quick capture to run offline.
Implements lifecycle negotiation, ping, tools/list, tools/call, and notifications.
"""

import inspect
import json
import sqlite3
import sys

from . import __version__
from .hooks import INSTRUCTIONS
from .journal import Journal, SECTIONS
from .storage import Conflict
from .reviews.protocol import TOOLS as REVIEW_TOOLS, invoke
from .reviews import Reviews

STRING = {"type": "string"}
BOOL = {"type": "boolean"}
INTEGER = {"type": "integer"}


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


TASK = schema({"path": STRING, "line": INTEGER, "text": STRING, "done": BOOL,
               "block_id": {"type": ["string", "null"]}, "fingerprint": STRING,
               "heading": STRING, "indent": INTEGER, "open_children": BOOL},
              ("path", "text", "fingerprint", "line"))
TOOLS = {
    "list_projects": ("List configured research projects and their existing note paths.",
                      schema({})),
    "get_context": ("Read recent research progress, existing project notes, and open tasks before working.",
                    schema({"workspace": STRING, "project": STRING})),
    "search_tasks": ("Find exact checkbox references; pass a returned object unchanged to update_task.",
                     schema({"query": STRING, "include_done": BOOL})),
    "record_progress": ("Save a research checkpoint; distinguish findings from plans. Include session ID and turn from hook context.",
                        schema({"title": STRING, "sections": schema({key: STRING for key in SECTIONS}),
                                "client": STRING, "session_id": STRING, "project": STRING,
                                "workspace": STRING, "source": STRING, "event_id": STRING,
                                "turn": INTEGER}, ("title", "sections"))),
    "capture_idea": ("Save a future project idea without creating a task.",
                     schema({"title": STRING, "motivation": STRING, "next_steps": STRING,
                             "project": STRING, "source": STRING, "event_id": STRING},
                            ("title", "motivation"))),
    "add_task": ("Add an intended action under today's date in the existing TODO list.",
                 schema({"text": STRING, "project": STRING, "event_id": STRING}, ("text",))),
    "update_task": ("Complete or reopen an exact task. Completion requires evidence and a reason; uncertain updates go to the inbox.",
                    schema({"task": TASK, "done": BOOL, "evidence": STRING,
                            "reason": STRING, "event_id": STRING}, ("task",))),
    "link_task": ("Link an existing task to a registered project, preserving its original location.",
                  schema({"task": TASK, "project": STRING, "event_id": STRING}, ("task", "project"))),
    "assign_session": ("Assign an unassigned session to a registered project.",
                       schema({"path": STRING, "project": STRING, "event_id": STRING}, ("path", "project"))),
    "checkpoint": ("Acknowledge a turn with nothing to record. For actual work use record_progress.",
                   schema({"client": STRING, "session_id": STRING, "turn": INTEGER,
                           "nothing_to_record": BOOL}, ("client", "session_id", "turn", "nothing_to_record"))),
    "status": ("Show pending writes and sessions lacking a checkpoint.", schema({})),
    "refresh": ("Refresh Obsidian dashboards after handwritten changes.", schema({})),
}

TOOLS.update(REVIEW_TOOLS)

# Expose only known non-mutating operations to ChatGPT unless the operator opts in.
# Future MCP tools must be reviewed before joining this allowlist.
READ_ONLY_TOOLS = frozenset({"list_projects", "get_context", "search_tasks", "status", "review_get_packet", "review_status"})
CHATGPT_WRITE_TOOLS = READ_ONLY_TOOLS | frozenset({
    "record_progress", "capture_idea", "add_task", "update_task",
    "link_task", "assign_session", "refresh",
})
# Never expose review execution, participant mutations, or hook-only checkpointing
# over a browser-originated connection, even when writes are explicitly enabled.


def validate(value, spec, field="arguments"):
    types = spec.get("type")
    allowed = types if isinstance(types, list) else [types]
    match = {"object": isinstance(value, dict), "string": isinstance(value, str),
             "integer": isinstance(value, int) and not isinstance(value, bool),
             "boolean": isinstance(value, bool), "null": value is None,
             "array": isinstance(value, list),
             "number": isinstance(value, (int, float)) and not isinstance(value, bool)}
    if not any(match.get(kind, False) for kind in allowed):
        raise ValueError(f"{field} must be {types}")
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError(f"{field} must be one of {spec['enum']}")
    if isinstance(value, list) and "items" in spec:
        for index, item in enumerate(value):
            validate(item, spec["items"], f"{field}[{index}]")
    if isinstance(value, dict):
        properties = spec.get("properties", {})
        missing = set(spec.get("required", [])) - set(value)
        if missing:
            raise ValueError(f"{field} missing: {', '.join(sorted(missing))}")
        if spec.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError(f"{field} contains unknown fields")
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], f"{field}.{key}")


class Server:
    def __init__(self, journal: Journal, client="manual", *, allow_writes=False):
        self.journal = journal
        self.client = client
        self.allowed_tools = ((CHATGPT_WRITE_TOOLS if allow_writes else READ_ONLY_TOOLS)
                              if client == "chatgpt" else frozenset(TOOLS))
        self.initialized = False
        self.ready = False

    def dispatch(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                    "error": {"code": -32600, "message": "Invalid Request"}}
        method = request["method"]
        identifier = request.get("id")
        if "id" not in request:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            return None
        try:
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            if method == "initialize":
                supported = {"2024-11-05", "2025-03-26", "2025-06-18"}
                requested = params.get("protocolVersion")
                version = requested if requested in supported else "2025-06-18"
                result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "research-journal", "version": __version__},
                          "instructions": INSTRUCTIONS}
                self.initialized = True
            elif method == "ping":
                result = {}
            elif not self.ready:
                return self._error(identifier, -32000, "Initialize the MCP connection first")
            elif method == "tools/list":
                result = {"tools": [{"name": name, "description": description, "inputSchema": spec,
                          "annotations": {"readOnlyHint": name in READ_ONLY_TOOLS,
                                          "destructiveHint": False, "openWorldHint": False}}
                         for name, (description, spec) in TOOLS.items()
                         if name in self.allowed_tools]}
            elif method == "tools/call":
                name = params.get("name")
                if name not in self.allowed_tools:
                    return self._error(identifier, -32602, "Unknown or disabled tool")
                arguments = params.get("arguments", {})
                try:
                    validate(arguments, TOOLS[name][1])
                    arguments = dict(arguments)
                    if name in REVIEW_TOOLS:
                        value = invoke(Reviews(self.journal.config), name, arguments, self.client)
                    else:
                        signature = inspect.signature(getattr(self.journal, name))
                        if "client" in signature.parameters:
                            if "client" in arguments and arguments["client"] != self.client:
                                raise ValueError(f"client must match this connection: {self.client}")
                            arguments["client"] = self.client
                        value = getattr(self.journal, name)(**arguments)
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                              "isError": False}
                except (ValueError, TypeError, OSError, sqlite3.Error, Conflict) as error:
                    result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
            else:
                return self._error(identifier, -32601, "Method not found")
            return {"jsonrpc": "2.0", "id": identifier, "result": result}
        except (ValueError, TypeError) as error:
            return self._error(identifier, -32602, str(error))

    @staticmethod
    def _error(identifier, code, message):
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}

    def run(self, stdin=None, stdout=None):
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            request = None
            try:
                request = json.loads(line)
                response = self.dispatch(request)
            except json.JSONDecodeError:
                response = self._error(None, -32700, "Parse error")
            except Exception as error:
                print(f"research-journal: {type(error).__name__}: {error}", file=sys.stderr)
                response = self._error(request.get("id") if isinstance(request, dict) else None,
                                       -32603, "Internal error; see server stderr")
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()
