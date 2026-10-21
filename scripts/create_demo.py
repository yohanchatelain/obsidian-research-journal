#!/usr/bin/env python3
"""Generate an isolated, explicitly illustrative vault without touching user settings."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from research_journal.config import Config
from research_journal.hooks import handle
from research_journal.journal import Journal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("local/demo"))
    args = parser.parse_args()
    destination = args.output.resolve()
    config = Config(destination / "vault", destination / "state", projects={
        "pilot": {"name": "Pilot study (demo)", "workspaces": [str(destination / "example-workspace")],
                  "notes": ["Existing research/Overview.md"]}})
    config.vault.mkdir(parents=True, exist_ok=True)
    existing = config.vault / "Existing research/Overview.md"
    existing.parent.mkdir(exist_ok=True)
    if not existing.exists():
        existing.write_text("# Pilot study — illustrative example\n\n"
                            "This vault contains fictional work to demonstrate the journal workflow.\n")
    config_path = destination / "config.json"
    config_path.write_text(json.dumps(config.to_dict(), indent=2) + "\n")
    journal = Journal(config)
    journal.initialize()
    journal.add_task("Inspect the completed pilot results", project="pilot", event_id="demo-inspect-task")
    journal.add_task("Read the methodology paper", project="pilot", event_id="demo-read-task")
    for client, title, sections in [
        ("codex", "Pilot experiment launched (demo)", {
            "goals": "Compare the pilot against the reference baseline.",
            "methods": "Prepare and launch a small permutation experiment.",
            "findings": "Illustrative job is running; results have not been inspected.",
            "failed_attempts": "The first illustrative run exceeded its time limit.",
            "evidence": "Demo scenario only; no real experiment was run.",
            "next_steps": "Inspect results before completing the corresponding task."}),
        ("claude", "Methodology review (demo)", {
            "goals": "Understand how to report variability.",
            "findings": "The illustrative review suggests reporting the median and best permutation.",
            "decisions": "Preserve negative results and distinguish results from plans.",
            "next_steps": "Consider a sensitivity analysis."}),
        ("antigravity", "Pipeline verification (demo)", {
            "methods": "Inspect the illustrative analysis pipeline.",
            "findings": "The output summary is ready for later review.",
            "blockers": "Results still need a scientific interpretation.",
            "next_steps": "Compare the summary with the reference."})]:
        session_id = f"demo-{client}"
        handle(journal, client, "SessionStart", {"session_id": session_id, "conversationId": session_id})
        journal.record_progress(title, sections, client=client, session_id=session_id, project="pilot",
                                event_id=f"demo-{client}-progress", turn=0)
        handle(journal, client, "Stop", {"session_id": session_id, "conversationId": session_id})
    journal.capture_idea("Sensitivity to software versions (demo)",
        "An illustrative follow-up: compare variation across software releases.",
        "Start with a small paired pilot before committing to a larger project.",
        project="pilot", source="Illustrative Claude session", event_id="demo-idea")
    task = journal.search_tasks("Inspect the completed pilot")[0]
    journal.update_task(task, reason="The job was launched; findings are still pending.",
                        event_id="demo-uncertain-completion")
    print(json.dumps({"vault": str(config.vault), "home": str(journal._note("Home.md")),
                      "config": str(config_path), "notice": "Fictional demonstration only"}, indent=2))


if __name__ == "__main__":
    main()
