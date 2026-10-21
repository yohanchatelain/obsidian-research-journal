#!/usr/bin/env python3
"""Create a fictional review vault, using only small local checks and synthetic reviewers."""

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from research_journal.config import Config
from research_journal.journal import Journal
from research_journal.reviews import Reviews
from research_journal.reviews.store import identifier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("local/review-demo"))
    args = parser.parse_args()
    root = args.output.resolve()
    if root.exists():
        raise SystemExit("Choose a new --output directory; the demo does not overwrite existing notes.")
    root.mkdir(parents=True)
    config = Config(root / "vault", root / "state")
    Journal(config).initialize()
    (root / "config.json").write_text(json.dumps(config.to_dict(), indent=2) + "\n")
    source = root / "study.md"
    source.write_text("# Fictional pilot\n\nAll three runs succeeded, so the result generalizes.\n")
    reviews = Reviews(config)
    rid = reviews.create("Fictional pilot: evidence review", source,
                         "Assess whether the claim follows from the available evidence.",
                         context="This is an illustrative research workflow, not a scientific result.")["review_id"]
    check = reviews.request_check(rid, {"argv": [sys.executable, "-c", "from pathlib import Path; print(Path('study.md').read_text())"],
                                       "timeout": 5}, owner=True)["check_id"]
    reviews.approve_check(rid, check)

    def run_check():
        reviews.run_check(rid, check, owner=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            reviews.poll(rid)
            jobs = reviews.status(rid, owner=True)["jobs"]
            if all(j["status"] in {"succeeded", "failed", "cancelled", "timed_out"} for j in jobs):
                assert all(j["status"] == "succeeded" for j in jobs)
                return
            time.sleep(0.05)
        raise RuntimeError("Demo check timed out")

    def submit(slot, client, version, result):
        token = reviews.invite(rid, slot)["token"]
        # Synthetic receipts are exclusively for this fictional demo. Real reviews require hooks.
        session = "fictional-demo-" + identifier()
        reviews.store.receipt(client, session)
        reviews.join(rid, token, client, session, True)
        attempt = reviews.claim(rid, token)["attempt_id"]
        reviews.submit(rid, token, attempt, version, result, identifier())

    run_check()
    reviews.start(rid)
    for slot, client, concern in [("reviewer-1", "codex", "Three runs cannot establish generalization."),
                                  ("reviewer-2", "claude", "The claim omits uncertainty and the sample size.")]:
        submit(slot, client, 1, {"summary": "Fictional critique", "findings": [{"id": "F1",
            "claim": "The result generalizes", "concern": concern,
            "evidence": "study.md version 1 line 3", "recommendation": "Limit the claim to the pilot."}]})
    author = reviews.invite(rid, "", "author")["token"]
    reviews.respond(rid, author, {f"{slot}:F1": {"disposition": "accepted", "reason": "Restricted the claim to observed runs."}
                                 for slot in ("reviewer-1", "reviewer-2")}, identifier())
    source.write_text("# Fictional pilot\n\nThree pilot runs succeeded. Generalization and uncertainty remain untested.\n")
    reviews.revise(rid, author, source)
    run_check()
    reviews.follow_up(rid, author)
    for slot, client in [("reviewer-1", "codex"), ("reviewer-2", "claude")]:
        submit(slot, client, 2, {"summary": "The revised text restricts the claim to the pilot; no generalization was verified.",
                               "findings": []})
    reviews.close(rid, "completed", "Both fictional reviewers inspected the revision. The pilot remains illustrative.",
                  {f"1:{slot}:F1": {"status": "verified", "version": 2, "evidence": "study.md v2 line 3",
                                    "explanation": "The revised wording explicitly says generalization is untested."}
                   for slot in ("reviewer-1", "reviewer-2")})
    reviews.export_bundle(rid, root / "review-bundle.json")
    Journal(config).refresh()
    print(json.dumps({"vault": str(config.vault), "review_id": rid, "phase": "closed", "outcome": "completed"}, indent=2))


if __name__ == "__main__":
    main()
