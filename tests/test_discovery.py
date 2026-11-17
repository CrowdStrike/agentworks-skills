"""Tests for the discovery scripts and compare_versions.py."""

import json
from unittest.mock import MagicMock

import pytest

import compare_versions
import models_search
import spans_get
import spans_search
import templates_search
import tools_get
import tools_search
import versions_search
from conftest import err, ok


def tool(tool_id, name, category="falcon_platform", description=""):
    return {"id": tool_id, "name": name, "category": category, "description": description}


@pytest.fixture
def tools_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(tools_get, "get_tools_client", lambda: client)
    return client


def serve_tools(client, tools):
    """Page and hydrate `tools` the way queries_tools_v1 / entities_tools_v1 do."""
    def query(parameters):
        pool = tools
        if "filter" in parameters:
            category = parameters["filter"].split("'")[1]
            pool = [t for t in tools if t["category"] == category]
        off, lim = parameters.get("offset", 0), parameters["limit"]
        return ok({"resources": [t["id"] for t in pool[off:off + lim]], "meta": {"pagination": {"total": len(pool)}}})
    by_id = {t["id"]: t for t in tools}
    client.queries_tools_v1.side_effect = query
    client.entities_tools_v1.side_effect = lambda ids: ok({"resources": [by_id[i] for i in ids if i in by_id]})


class TestToolsGet:
    def test_search_sees_tools_past_the_first_page(self, tools_client, run_main):
        """Regression: --search only filtered the first --limit IDs, so a match at position 250
        on a 400-tool tenant was never found."""
        tools = [tool(f"t-{i}", f"Tool {i}") for i in range(400)]
        tools[250] = tool("t-250", "Vulnerability Lookup")
        serve_tools(tools_client, tools)
        code, out, _ = run_main(tools_get, "--search", "vulnerability", "--json")
        assert code == 0
        assert [t["id"] for t in json.loads(out)["resources"]] == ["t-250"]

    def test_limit_applies_to_matches_when_searching(self, tools_client, run_main):
        serve_tools(tools_client, [tool(f"t-{i}", f"Host Query {i}") for i in range(30)])
        _, out, _ = run_main(tools_get, "--search", "host", "--limit", "5", "--json")
        assert json.loads(out)["count"] == 5

    def test_without_search_only_limit_ids_are_fetched(self, tools_client, run_main):
        serve_tools(tools_client, [tool(f"t-{i}", f"Tool {i}") for i in range(400)])
        _, out, _ = run_main(tools_get, "--limit", "7", "--json")
        assert json.loads(out)["count"] == 7
        assert tools_client.queries_tools_v1.call_count == 1

    def test_search_matches_name_description_or_category(self, tools_client, run_main):
        serve_tools(tools_client, [
            tool("n", "Detections Search"), tool("d", "Other", description="lists DETECTIONS by host"),
            tool("c", "Thing", category="detections"), tool("x", "Unrelated"),
        ])
        _, out, _ = run_main(tools_get, "--search", "detections", "--json")
        assert sorted(t["id"] for t in json.loads(out)["resources"]) == ["c", "d", "n"]

    def test_brand_words_alone_do_not_match_everything(self, tools_client, run_main):
        """'falcon' is dropped from a multi-word search so it doesn't match every tool."""
        serve_tools(tools_client, [tool("a", "Falcon Hosts"), tool("b", "Falcon Detections")])
        _, out, _ = run_main(tools_get, "--search", "falcon hosts", "--json")
        assert [t["id"] for t in json.loads(out)["resources"]] == ["a"]

    def test_category_is_exact_and_case_sensitive(self, tools_client, run_main):
        serve_tools(tools_client, [tool("a", "A", category="Collections"), tool("b", "B", category="collections")])
        _, out, _ = run_main(tools_get, "--category", "Collections", "--json")
        assert tools_client.queries_tools_v1.call_args.kwargs["parameters"]["filter"] == "category:'Collections'"
        assert [t["id"] for t in json.loads(out)["resources"]] == ["a"]

    def test_category_is_rechecked_after_hydration(self, tools_client, run_main):
        """If the query filter matches loosely (or is ignored), wrong-category tools are dropped."""
        tools_client.queries_tools_v1.return_value = ok({"resources": ["a", "b"], "meta": {"pagination": {"total": 2}}})
        tools_client.entities_tools_v1.return_value = ok({"resources": [
            tool("a", "A", category="Collections"), tool("b", "B", category="collections")]})
        _, out, _ = run_main(tools_get, "--category", "Collections", "--json")
        assert [t["id"] for t in json.loads(out)["resources"]] == ["a"]

    def test_ids_are_hydrated_in_batches_of_50(self, tools_client, run_main):
        tools = [tool(f"t-{i}", f"Tool {i}") for i in range(120)]
        serve_tools(tools_client, tools)
        _, out, _ = run_main(tools_get, "--ids", *[t["id"] for t in tools], "--json")
        assert [len(c.kwargs["ids"]) for c in tools_client.entities_tools_v1.call_args_list] == [50, 50, 20]
        assert json.loads(out)["count"] == 120
        tools_client.queries_tools_v1.assert_not_called()

    def test_no_tools(self, tools_client, run_main):
        serve_tools(tools_client, [])
        assert "No tools found." in run_main(tools_get)[1]

    def test_list_categories_counts_without_hydrating(self, tools_client, run_main):
        serve_tools(tools_client, [tool("a", "A", category="gce"), tool("b", "B", category="gce")])
        code, out, _ = run_main(tools_get, "--list-categories")
        assert code == 0
        row = next(line for line in out.splitlines() if line.startswith("gce"))
        assert row.split() == ["gce", "2"]
        tools_client.entities_tools_v1.assert_not_called()

    def test_long_descriptions_are_truncated(self, tools_client, run_main):
        serve_tools(tools_client, [tool("a", "A", description="d" * 150)])
        _, out, _ = run_main(tools_get)
        assert "d" * 100 + "..." in out and "d" * 101 not in out

    def test_query_error_raises(self, tools_client, run_main):
        tools_client.queries_tools_v1.return_value = err(403, "missing scope")
        with pytest.raises(RuntimeError, match="HTTP 403"):
            run_main(tools_get)


@pytest.mark.parametrize("module, factory, method, plural, time_filter", [
    (spans_search, "get_spans_client", "queries_spans_v1", "span", True),
    (tools_search, "get_tools_client", "queries_tools_v1", "tool", False),
    (templates_search, "get_agent_templates_client", "queries_agent_templates_v1", "template", False),
    (versions_search, "get_agent_versions_client", "query_agent_versions_v1", "version", False),
])
class TestSearchScripts:
    def test_calls_its_own_endpoint(self, monkeypatch, run_main, module, factory, method, plural, time_filter):
        client = MagicMock()
        getattr(client, method).return_value = ok({"resources": ["id-1"], "meta": {"pagination": {"total": 1}}})
        monkeypatch.setattr(module, factory, lambda: client)
        code, out, _ = run_main(module, "--limit", "3")
        params = getattr(client, method).call_args.kwargs["parameters"]
        assert code == 0 and "id-1" in out and f"1 {plural} ID(s)" in out
        assert params["limit"] == 3
        assert ("filter" in params) is time_filter

    def test_help_needs_no_credentials(self, monkeypatch, run_main, module, factory, method, plural, time_filter):
        """--help must work before any client (and so any credential lookup) is built."""
        build = MagicMock(side_effect=AssertionError("client built for --help"))
        monkeypatch.setattr(module, factory, build)
        assert run_main(module, "--help")[0] == 0


class TestModelsSearch:
    def test_lists_model_ids(self, monkeypatch, run_main):
        client = MagicMock()
        client.queries_models_v1.return_value = ok({"resources": ["bedrock.claude-5-opus", "openai.gpt-6-sol"],
                                                    "meta": {"pagination": {"total": 12}}})
        monkeypatch.setattr(models_search, "get_models_client", lambda: client)
        _, out, _ = run_main(models_search, "--filter", "id:'x'", "--offset", "10")
        client.queries_models_v1.assert_called_once_with(parameters={"limit": 100, "offset": 10, "filter": "id:'x'"})
        assert "Found 2 model ID(s) (total: 12):" in out and "  openai.gpt-6-sol" in out

    def test_none_found(self, monkeypatch, run_main):
        client = MagicMock()
        client.queries_models_v1.return_value = ok({"resources": []})
        monkeypatch.setattr(models_search, "get_models_client", lambda: client)
        assert "No models found." in run_main(models_search)[1]


class TestSpansGet:
    @pytest.fixture
    def hydrate(self, monkeypatch):
        fake = MagicMock()
        monkeypatch.setattr(spans_get, "hydrate_spans", fake)
        return fake

    def test_prints_each_span(self, hydrate, run_main):
        hydrate.return_value = [
            {"span_id": "s-1", "name": "search", "span_type": "tool_response", "duration_ms": 61000,
             "status": "error", "trace_id": "abcdef1234567890", "attributes": {"cost.raw_credit_cents": 250}},
            {"id": "s-2", "name": "llm", "span_type": "llm", "duration_ms": 850, "status": "ok", "trace_id": "abcdef12"},
        ]
        code, out, _ = run_main(spans_get, "--ids", "s-1", "s-2", "--batch-size", "10")
        hydrate.assert_called_once_with(["s-1", "s-2"], batch_size=10)
        assert code == 0
        assert "Duration: 1m01s  ⚠ error" in out and "Credits: 2.50" in out
        assert "ID: s-2" in out and "Duration: 850ms" in out

    def test_span_with_null_attributes_does_not_crash(self, hydrate, run_main):
        hydrate.return_value = [{"span_id": "s-1", "name": "n", "trace_id": "t", "attributes": None}]
        assert run_main(spans_get, "--ids", "s-1")[0] == 0

    def test_span_with_null_duration_shows_a_dash(self, hydrate, run_main):
        hydrate.return_value = [{"span_id": "s-1", "name": "n", "trace_id": "t", "duration_ms": None}]
        assert "Duration: —" in run_main(spans_get, "--ids", "s-1")[1]

    def test_none_found(self, hydrate, run_main):
        hydrate.return_value = []
        assert "No spans found for 1 ID(s)." in run_main(spans_get, "--ids", "nope")[2]


class FakeClock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


ANALYSIS = {"trace_durations": {"avg_ms": 1500.0}, "total_cost_cents": 320, "tool_usage": {"search": 2},
            "tool_errors": {"search": 1}, "empty_result_traces": 0, "saw_tool_telemetry": True}


@pytest.fixture
def compare(monkeypatch):
    client, clock = MagicMock(), FakeClock()
    monkeypatch.setattr(compare_versions, "get_agent_invocation_client", lambda: client)
    monkeypatch.setattr(compare_versions.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(compare_versions.time, "sleep", clock.sleep)
    monkeypatch.setattr(compare_versions, "query_invocation_spans", lambda ids: (["s-1"], [{"id": "s-1"}]))
    monkeypatch.setattr(compare_versions, "hydrate_spans", lambda ids: [])
    monkeypatch.setattr(compare_versions, "analyze_spans", lambda spans: dict(ANALYSIS))
    client.invoke_agent_version_external_v1.side_effect = [ok({"resources": [{"id": "inv-a"}]}),
                                                           ok({"resources": [{"id": "inv-b"}]})]
    client.clock = clock
    return client


def done(status="completed"):
    return ok({"resources": [{"id": "x", "status": status}]})


ARGS = ["--agent-id", "agent-1", "--version-id-a", "ver-aaaaaaaa", "--version-id-b", "ver-bbbbbbbb", "--message", "hi"]


class TestCompareVersions:
    def test_invokes_both_versions_with_the_same_message(self, compare, run_main):
        compare.get_agent_invocation_v3.return_value = done()
        code, out, _ = run_main(compare_versions, *ARGS, "--json")
        bodies = [c.kwargs["body"] for c in compare.invoke_agent_version_external_v1.call_args_list]
        assert [b["version_id"] for b in bodies] == ["ver-aaaaaaaa", "ver-bbbbbbbb"]
        assert all(b["id"] == "agent-1" and b["messages"] == [{"role": "user", "content": "hi"}] for b in bodies)
        summary = json.loads(out)
        assert code == 0
        assert (summary["version_a"]["invocation_id"], summary["version_b"]["invocation_id"]) == ("inv-a", "inv-b")
        assert summary["version_a"]["duration_ms"] == 1500.0 and summary["version_a"]["total_cost_cents"] == 320

    def test_table(self, compare, run_main):
        compare.get_agent_invocation_v3.return_value = done()
        _, out, _ = run_main(compare_versions, *ARGS)
        assert "A (ver-aaaa...)" in out and "B (ver-bbbb...)" in out
        assert "3.20 credits" in out and "search: 2 (1 errors)" in out

    def test_waits_through_cancelling(self, compare, run_main):
        compare.get_agent_invocation_v3.side_effect = [done("processing"), done("cancelling"), done("cancelled"), done()]
        run_main(compare_versions, *ARGS, "--json")
        assert compare.get_agent_invocation_v3.call_count == 4

    def test_unknown_invocation_fails_fast(self, compare, run_main):
        """Regression: an empty resources list polled a bad ID for 2x --timeout."""
        compare.get_agent_invocation_v3.return_value = ok({"resources": []})
        with pytest.raises(RuntimeError, match="No invocation found for ID inv-a"):
            run_main(compare_versions, *ARGS)
        assert compare.get_agent_invocation_v3.call_count == 1 and not compare.clock.sleeps

    def test_permanent_error_is_not_retried(self, compare, run_main):
        compare.get_agent_invocation_v3.return_value = err(403, "missing scope")
        with pytest.raises(RuntimeError, match="HTTP 403"):
            run_main(compare_versions, *ARGS)
        assert compare.get_agent_invocation_v3.call_count == 1

    def test_transient_error_is_retried(self, compare, run_main):
        compare.get_agent_invocation_v3.side_effect = [err(503, "busy"), done(), done()]
        code, _, err_out = run_main(compare_versions, *ARGS, "--json")
        assert code == 0 and "transient error while polling inv-a" in err_out

    def test_timeout_compares_partial_state_with_a_warning(self, compare, run_main):
        compare.get_agent_invocation_v3.return_value = done("processing")
        code, _, err_out = run_main(compare_versions, *ARGS, "--timeout", "30", "--json")
        assert code == 0
        assert "invocation inv-a still 'processing' after 30s" in err_out
        assert compare.clock.now == pytest.approx(60)

    def test_invoke_returning_no_resource_exits(self, compare, run_main):
        compare.invoke_agent_version_external_v1.side_effect = [ok({"resources": []})]
        code, _, err_out = run_main(compare_versions, *ARGS)
        assert code == 1 and "returned no resource" in err_out
        compare.get_agent_invocation_v3.assert_not_called()


class TestProbeSpanFilters:
    """probe_span_filters.py tries filter syntaxes against the live API and recommends one."""

    SCRIPT_SYNTAX = "start_time:>='now-24h'"  # what discovery_helpers and every script send

    @pytest.fixture
    def probe(self, monkeypatch):
        import probe_span_filters  # pylint: disable=import-outside-toplevel

        client = MagicMock()
        monkeypatch.setattr(probe_span_filters, "get_spans_client", lambda: client)
        return probe_span_filters, client

    def accept_only(self, client, accepted):
        def query(parameters):
            if parameters["filter"] in accepted:
                return ok({"resources": ["s-1"]})
            return err(400, "invalid filter")
        client.queries_spans_v1.side_effect = query

    def test_it_probes_the_syntax_the_scripts_actually_use(self, probe, run_main):
        """Regression: the probe never tried the single-quoted form every script sends, so it
        could recommend switching away from syntax that works."""
        module, client = probe
        self.accept_only(client, {""})
        run_main(module)
        tried = [c.kwargs["parameters"]["filter"] for c in client.queries_spans_v1.call_args_list]
        assert self.SCRIPT_SYNTAX in tried

    def test_when_the_scripts_syntax_works_it_says_no_change_is_needed(self, probe, run_main):
        module, client = probe
        self.accept_only(client, {"", self.SCRIPT_SYNTAX, "start_time:>=now-24h"})
        _, out, _ = run_main(module)
        assert "The scripts already use a working syntax" in out
        assert "Update scripts" not in out

    def test_when_only_another_syntax_works_it_recommends_that_one(self, probe, run_main):
        module, client = probe
        self.accept_only(client, {"", "start_time:>=now-7d"})
        _, out, _ = run_main(module)
        assert "Use this filter format:\n  start_time:>=now-7d" in out

    def test_the_empty_filter_is_never_the_recommendation(self, probe, run_main):
        module, client = probe
        self.accept_only(client, {""})
        _, out, _ = run_main(module)
        assert "Since no time filters work" in out and "Use this filter format" not in out

    def test_a_failing_probe_is_reported_not_raised(self, probe, run_main):
        module, client = probe
        client.queries_spans_v1.side_effect = ConnectionError("reset")
        code, out, _ = run_main(module)
        assert code == 0 and "reset" in out

    def test_trace_id_adds_trace_probes(self, probe, run_main):
        module, client = probe
        self.accept_only(client, {""})
        run_main(module, "--trace-id", "abc123")
        tried = [c.kwargs["parameters"]["filter"] for c in client.queries_spans_v1.call_args_list]
        assert "trace_id:'abc123'+start_time:>='now-24h'" in tried
