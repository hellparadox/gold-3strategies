"""Inspect unresolved order intents; release only after manual broker review.

Run with python -m tools.execution_journal --config config/settings.yaml
This tool never sends broker orders. Stop the bot before acknowledging an intent.
"""
from __future__ import annotations

import argparse
import json

from core import Settings
from core.database import Database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--acknowledge", help="exact signal_key from the listing")
    parser.add_argument("--broker-reviewed", action="store_true")
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    settings = Settings.load(args.config)
    with Database(str(settings.get("database.path", "subscriptions.db"))) as db:
        unresolved = db.unresolved_executions()
        if args.acknowledge:
            if not args.broker_reviewed or not args.note.strip():
                parser.error("acknowledgement requires --broker-reviewed and --note")
            if args.acknowledge not in {r["signal_key"] for r in unresolved}:
                parser.error("key is not an unresolved intent")
            db.finish_execution(args.acknowledge, "reviewed", args.note)
            print("Acknowledged. The old signal remains consumed and will not be sent again.")
        else:
            print(json.dumps(unresolved, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
