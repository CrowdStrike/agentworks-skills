#!/usr/bin/env python3
"""
probe_span_filters.py - Probe different filter syntaxes against the live API to find what works.

Systematically tries different filter formats to determine which ones
the Charlotte AI AgentWorks API actually accepts.

Examples:
    # Test all filter variations
    python probe_span_filters.py

    # Test with known trace ID
    python probe_span_filters.py --trace-id <trace-id>
"""

import argparse
import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.realpath(__file__)),
        "..", "..", "..", "common", "scripts",
    ),
)
import _bootstrap  # pylint: disable=wrong-import-position

_bootstrap.ensure_deps(__file__)
from auth import call_native, get_spans_client  # pylint: disable=wrong-import-position


# The time bound every script adds to a span query.
SCRIPT_TIME_FILTER = "start_time:>='now-24h'"


def probe_filter(filter_expr: str) -> tuple[bool, int, str]:
    """Test a filter and return (success, result_count, error_message)."""
    params = {"filter": filter_expr, "limit": "5"}

    try:
        response = call_native(get_spans_client().queries_spans_v1, parameters=params)
        resources = response.get("resources", [])
        return (True, len(resources), "")
    except Exception as e:  # pylint: disable=broad-exception-caught  # a probe reports any failure, it never raises
        return (False, 0, str(e))


def main() -> None:  # pylint: disable=too-many-statements  # refactor once covered by tests
    """CLI entry point: test different span filter syntaxes to find what works."""
    parser = argparse.ArgumentParser(
        description="Test different span filter syntaxes to find what works"
    )
    parser.add_argument("--trace-id", help="Optional known trace ID to test with")
    args = parser.parse_args()

    print("Testing Span Query Filter Syntaxes")
    print("=" * 60)
    print()

    # Calculate RFC3339 timestamps
    now = datetime.now(timezone.utc)
    one_hour_ago = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    one_day_ago = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seven_days_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ninety_days_ago = (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")

    tests = []

    # Group 1: Empty/minimal filters
    tests.append(("Empty filter (relies on API default)", ""))

    # Group 2: The syntax the scripts send today (discovery_helpers._ensure_span_time_filter).
    tests.append(("Scripts' syntax: quoted date math", SCRIPT_TIME_FILTER))

    # Group 3: Unquoted date math expressions
    tests.append(("Date math: now-1h", "start_time:>=now-1h"))
    tests.append(("Date math: now-24h", "start_time:>=now-24h"))
    tests.append(("Date math: now-7d", "start_time:>=now-7d"))
    tests.append(("Date math: now-90d", "start_time:>=now-90d"))
    tests.append(("Date math with rounding: now-7d/d", "start_time:>=now-7d/d"))

    # Group 3: RFC3339 timestamps (quoted)
    tests.append(("RFC3339 quoted: 1h ago", f'start_time:>="{one_hour_ago}"'))
    tests.append(("RFC3339 quoted: 24h ago", f'start_time:>="{one_day_ago}"'))
    tests.append(("RFC3339 quoted: 7d ago", f'start_time:>="{seven_days_ago}"'))
    tests.append(("RFC3339 quoted: 90d ago", f'start_time:>="{ninety_days_ago}"'))

    # Group 4: RFC3339 timestamps (unquoted - might be required)
    tests.append(("RFC3339 unquoted: 1h ago", f'start_time:>={one_hour_ago}'))
    tests.append(("RFC3339 unquoted: 24h ago", f'start_time:>={one_day_ago}'))

    # Group 5: Alternative operators
    tests.append(("Greater than (>): now-24h", "start_time:>now-24h"))
    tests.append(("Greater than (>) RFC3339", f'start_time:>{one_day_ago}'))

    # Group 6: Alternative field names
    tests.append(("Field: @timestamp", "@timestamp:>=now-24h"))
    tests.append(("Field: timestamp", "timestamp:>=now-24h"))
    tests.append(("Field: created_at", "created_at:>=now-24h"))

    # Group 7: FQL variations
    tests.append(("Brackets: [start_time >=]", "[start_time:>=now-24h]"))
    tests.append(("Space before operator", "start_time :>=now-24h"))
    tests.append(("Space after operator", "start_time:>= now-24h"))

    # Group 8: With trace_id (if provided)
    script_filter = SCRIPT_TIME_FILTER
    if args.trace_id:
        script_filter = f"trace_id:'{args.trace_id}'+{SCRIPT_TIME_FILTER}"
        tests.append(("Scripts' syntax: trace ID + date math", script_filter))
        tests.append(("Trace ID only", f'trace_id:"{args.trace_id}"'))
        tests.append(("Trace ID + date math", f'trace_id:"{args.trace_id}"+start_time:>=now-24h'))
        tests.append(("Trace ID + RFC3339", f'trace_id:"{args.trace_id}"+start_time:>="{one_day_ago}"'))

    # Run tests
    results = []
    for description, filter_expr in tests:
        print(f"Testing: {description}")
        print(f"  Filter: {filter_expr}")

        success, count, error = probe_filter(filter_expr)

        if success:
            print(f"  ✅ SUCCESS - Got {count} span ID(s)")
            results.append((description, filter_expr, True, count))
        else:
            # Shorten error message
            error_short = error[:100] + "..." if len(error) > 100 else error
            print(f"  ❌ FAILED - {error_short}")
            results.append((description, filter_expr, False, 0))

        print()

    # Summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print()

    successful = [r for r in results if r[2]]

    print(f"✅ Successful: {len(successful)}/{len(results)}")
    if successful:
        print("\nWorking filters:")
        for desc, filt, _, count in successful:
            print(f"  • {desc}")
            print(f"    {filt}")
            print(f"    → {count} results")
            print()

    print(f"\n❌ Failed: {len(results) - len(successful)}/{len(results)}")

    if not successful:
        print("\n⚠️ NO FILTERS WORKED!")
        print("\nPossible causes:")
        print("  1. Spans API might not support time-based filtering at all")
        print("  2. Different FQL syntax required (check Charlotte AI AgentWorks API docs)")
        print("  3. Field names might be different")
        print("  4. API might require a different query endpoint")
        print("\nRecommendations:")
        print("  • Check Charlotte AI AgentWorks API documentation for /queries/spans/v1")
        print("  • Try using known invocation IDs directly with inspect_invocation.py")
        print("  • Contact Charlotte AI AgentWorks team for correct filter syntax")

    print("\n" + "=" * 60)
    print("RECOMMENDED APPROACH")
    print("=" * 60)

    # An empty filter isn't useful advice. When a trace ID was requested, only recommend
    # a working time-bounded trace filter, not a time-only filter or an unbounded trace.
    best = next((r for r in successful if r[1] and (
        not args.trace_id or ("trace_id:" in r[1] and "start_time:" in r[1])
    )), None)

    if any(r[1] == script_filter for r in successful):
        print(f"\nThe scripts already use a working syntax ({script_filter}). No change is needed.")
    elif best:
        print("\nUse this filter format:")
        print(f"  {best[1]}")
        print("\nUpdate scripts to use this syntax instead of current format.")
    else:
        if args.trace_id:
            print("\nThe scripts' trace-ID filter failed. No time-bounded trace-ID filter worked.")
        else:
            print("\nSince no time filters work, use fallback strategy:")
        print("  1. If you have invocation IDs:")
        print("     Use: inspect_invocation.py --invocation-id <id>")
        print()
        print("  2. If you need to list invocations:")
        print("     • Get invocation IDs from invoke responses")
        print("     • Store them in a local file/database")
        print("     • Or use a different API endpoint (check docs)")


if __name__ == "__main__":
    main()
