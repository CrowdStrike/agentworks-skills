#!/usr/bin/env python3
"""
templates_search.py - Query available Charlotte AI AgentWorks templates.

Uses the native FalconPy AgentTemplates.queries_agent_templates_v1 method.

Examples:
    python templates_search.py --filter 'category:"..."'
    python templates_search.py --limit 50
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
from auth import get_agent_templates_client  # pylint: disable=wrong-import-position
from discovery_helpers import run_entity_search  # pylint: disable=wrong-import-position


def main() -> None:
    """CLI entry point: query available agent template IDs by FQL filter."""
    run_entity_search(
        entity_name="template",
        entity_plural="templates",
        # Deferred so get_agent_templates_client() (which resolves credentials) only
        # runs after argparse has had a chance to handle --help / bad args.
        query_fn=lambda **kw: get_agent_templates_client().queries_agent_templates_v1(**kw),
    )


if __name__ == "__main__":
    main()
