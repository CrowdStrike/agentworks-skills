"""Tests for the agent list, get, search, and publish scripts."""

import json
from unittest.mock import MagicMock

import pytest

import agent_get
import agent_list
import agent_publish
import agent_search
import discovery_helpers
from conftest import err, ok


def agent(agent_id, name, updated_at="2026-10-05T14:03:09Z", published=True, **version):
    """An agent entity as get_studio_agents returns it."""
    return {"id": agent_id, "is_deleted": False, "published_version_ids": ["ver-1"] if published else [],
            "active_version": {"id": "ver-1", "name": name, "description": f"{name} agent", "model": "bedrock.claude-5-opus",
                               "updated_at": updated_at, "is_published": published,
                               "created_by": {"name": "Ana Silva"}, "updated_by": {"api_client_id": "client-9"},
                               "tools": [{"id": "t"}], "knowledge_base_ids": ["kb-1", "kb-2"], **version}}


@pytest.fixture
def agents(monkeypatch):
    client = MagicMock()
    for module in (agent_list, agent_get, agent_publish, agent_search):
        monkeypatch.setattr(module, "get_agents_client", lambda: client)
    return client


def serve(client, entities, report_total=True):
    def query(parameters):
        off, lim = parameters["offset"], parameters["limit"]
        body = {"resources": [e["id"] for e in entities[off:off + lim]]}
        if report_total:
            body["meta"] = {"pagination": {"total": len(entities)}}
        return ok(body)
    by_id = {e["id"]: e for e in entities}
    client.query_studio_agents.side_effect = query
    client.get_studio_agents.side_effect = lambda ids: ok({"resources": [by_id[i] for i in ids]})


def rows(out):
    return json.loads(out)["rows"]


class TestAgentList:
    def test_pages_every_agent(self, agents, run_main):
        serve(agents, [agent(f"a-{i}", f"Agent {i:03d}") for i in range(230)])
        _, out, _ = run_main(agent_list, "--json", "--limit", "500")
        assert json.loads(out)["total_matched"] == 230
        assert [len(c.kwargs["ids"]) for c in agents.get_studio_agents.call_args_list] == [100, 100, 30]

    def test_pages_past_100_when_the_api_reports_no_total(self, agents, run_main):
        serve(agents, [agent(f"a-{i}", f"Agent {i:03d}") for i in range(230)], report_total=False)
        _, out, _ = run_main(agent_list, "--json", "--limit", "500")
        assert json.loads(out)["total_matched"] == 230

    def test_query_window_keeps_rows_and_warns_without_breaking_json(self, agents, run_main, monkeypatch):
        monkeypatch.setattr(discovery_helpers, "QUERY_RESULT_WINDOW", 100)
        serve(agents, [agent(f"a-{i}", f"Agent {i:03d}") for i in range(101)], report_total=False)
        code, out, err_out = run_main(agent_list, "--json", "--limit", "500")
        assert code == 0 and len(rows(out)) == 100
        assert agents.query_studio_agents.call_count == 1
        assert "may be incomplete" in err_out

    def test_default_is_most_recently_modified_first(self, agents, run_main):
        serve(agents, [agent("old", "Old", "2026-09-01T00:00:00Z"), agent("new", "New", "2026-10-09T00:00:00Z")])
        _, out, _ = run_main(agent_list, "--json")
        assert [r["id"] for r in rows(out)] == ["new", "old"]

    def test_status_filter(self, agents, run_main):
        serve(agents, [agent("p", "Pub"), agent("d", "Draft", published=False)])
        assert [r["id"] for r in rows(run_main(agent_list, "--json", "--status", "published")[1])] == ["p"]
        assert [r["id"] for r in rows(run_main(agent_list, "--json", "--status", "unpublished")[1])] == ["d"]

    def test_name_and_creator_filters_ignore_case(self, agents, run_main):
        serve(agents, [agent("a", "Root Access Hunter"), agent("b", "Triage", created_by={"name": "Bob"})])
        assert [r["id"] for r in rows(run_main(agent_list, "--json", "--name-contains", "root ACCESS")[1])] == ["a"]
        assert [r["id"] for r in rows(run_main(agent_list, "--json", "--created-by", "BOB")[1])] == ["b"]

    def test_date_range_compares_instants(self, agents, run_main):
        serve(agents, [agent("late", "Late", "2026-10-05T23:30:00-06:00"), agent("early", "Early", "2026-10-05T10:00:00Z"),
                       agent("after", "After", "2026-10-07T00:00:00Z")])
        _, out, _ = run_main(agent_list, "--json", "--modified-since", "2026-10-06", "--modified-before", "2026-10-07")
        assert [r["id"] for r in rows(out)] == ["late"]

    def test_bad_date_is_rejected_before_any_api_call(self, agents, run_main):
        serve(agents, [agent("a", "A")])
        code, _, err_out = run_main(agent_list, "--modified-since", "last week")
        assert code == 2
        assert "not an ISO date/datetime: 'last week'" in err_out
        agents.query_studio_agents.assert_not_called()

    def test_null_name_does_not_crash(self, agents, run_main):
        """The API can return name: null on a draft; .get("name", "") then yields None."""
        serve(agents, [agent("a", None), agent("b", "Beta")])
        code, out, _ = run_main(agent_list, "--json", "--name-contains", "beta")
        assert code == 0
        assert [r["id"] for r in rows(out)] == ["b"]
        assert run_main(agent_list, "--sort", "name")[0] == 0

    def test_agent_without_an_active_version_is_listed(self, agents, run_main):
        serve(agents, [{"id": "bare", "active_version": None}])
        code, out, _ = run_main(agent_list, "--json")
        assert code == 0 and rows(out)[0]["id"] == "bare"

    def test_table_and_trailer(self, agents, run_main):
        serve(agents, [agent(f"a-{i}", f"Agent {i:02d}") for i in range(25)])
        _, out, _ = run_main(agent_list, "--limit", "10", "--sort", "name", "--order", "asc")
        assert "| Agent 00 | Agent 00 agent | Oct. 05, 2026 14:03:09 | client-9 (API client) | Ana Silva | Published |" in out
        assert "Showing 1-10 of 25 matching agent(s)." in out
        assert "MORE_RESULTS_AVAILABLE next_offset=10 remaining=15" in out

    def test_api_error_raises(self, agents, run_main):
        agents.query_studio_agents.return_value = err(403, "missing scope")
        with pytest.raises(RuntimeError, match="HTTP 403"):
            run_main(agent_list)


class TestAgentGet:
    def test_ids_are_sent_as_a_list(self, agents, run_main):
        """A comma-joined string is treated as one literal ID by the API."""
        agents.get_studio_agents.return_value = ok({"resources": [agent("a-1", "Hunter")]})
        code, out, _ = run_main(agent_get, "--ids", "a-1", "a-2")
        agents.get_studio_agents.assert_called_once_with(ids=["a-1", "a-2"])
        assert code == 0
        assert "Name: Hunter" in out and "Tools: 1 attached" in out and "Knowledge Bases: 2 attached" in out

    def test_draft_only_agent(self, agents, run_main):
        agents.get_studio_agents.return_value = ok({"resources": [agent("a-1", "Draft", published=False, description=None)]})
        _, out, _ = run_main(agent_get, "--ids", "a-1")
        assert "Published version IDs: (none - draft only)" in out
        assert "Description: (none)" in out

    def test_unknown_ids(self, agents, run_main):
        agents.get_studio_agents.return_value = ok({"resources": []})
        assert "No agents found for given IDs." in run_main(agent_get, "--ids", "nope")[1]


class TestAgentSearch:
    def test_filter_and_paging_are_passed_through(self, agents, run_main):
        agents.query_studio_agents.return_value = ok({"resources": ["a-1", "a-2"], "meta": {"pagination": {"total": 40}}})
        code, out, _ = run_main(agent_search, "--filter", "active_version.name:~'root'", "--limit", "2", "--offset", "4")
        agents.query_studio_agents.assert_called_once_with(
            parameters={"limit": 2, "offset": 4, "filter": "active_version.name:~'root'"})
        assert code == 0 and "a-2" in out and "40" in out

    def test_no_filter_sends_no_filter_key(self, agents, run_main):
        agents.query_studio_agents.return_value = ok({"resources": []})
        _, out, _ = run_main(agent_search)
        assert "filter" not in agents.query_studio_agents.call_args.kwargs["parameters"]
        assert "No agents found matching filter." in out


class TestAgentPublish:
    def test_sends_both_ids_and_is_published(self, agents, run_main):
        """The agent ID alone is not enough; the API needs the version ID too."""
        agents.update_agent.return_value = ok({"resources": [agent("a-1", "Hunter")]})
        code, out, _ = run_main(agent_publish, "--id", "a-1", "--version-id", "ver-1")
        agents.update_agent.assert_called_once_with(id="a-1", version_id="ver-1", is_published=True)
        assert code == 0 and "published successfully" in out and "Version ID: ver-1" in out

    def test_no_resource_returned_exits_1(self, agents, run_main):
        agents.update_agent.return_value = ok({"resources": []})
        code, _, err_out = run_main(agent_publish, "--id", "a-1", "--version-id", "ver-1")
        assert code == 1 and "no resource returned" in err_out

    def test_api_error_raises(self, agents, run_main):
        agents.update_agent.return_value = err(404, "version not found")
        with pytest.raises(RuntimeError, match="HTTP 404"):
            run_main(agent_publish, "--id", "a-1", "--version-id", "nope")
