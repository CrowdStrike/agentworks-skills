"""Tests for span analysis and trace inspection: analyze_agent, inspect_invocation, list_invocations.

Each trace below is built by hand in the shape the live API returns (flat dotted
attributes, a root aw_agent span with the definition, an aw_agent_response span with
cost and output, and a tool + tool_response pair per tool call), and every expected
number is worked out from those fixtures by hand.
"""

import json
from unittest.mock import MagicMock

import pytest

import analyze_agent
import inspect_invocation
import list_invocations
from conftest import err, ok


def root(trace, inv, start, model="bedrock.claude-5-opus", agent_id: str | None = "agent-1", prompt="p1", **extra):
    """The aw_agent root span: carries aw_agent.id and the version definition."""
    attrs = {"aw_agent.id": agent_id, "aw_agent.invocation_id": inv, "aw_agent.definition.name": "Hunter",
             "aw_agent.definition.model": model, "aw_agent.definition.system_prompt": prompt,
             "aw_agent.definition.tools": [{"id": "mcp/x/search"}],
             "aw_agent.definition.model_config.temperature": 0.2, **extra}
    if agent_id is None:
        del attrs["aw_agent.id"]
        attrs["aw_agent.inline_invocation"] = True
    return {"id": f"{trace}-root", "trace_id": trace, "span_type": "aw_agent", "name": "agent",
            "status": "ok", "start_time": start, "end_time": start, "attributes": attrs}


def response(trace, inv, end, cost=150, output="done"):
    """The aw_agent_response span: invocation id, cost, and output, but no definition."""
    return {"id": f"{trace}-resp", "trace_id": trace, "span_type": "aw_agent_response", "name": "response",
            "status": "ok", "start_time": end, "end_time": end,
            "attributes": {"aw_agent.invocation_id": inv, "cost.agg_credit_cents": cost, "aw_agent.output": output}}


def tool_call(trace, n, tool="mcp/x/search", error=False, duration=400):
    """One tool call: the 'tool' request span and its 'tool_response' span."""
    return [
        {"id": f"{trace}-t{n}", "trace_id": trace, "span_type": "tool", "name": "Tool", "status": "ok",
         "attributes": {"tool.id": tool}},
        {"id": f"{trace}-r{n}", "trace_id": trace, "span_type": "tool_response", "name": "Tool Response",
         "status": "error" if error else "ok", "duration_ms": duration,
         "attributes": {"tool.id": tool, "tool.result_is_error": error}},
    ]


def trace(name, inv, start, end, calls=(), **kw):
    spans = [root(name, inv, start, **kw), response(name, inv, end)]
    for i, c in enumerate(calls):
        spans += tool_call(name, i, **c)
    return spans


class TestAnalyzeSpans:
    def test_counts_each_tool_call_once(self):
        """Regression: matching 'tool' in span_type counted the request and response spans,
        so 3 calls were reported as 6."""
        spans = trace("t1", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:02Z",
                      calls=[{}, {}, {"tool": "mcp/x/lookup", "error": True}])
        result = analyze_agent.analyze_spans(spans)
        assert result["tool_usage"] == {"mcp/x/search": 2, "mcp/x/lookup": 1}
        assert result["tool_errors"] == {"mcp/x/lookup": 1}
        assert result["saw_tool_telemetry"] is True

    def test_trace_metrics(self):
        spans = (trace("t1", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:02Z", calls=[{}])
                 + trace("t2", "inv-2", "2026-11-16T11:00:00Z", "2026-11-16T11:00:06Z", calls=[{"error": True}]))
        result = analyze_agent.analyze_spans(spans)
        assert result["total_traces"] == 2
        assert sorted(result["invocation_ids"]) == ["inv-1", "inv-2"]
        assert result["trace_statuses"] == {"success": 1, "error": 1}
        assert result["trace_durations"] == {"count": 2, "avg_ms": 4000.0, "max_ms": 6000.0}
        assert result["total_cost_cents"] == 300
        assert result["avg_cost_cents"] == 150

    def test_result_is_error_alone_marks_the_trace_failed(self):
        spans = trace("t1", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:01Z")
        spans += [{"id": "x", "trace_id": "t1", "span_type": "tool_response", "status": "ok",
                   "attributes": {"tool.id": "mcp/x/search", "tool.result_is_error": True}}]
        assert analyze_agent.analyze_spans(spans)["trace_statuses"] == {"error": 1}

    def test_blank_output_counts_only_when_the_trace_succeeded(self):
        ok_blank = [root("t1", "inv-1", "2026-11-16T10:00:00Z"), response("t1", "inv-1", "2026-11-16T10:00:01Z", output="")]
        err_blank = (trace("t2", "inv-2", "2026-11-16T10:00:00Z", "2026-11-16T10:00:01Z", calls=[{"error": True}])[:1]
                     + [response("t2", "inv-2", "2026-11-16T10:00:01Z", output="")]
                     + tool_call("t2", 0, error=True))
        assert analyze_agent.analyze_spans(ok_blank + err_blank)["empty_result_traces"] == 1

    def test_response_span_does_not_create_a_phantom_version(self):
        spans = trace("t1", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:01Z")
        by_version = analyze_agent.analyze_spans(spans)["by_version"]
        assert len(by_version) == 1
        assert next(iter(by_version.values()))["definition"]["model"] == "bedrock.claude-5-opus"

    def test_tool_order_does_not_split_a_version(self):
        a = root("t1", "inv-1", "2026-11-16T10:00:00Z")
        b = root("t2", "inv-2", "2026-11-16T11:00:00Z")
        a["attributes"]["aw_agent.definition.tools"] = [{"id": "x"}, {"id": "y"}]
        b["attributes"]["aw_agent.definition.tools"] = [{"id": "y"}, {"id": "x"}]
        assert len(analyze_agent.analyze_spans([a, b])["by_version"]) == 1

    def test_no_tool_spans_is_reported_as_no_telemetry(self):
        spans = trace("t1", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:01Z")
        result = analyze_agent.analyze_spans(spans)
        assert result["saw_tool_telemetry"] is False and result["tool_usage"] == {}

    def test_spans_with_null_attributes_are_analyzed(self):
        """The API can return attributes: null; .get("attributes", {}) then yields None."""
        spans = trace("t1", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:02Z", calls=[{}])
        spans.append({"id": "bare", "trace_id": "t1", "span_type": "llm", "status": "ok", "attributes": None})
        result = analyze_agent.analyze_spans(spans)
        assert result["total_spans"] == 5 and result["tool_usage"] == {"mcp/x/search": 1}

    def test_empty_input(self):
        result = analyze_agent.analyze_spans([])
        assert result["total_traces"] == 0 and result["avg_cost_cents"] == 0

    def test_tool_without_an_id_is_counted_under_its_span_name(self):
        spans = [{"id": "s", "trace_id": "t1", "span_type": "tool_response", "status": "ok", "name": "Tool Response"}]
        assert analyze_agent.analyze_spans(spans)["tool_usage"] == {"Tool Response": 1}


@pytest.fixture
def spans_api(monkeypatch):
    """Route analyze_agent's span queries through a fake catalog of spans."""
    catalog = []

    def query(filter_expr, max_results, sort="start_time|desc"):
        if "aw_agent.id:'" in filter_expr:
            agent = filter_expr.split("aw_agent.id:'")[1].split("'")[0]
            hits = [s for s in catalog if s["attributes"].get("aw_agent.id") == agent]
        elif "definition.name:'" in filter_expr:
            name = filter_expr.split("definition.name:'")[1].split("'")[0]
            hits = [s for s in catalog if s["attributes"].get("aw_agent.definition.name") == name
                    and s["span_type"] == "aw_agent"]
        elif "invocation_id:'" in filter_expr:
            inv = filter_expr.split("invocation_id:'")[1].split("'")[0]
            hits = [s for s in catalog if s["attributes"].get("aw_agent.invocation_id") == inv]
        else:
            traces = filter_expr.split("trace_id:")[1]
            hits = [s for s in catalog if f"'{s['trace_id']}'" in traces]
        hits = sorted(hits, key=lambda s: s.get("start_time") or "", reverse=True)
        return [s["id"] for s in hits][:max_results]

    def hydrate(ids, batch_size=50):
        by_id = {s["id"]: s for s in catalog}
        return [by_id[i] for i in ids]

    for module in (analyze_agent, list_invocations):
        monkeypatch.setattr(module, "query_span_ids", query)
        monkeypatch.setattr(module, "hydrate_spans", hydrate)
    return catalog


class TestQueryAgentSpans:
    def test_fetches_child_spans_through_the_trace(self, spans_api):
        """Regression: querying on aw_agent.id alone returned only root spans, dropping every
        tool and LLM span the analysis needs."""
        spans_api += trace("t1", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:02Z", calls=[{}, {}])
        ids, roots = analyze_agent.query_agent_spans("agent-1", days=7)
        assert {"t1-r0", "t1-r1", "t1-t0", "t1-t1"} <= set(ids)
        assert [r["id"] for r in roots] == ["t1-root"]

    def test_other_agents_traces_are_dropped(self, spans_api):
        spans_api += trace("t1", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:01Z")
        spans_api += trace("t2", "inv-2", "2026-11-16T10:00:00Z", "2026-11-16T10:00:01Z", agent_id="agent-2")
        ids, _ = analyze_agent.query_agent_spans("agent-1", days=7)
        assert not [i for i in ids if i.startswith("t2")]

    def test_same_named_agent_with_a_different_id_is_dropped(self, spans_api):
        """Agent names aren't unique: the name query also returns another agent's traces, and
        those must not be analyzed as this agent's."""
        spans_api += trace("mine", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:01Z", calls=[{}])
        spans_api += trace("theirs", "inv-2", "2026-11-16T10:00:00Z", "2026-11-16T10:00:01Z",
                           agent_id="agent-2", calls=[{}])
        ids, roots = analyze_agent.query_agent_spans("agent-1", days=7, agent_name="Hunter")
        assert [r["id"] for r in roots] == ["mine-root"]
        assert not [i for i in ids if i.startswith("theirs")]

    def test_inline_invocations_match_by_name(self, spans_api):
        spans_api += trace("t1", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:01Z", agent_id=None)
        ids, _ = analyze_agent.query_agent_spans("agent-1", days=7, agent_name="Hunter")
        assert "t1-root" in ids
        assert not analyze_agent.query_agent_spans("agent-1", days=7)[0]

    def test_cap_warning(self, spans_api, monkeypatch, capsys):
        monkeypatch.setattr(analyze_agent, "MAX_ROOT_SPANS", 2)
        for i in range(3):
            spans_api += trace(f"t{i}", f"inv-{i}", f"2026-11-16T1{i}:00:00Z", f"2026-11-16T1{i}:00:01Z")
        analyze_agent.query_agent_spans("agent-1", days=7)
        assert "hit the 2-root-span cap" in capsys.readouterr().err

    def test_invocation_scope(self, spans_api):
        spans_api += trace("t1", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:01Z", calls=[{}])
        spans_api += trace("t2", "inv-2", "2026-11-16T10:00:00Z", "2026-11-16T10:00:01Z")
        ids, _ = analyze_agent.query_invocation_spans(["inv-1"])
        assert "t1-r0" in ids and not [i for i in ids if i.startswith("t2")]


class TestAnalyzeAgentMain:
    def test_versions_are_ordered_oldest_first(self, spans_api, monkeypatch, run_main):
        """Regression: traces arrive newest-first, so the comparison labelled the newer version
        "Version 1" and swapped first/last seen."""
        spans_api += trace("old1", "inv-1", "2026-11-10T10:00:00Z", "2026-11-10T10:00:09Z", model="m-old", prompt="v1")
        spans_api += trace("old2", "inv-2", "2026-11-11T10:00:00Z", "2026-11-11T10:00:09Z", model="m-old", prompt="v1")
        spans_api += trace("new1", "inv-3", "2026-11-15T10:00:00Z", "2026-11-15T10:00:03Z", model="m-new", prompt="v2")
        agents = MagicMock()
        agents.get_studio_agents.return_value = ok({"resources": [{"id": "agent-1", "active_version": {
            "name": "Hunter", "model": "m-new", "tools": [{"id": "mcp/x/search"}]}}]})
        monkeypatch.setattr(analyze_agent, "get_agents_client", lambda: agents)
        code, out, _ = run_main(analyze_agent, "--agent-id", "agent-1", "--days", "30")
        assert code == 0
        v1 = out.index("Version 1:")
        v2 = out.index("Version 2:")
        assert out.index("Model: m-old", v1) < v2 < out.index("Model: m-new", v2)
        assert "First seen: 2026-11-10T10:00:00Z" in out[v1:v2]
        assert "Last seen: 2026-11-11T10:00:00Z" in out[v1:v2]
        assert "Version 2 is 3.0x faster than Version 1" in out

    def test_no_spans(self, spans_api, monkeypatch, run_main):
        agents = MagicMock()
        agents.get_studio_agents.return_value = ok({"resources": []})
        monkeypatch.setattr(analyze_agent, "get_agents_client", lambda: agents)
        code, out, err_out = run_main(analyze_agent, "--agent-id", "agent-x")
        assert code == 0
        assert "No spans found" in out + err_out


def trace_spans_for_inspect():
    spans = trace("t1", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:02Z",
                  calls=[{}, {}, {"error": True}])
    for i, s in enumerate(spans):
        s.setdefault("start_time", f"2026-11-16T10:00:0{min(i, 2)}Z")
        s.setdefault("end_time", "2026-11-16T10:00:02Z")
        s["span_id"] = s["id"]
        s["parent_span_id"] = None if s["span_type"] == "aw_agent" else "t1-root"
    return spans


class TestInspectInvocation:
    @pytest.fixture
    def inspect_api(self, monkeypatch):
        spans = trace_spans_for_inspect()
        by_id = {s["id"]: s for s in spans}

        def query(filter_expr, max_results, sort="start_time|desc"):
            if "invocation_id" in filter_expr:
                return ["t1-root"] if "'inv-1'" in filter_expr else []
            return [s["id"] for s in spans]

        monkeypatch.setattr(inspect_invocation, "query_span_ids", query)
        monkeypatch.setattr(inspect_invocation, "hydrate_spans", lambda ids, **kw: [by_id[i] for i in ids])
        return spans

    def test_counts_tool_calls_once(self, inspect_api, run_main):
        """Regression: 'tool' in span_type matched tool and tool_response, showing 6 for 3 calls."""
        code, out, _ = run_main(inspect_invocation, "--invocation-id", "inv-1", "--tool-output-only")
        assert code == 0
        assert "Tool Outputs (3" in out and "Tool Outputs (6" not in out

    def test_errors_only_keeps_failed_calls(self, inspect_api, run_main):
        _, out, _ = run_main(inspect_invocation, "--invocation-id", "inv-1", "--tool-output-only", "--errors-only")
        assert "=== Tool Outputs (1 error tool span(s)) ===" in out

    def test_tool_span_with_null_attributes_is_still_listed(self, inspect_api, run_main):
        responses = [s for s in inspect_api if s["span_type"] == "tool_response"]
        responses[0]["attributes"] = None
        code, out, _ = run_main(inspect_invocation, "--invocation-id", "inv-1", "--tool-output-only")
        assert code == 0
        assert "Tool Outputs (3 tool spans)" in out and "Tool #3" in out

    def test_errors_only_with_null_attributes(self, inspect_api, run_main):
        responses = [s for s in inspect_api if s["span_type"] == "tool_response"]
        responses[0]["attributes"] = None
        code, out, _ = run_main(inspect_invocation, "--invocation-id", "inv-1", "--tool-output-only", "--errors-only")
        assert code == 0 and "Tool Outputs (1 error tool span(s))" in out

    def test_unknown_invocation(self, inspect_api, run_main):
        code, out, err_out = run_main(inspect_invocation, "--invocation-id", "inv-nope")
        assert code == 1
        assert "inv-nope" in out + err_out


class TestListInvocations:
    def test_sorts_with_missing_start_times(self):
        """Regression: x.get("start_time", "") returns None when the key holds null, and sorting
        None against str raised TypeError."""
        spans = [
            root("t1", "inv-1", "2026-11-16T10:00:00Z"),
            dict(root("t2", "inv-2", "2026-11-16T12:00:00Z"), start_time=None, end_time=None),
            root("t3", "inv-3", "2026-11-16T11:00:00Z"),
        ]
        invs = list_invocations.extract_invocations(spans, target_agent_id="agent-1")
        assert [i["invocation_id"] for i in invs] == ["inv-3", "inv-1", "inv-2"]

    def test_inline_runs_match_only_by_name(self):
        spans = [root("t1", "inv-1", "2026-11-16T10:00:00Z", agent_id=None),
                 root("t2", "inv-2", "2026-11-16T10:00:00Z", agent_id="agent-2")]
        invs = list_invocations.extract_invocations(spans, target_agent_id="agent-1", target_agent_name="Hunter")
        assert [(i["invocation_id"], i["inline"]) for i in invs] == [("inv-1", True)]

    def test_without_a_name_inline_runs_are_not_attributed(self):
        spans = [root("t1", "inv-1", "2026-11-16T10:00:00Z", agent_id=None)]
        assert not list_invocations.extract_invocations(spans, target_agent_id="agent-1")

    def test_trace_stats_with_null_attributes(self):
        spans = [dict(root("t1", "inv-1", "2026-11-16T10:00:00Z"), attributes=None)]
        assert list_invocations._trace_stats(spans)["status"] == "success"  # pylint: disable=protected-access

    def test_trace_stats_flag_errors_and_duration(self):
        spans = trace("t1", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:05Z", calls=[{"error": True}])
        for s in spans:
            s.setdefault("start_time", "2026-11-16T10:00:01Z")
            s.setdefault("end_time", "2026-11-16T10:00:04Z")
        stats = list_invocations._trace_stats(spans)  # pylint: disable=protected-access
        assert stats["status"] == "error"
        assert stats["duration_ms"] == 5000.0
        assert stats["span_count"] == 4


class TestListInvocationsMain:
    @pytest.fixture
    def agents(self, monkeypatch):
        client = MagicMock()
        client.get_studio_agents.return_value = ok({"resources": [{"id": "agent-1", "active_version": {"name": "Hunter"}}]})
        monkeypatch.setattr(list_invocations, "get_agents_client", lambda: client)
        return client

    def test_failed_tool_call_marks_the_invocation_as_error(self, spans_api, agents, run_main):
        """The root spans say "ok"; only the full trace shows the tool call that failed."""
        spans_api += trace("good", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:02Z", calls=[{}])
        spans_api += trace("bad", "inv-2", "2026-11-16T11:00:00Z", "2026-11-16T11:00:05Z", calls=[{"error": True}])
        code, out, _ = run_main(list_invocations, "--agent-id", "agent-1", "--json")
        invs = {i["invocation_id"]: i for i in json.loads(out)["invocations"]}
        assert code == 0
        assert (invs["inv-1"]["status"], invs["inv-2"]["status"]) == ("success", "error")
        assert invs["inv-2"]["span_count"] == 4 and invs["inv-2"]["duration_ms"] == 5000.0

    def test_newest_first_and_limit(self, spans_api, agents, run_main):
        for i in range(4):
            spans_api += trace(f"t{i}", f"inv-{i}", f"2026-11-1{i}T10:00:00Z", f"2026-11-1{i}T10:00:01Z")
        _, out, _ = run_main(list_invocations, "--agent-id", "agent-1", "--limit", "2", "--json")
        assert [i["invocation_id"] for i in json.loads(out)["invocations"]] == ["inv-3", "inv-2"]

    def test_inline_runs_are_labelled_in_the_text_output(self, spans_api, agents, run_main):
        spans_api += trace("t1", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:01Z", agent_id=None)
        _, out, _ = run_main(list_invocations, "--agent-id", "agent-1")
        assert "Invocation: inv-1 (inline, matched by agent name)" in out

    def test_name_lookup_failure_still_lists_runs_matched_by_id(self, spans_api, agents, run_main):
        agents.get_studio_agents.return_value = err(403, "missing scope")
        spans_api += trace("t1", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:01Z")
        spans_api += trace("t2", "inv-2", "2026-11-16T10:00:00Z", "2026-11-16T10:00:01Z", agent_id=None)
        code, out, err_out = run_main(list_invocations, "--agent-id", "agent-1", "--json")
        assert code == 0
        assert "inline invocations will not be matched" in err_out
        assert [i["invocation_id"] for i in json.loads(out)["invocations"]] == ["inv-1"]

    def test_no_invocations(self, spans_api, agents, run_main):
        code, out, err_out = run_main(list_invocations, "--agent-id", "agent-1", "--days", "3", "--json")
        assert code == 0
        assert json.loads(out) == {"agent_id": "agent-1", "invocations": []}
        assert "No invocations found for agent agent-1 in the last 3 day(s)." in err_out

    @pytest.mark.parametrize("limit", ["0", "-1"])
    def test_limit_below_one_is_rejected(self, spans_api, agents, run_main, limit):
        code, _, err_out = run_main(list_invocations, "--agent-id", "agent-1", "--limit", limit)
        assert code != 0 and "--limit must be >= 1" in err_out

    def test_a_trace_at_the_span_cap_is_refetched_alone_with_a_warning(self, spans_api, agents, monkeypatch, run_main, capsys):
        monkeypatch.setattr(list_invocations, "MAX_SPANS_PER_BATCH", 3)
        monkeypatch.setattr(list_invocations, "MAX_SPANS_PER_TRACE", 3)
        spans_api += trace("big", "inv-1", "2026-11-16T10:00:00Z", "2026-11-16T10:00:02Z", calls=[{}, {}])
        code, _, err_out = run_main(list_invocations, "--agent-id", "agent-1", "--json")
        assert code == 0
        assert "trace big has at least 3 spans" in err_out



def analysis(**overrides):
    """An analyze_spans() result for a healthy agent; tests override one thing at a time."""
    base = {"total_traces": 10, "trace_statuses": {"success": 10}, "tool_usage": {"search": 10},
            "tool_errors": {}, "tool_durations": {"search": {"count": 10, "avg_ms": 400, "max_ms": 900}},
            "trace_durations": {"count": 10, "avg_ms": 5000, "max_ms": 9000}, "saw_tool_telemetry": True,
            "total_cost_cents": 0, "avg_cost_cents": 0, "empty_result_traces": 0}
    return {**base, **overrides}


def recommend(result, tools=("search",), patterns=None):
    details = {"active_version": {"tools": [{"id": t, "name": ""} for t in tools]}} if tools is not None else None
    return analyze_agent.generate_recommendations(result, details, patterns or {"tool_call_patterns": {}})


def mentions(recs, text):
    return [r for r in recs if text in r]


class TestRecommendations:
    def test_a_healthy_agent_gets_no_warnings(self):
        recs = recommend(analysis())
        assert not [r for r in recs if "⚠️" in r or "📋" in r or "⏱️" in r or "❓" in r]

    def test_unused_tools_are_named(self):
        recs = recommend(analysis(), tools=("search", "lookup", "enrich"))
        assert mentions(recs, "Unused tools detected: enrich, lookup.")

    def test_no_tool_telemetry_never_reports_tools_as_unused(self):
        """Regression: with zero tool spans fetched, every configured tool looked unused, and
        acting on that would strip working tools from a healthy agent."""
        recs = recommend(analysis(tool_usage={}, saw_tool_telemetry=False), tools=("search", "lookup"))
        assert mentions(recs, "No tool telemetry in scope")
        assert not mentions(recs, "Unused tools detected")

    def test_tools_are_matched_by_id_when_the_name_is_blank(self):
        assert not mentions(recommend(analysis(tool_usage={"mcp/x/search": 3}), tools=("mcp/x/search",)), "Unused")

    def test_without_agent_details_nothing_is_said_about_unused_tools(self):
        recs = recommend(analysis(), tools=None)
        assert not mentions(recs, "Unused") and not mentions(recs, "No tool telemetry")

    @pytest.mark.parametrize("failed, warned", [(2, False), (3, True)])
    def test_trace_error_rate_warns_above_20_percent(self, failed, warned):
        recs = recommend(analysis(trace_statuses={"success": 10 - failed, "error": failed}))
        assert bool(mentions(recs, "High error rate")) is warned

    @pytest.mark.parametrize("errors, warned", [(3, False), (4, True)])
    def test_tool_error_rate_warns_above_30_percent(self, errors, warned):
        recs = recommend(analysis(tool_errors={"search": errors}))
        assert bool(mentions(recs, "Tool 'search' has high error rate")) is warned

    @pytest.mark.parametrize("avg_ms, warned", [(30000, False), (30001, True)])
    def test_slow_tool_warns_above_30_seconds(self, avg_ms, warned):
        durations = {"search": {"count": 10, "avg_ms": avg_ms, "max_ms": avg_ms}}
        assert bool(mentions(recommend(analysis(tool_durations=durations)), "Tool 'search' is slow")) is warned

    @pytest.mark.parametrize("avg_ms, warned", [(60000, False), (60001, True)])
    def test_slow_invocations_warn_above_one_minute(self, avg_ms, warned):
        trace = {"count": 10, "avg_ms": avg_ms, "max_ms": avg_ms}
        assert bool(mentions(recommend(analysis(trace_durations=trace)), "Average invocation duration")) is warned

    def test_empty_results_are_reported(self):
        assert mentions(recommend(analysis(empty_result_traces=2)), "2 trace(s) completed successfully but returned")

    def test_cost_is_reported_in_credits(self):
        recs = recommend(analysis(total_cost_cents=1500, avg_cost_cents=150))
        assert mentions(recs, "Average cost per invocation: 1.50 credits (total: 15.00 credits across 10 traces).")

    def test_no_traces_does_not_divide_by_zero(self):
        recs = recommend(analysis(total_traces=0, trace_statuses={}, tool_usage={}, tool_durations={}))
        assert isinstance(recs, list)
