#!/usr/bin/env python3
"""Minimal script used by skills-demo for skill execution smoke tests."""

from __future__ import annotations

import argparse
import json


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Emit a small JSON payload for bundle skill smoke tests."
    )
    parser.add_argument("--text", default="hello", help="Message text to include in payload.")
    args = parser.parse_args()

    payload = {
        "ok": True,
        "message": args.text,
        "source": "skills-demo.basic-script-skill",
    }
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
