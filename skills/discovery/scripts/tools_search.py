#!/usr/bin/env python3
"""
tools_search.py - Query available Charlotte AI AgentWorks tools.

Uses the native FalconPy Tools.queries_tools_v1 method.

Examples:
    python tools_search.py --filter 'type:"..."'
    python tools_search.py --limit 50
"""

import sys
import os

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.realpath(__file__)),
        "..", "..", "..", "common", "scripts",
    ),
)
import _bootstrap  # pylint: disable=wrong-import-position

_bootstrap.ensure_deps(__file__)
from auth import get_tools_client  # pylint: disable=wrong-import-position
from discovery_helpers import run_entity_search  # pylint: disable=wrong-import-position


def main() -> None:
    """CLI entry point: query available tool IDs by FQL filter."""
    run_entity_search(
        entity_name="tool",
        entity_plural="tools",
        # Deferred so get_tools_client() (which resolves credentials) only runs
        # after argparse has had a chance to handle --help / bad args.
        query_fn=lambda **kw: get_tools_client().queries_tools_v1(**kw),
    )


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        # call_native raises RuntimeError on HTTP errors (e.g. 404 bad ID, 403 missing scope).
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
