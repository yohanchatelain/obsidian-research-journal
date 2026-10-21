"""Participant MCP surface. Approval, retry, invitation, import and closure are CLI-only."""

from .execution import RESULT_SCHEMA

STRING = {"type": "string"}
INTEGER = {"type": "integer"}
BOOL = {"type": "boolean"}
OBJECT = {"type": "object"}
STRINGS = {"type": "array", "items": STRING}


def tool(description, fields=None, required=()):
    properties = {"review_id": STRING, "token": STRING, **(fields or {})}
    return description, {"type": "object", "properties": properties,
                         "required": ["review_id", "token", *required], "additionalProperties": False}


TOOLS = {
    "review_join": tool("Bind this invitation to a fresh hooked session; explicitly declare fresh context.",
                        {"client": STRING, "session_id": STRING, "fresh_context": BOOL},
                        ("session_id", "fresh_context")),
    "review_claim": tool("Claim the joined assignment once; save the returned attempt ID.",
                         {"lease_seconds": INTEGER}),
    "review_get_packet": tool("Read immutable inputs and sealed evidence. Initial peer findings remain embargoed."),
    "review_status": tool("Read participant-visible review progress; this cannot approve checks or close a review."),
    "review_submit": tool("Submit structured findings for the claimed attempt and packet version; use a unique message ID.",
                          {"attempt_id": STRING, "version": INTEGER, "result": RESULT_SCHEMA, "message_id": STRING},
                          ("attempt_id", "version", "result", "message_id")),
    "review_request_check": tool("Propose an executable check. A human must approve its exact command and executor in the CLI.",
                                 {"spec": OBJECT, "message_id": STRING}, ("spec", "message_id")),
    "review_run_check": tool("Run an already human-approved check before evidence is sealed.",
                             {"check_id": STRING}, ("check_id",)),
    "review_poll": tool("Observe durable job handles once; never resubmit an unknown attempt."),
    "review_respond": tool("Author only: respond once to every initial finding after reveal, using slot:finding-id keys.",
                           {"responses": OBJECT, "message_id": STRING}, ("responses", "message_id")),
    "review_revise": tool("Author only: freeze one revised source packet before sealing follow-up evidence.",
                          {"source": STRING, "include": STRINGS}, ("source",)),
    "review_follow_up": tool("Author only: seal response, optional revision and check evidence for both reviewers' final pass."),
}

METHODS = {"review_get_packet": "packet", "review_request_check": "request_check",
           "review_run_check": "run_check", "review_poll": "participant_poll"}


def invoke(reviews, name, arguments, client):
    args = dict(arguments)
    with reviews.store.transaction() as db:
        review = reviews.store.load(db, args["review_id"])
        _, assignment = reviews._actor(review, args["token"])
        if assignment is not None and assignment["profile"]["client"] != client:
            raise ValueError("Invitation client must match this MCP connection")
    if name == "review_join":
        if "client" in args and args["client"] != client:
            raise ValueError("Client must match this MCP connection")
        args["client"] = client
    method = METHODS.get(name, name.removeprefix("review_"))
    return getattr(reviews, method)(**args)
