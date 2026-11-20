"""Tests for agent_upsert.py: the create body, the update overlay, and version branching."""

import copy
import json
from unittest.mock import MagicMock

import pytest

import agent_upsert
from conftest import err, ok

# An active version as get_studio_agents returns it, including fields agent_upsert doesn't
# model (compaction_config, tool overrides) and the read-only identity/audit fields.
ACTIVE = {
    "id": "ver-1", "agent_id": "agent-1", "version": 3, "version_number": 3, "status": "published",
    "created_at": "2026-10-01T00:00:00Z", "created_by": {"uuid": "u1"},
    "updated_at": "2026-10-02T00:00:00Z", "updated_by": {"uuid": "u1"},
    "cid": "cid-1", "is_published": True,
    "name": "Hunter", "description": "Finds things", "system_prompt": "old prompt",
    "model": "bedrock.claude-5-opus",
    "tools": [{"type": "action", "name": "search", "id": "mcp/x/search",
               "parameter_overrides": {"limit": "5"}, "name_override": "Find", "description_override": "Search"}],
    "knowledge_base_ids": ["kb-1"],
    "compaction_config": {"strategy": "summarize", "threshold": 0.8},
    "model_config": {"temperature": 0.2, "max_tokens": 8000, "top_p": 1.0, "top_k": 50,
                     "enable_reasoning": True, "reasoning_effort": "high", "frequency_penalty": 0.0},
    "input_format": {"format": "unstructured", "json_schema": "", "usage_instructions": ""},
    "output_format": {"format": "json_with_schema", "json_schema": '{"type": "object"}', "usage_instructions": ""},
}

CREATED = ok({"resources": [{"agent": {"id": "agent-1"}, "agent_version": {"id": "ver-2", "name": "Hunter",
                                                                             "model": "bedrock.claude-5-opus"}}]})


@pytest.fixture
def clients(monkeypatch):
    agents, versions = MagicMock(), MagicMock()
    agents.get_studio_agents.return_value = ok({"resources": [{"id": "agent-1", "active_version": copy.deepcopy(ACTIVE)}]})
    agents.create_or_update_agent.return_value = CREATED
    versions.query_agent_versions_v1.return_value = ok({"resources": ["ver-1"]})
    monkeypatch.setattr(agent_upsert, "get_agents_client", lambda: agents)
    monkeypatch.setattr(agent_upsert, "get_agent_versions_client", lambda: versions)
    return agents, versions


def sent_definition(agents):
    return agents.create_or_update_agent.call_args.kwargs["body"]["version_definition"]


class TestUpdate:
    def test_carries_over_fields_the_script_does_not_model(self, clients, run_main):
        """Regression: rebuilding the body from a fixed field list dropped compaction_config."""
        agents, _ = clients
        code, _, _ = run_main(agent_upsert, "--id", "agent-1", "--system-prompt", "new prompt")
        assert code == 0
        definition = sent_definition(agents)
        assert definition["system_prompt"] == "new prompt"
        assert definition["compaction_config"] == {"strategy": "summarize", "threshold": 0.8}
        assert definition["model_config"]["reasoning_effort"] == "high"
        assert definition["output_format"]["json_schema"] == '{"type": "object"}'
        assert agents.create_or_update_agent.call_args.kwargs["body"]["id"] == "agent-1"

    def test_strips_read_only_fields(self, clients, run_main):
        agents, _ = clients
        run_main(agent_upsert, "--id", "agent-1", "--name", "Hunter 2")
        definition = sent_definition(agents)
        for field in ("id", "agent_id", "version", "version_number", "status",
                      "created_at", "created_by", "updated_at", "updated_by"):
            assert field not in definition, field
        assert definition["name"] == "Hunter 2"

    def test_keeps_overrides_on_tools_that_stay_attached(self, clients, run_main):
        """Regression: --tools replaced every tool with a fresh object, wiping its overrides."""
        agents, _ = clients
        run_main(agent_upsert, "--id", "agent-1", "--tools", "mcp/x/search", "mcp/y/lookup")
        tools = sent_definition(agents)["tools"]
        assert tools[0] == ACTIVE["tools"][0]
        assert tools[1] == {"type": "action", "name": "lookup", "id": "mcp/y/lookup",
                            "parameter_overrides": {}, "name_override": "", "description_override": ""}

    def test_empty_tools_and_kbs_clear_them(self, clients, run_main):
        agents, _ = clients
        run_main(agent_upsert, "--id", "agent-1", "--tools", "--knowledge-base-ids")
        definition = sent_definition(agents)
        assert definition["tools"] == [] and definition["knowledge_base_ids"] == []

    def test_omitted_options_leave_values_alone(self, clients, run_main):
        agents, _ = clients
        run_main(agent_upsert, "--id", "agent-1", "--temperature", "0.9")
        definition = sent_definition(agents)
        assert definition["model_config"]["temperature"] == 0.9
        assert definition["model_config"]["max_tokens"] == 8000
        assert definition["description"] == "Finds things"
        assert definition["tools"] == ACTIVE["tools"]

    def test_empty_string_clears_description(self, clients, run_main):
        agents, _ = clients
        run_main(agent_upsert, "--id", "agent-1", "--description", "")
        assert sent_definition(agents)["description"] == ""

    def test_null_sub_configs_get_defaults(self, clients, run_main):
        agents, _ = clients
        version = dict(ACTIVE, model_config=None, input_format=None)
        agents.get_studio_agents.return_value = ok({"resources": [{"id": "agent-1", "active_version": version}]})
        code, _, _ = run_main(agent_upsert, "--id", "agent-1", "--max-tokens", "2000")
        assert code == 0
        definition = sent_definition(agents)
        assert definition["model_config"]["max_tokens"] == 2000
        assert definition["model_config"]["temperature"] == 0.7
        assert definition["input_format"]["format"] == "unstructured"

    def test_unknown_agent_exits_without_writing(self, clients, run_main):
        """Regression: a missing agent fell through and sent defaults like "Unnamed Agent"."""
        agents, _ = clients
        agents.get_studio_agents.return_value = ok({"resources": []})
        code, _, err_out = run_main(agent_upsert, "--id", "agent-typo", "--system-prompt", "x")
        assert code == 1
        assert "agent agent-typo not found" in err_out
        agents.create_or_update_agent.assert_not_called()

    def test_agent_without_active_version_needs_a_base_version(self, clients, run_main):
        agents, _ = clients
        agents.get_studio_agents.return_value = ok({"resources": [{"id": "agent-1", "active_version": None}]})
        code, _, err_out = run_main(agent_upsert, "--id", "agent-1", "--system-prompt", "x")
        assert code == 1
        assert "--base-version-id" in err_out
        agents.create_or_update_agent.assert_not_called()


class TestBranching:
    def test_warns_when_a_newer_draft_exists(self, clients, run_main):
        agents, versions = clients
        versions.query_agent_versions_v1.return_value = ok({"resources": ["ver-9"]})
        versions.get_agent_versions_v1.return_value = ok({"resources": [{"id": "ver-9", "created_at": "2026-11-01"}]})
        _, _, err_out = run_main(agent_upsert, "--id", "agent-1", "--system-prompt", "x")
        params = versions.query_agent_versions_v1.call_args.kwargs["parameters"]
        assert params == {"filter": "agent_id:'agent-1'", "sort": "created_at|desc", "limit": 1}
        assert "a newer version exists (id=ver-9" in err_out
        assert "--base-version-id ver-9" in err_out
        agents.create_or_update_agent.assert_called_once()

    def test_no_warning_when_active_is_newest(self, clients, run_main):
        _, _, err_out = run_main(agent_upsert, "--id", "agent-1", "--system-prompt", "x")
        assert "newer version" not in err_out

    def test_base_version_id_branches_from_that_version(self, clients, run_main):
        agents, versions = clients
        draft = dict(ACTIVE, id="ver-9", system_prompt="draft prompt", name="Draft")
        versions.get_agent_versions_v1.return_value = ok({"resources": [draft]})
        run_main(agent_upsert, "--id", "agent-1", "--base-version-id", "ver-9", "--temperature", "0.1")
        versions.get_agent_versions_v1.assert_called_once_with(ids=["ver-9"])
        versions.query_agent_versions_v1.assert_not_called()
        definition = sent_definition(agents)
        assert definition["system_prompt"] == "draft prompt" and definition["name"] == "Draft"

    def test_unknown_base_version_exits(self, clients, run_main):
        agents, versions = clients
        versions.get_agent_versions_v1.return_value = ok({"resources": []})
        code, _, err_out = run_main(agent_upsert, "--id", "agent-1", "--base-version-id", "nope")
        assert code == 1
        assert "--base-version-id nope not found" in err_out
        agents.create_or_update_agent.assert_not_called()


class TestCreate:
    def test_requires_name_and_model(self, clients, run_main):
        agents, _ = clients
        assert run_main(agent_upsert, "--model", "m")[0] == 1
        code, _, err_out = run_main(agent_upsert, "--name", "New")
        assert code == 1 and "--model is required" in err_out
        agents.create_or_update_agent.assert_not_called()

    def test_create_body_has_defaults_and_no_id(self, clients, run_main):
        agents, _ = clients
        code, out, _ = run_main(agent_upsert, "--name", "New", "--model", "openai.gpt-6-sol")
        body = agents.create_or_update_agent.call_args.kwargs["body"]
        assert code == 0 and "id" not in body
        definition = body["version_definition"]
        assert definition["model"] == "openai.gpt-6-sol"
        assert definition["model_config"]["max_tokens"] == 4096
        assert definition["targeting_config"]["apigw"] is True
        assert "python agent_publish.py --id agent-1 --version-id ver-2" in out
        agents.get_studio_agents.assert_not_called()

    def test_warns_about_max_tokens_for_tool_agents(self, clients, run_main):
        _, _, err_out = run_main(agent_upsert, "--name", "New", "--model", "m", "--tools", "mcp/x/a")
        assert "max_tokens defaults to 4096" in err_out

    def test_no_max_tokens_warning_when_set(self, clients, run_main):
        _, _, err_out = run_main(agent_upsert, "--name", "New", "--model", "m", "--tools", "mcp/x/a",
                                 "--max-tokens", "16000")
        assert "max_tokens defaults" not in err_out


class TestFormats:
    def test_input_schema_is_normalized_json(self, clients, run_main):
        agents, _ = clients
        run_main(agent_upsert, "--id", "agent-1", "--input-format", "json_with_schema",
                 "--input-schema", '{ "type" :  "object" }')
        assert sent_definition(agents)["input_format"] == {
            "format": "json_with_schema", "json_schema": '{"type": "object"}', "usage_instructions": ""}

    def test_switching_to_unstructured_clears_the_input_schema(self, clients, run_main):
        agents, _ = clients
        version = dict(ACTIVE, input_format={"format": "json_with_schema", "json_schema": "{}", "usage_instructions": ""})
        agents.get_studio_agents.return_value = ok({"resources": [{"id": "agent-1", "active_version": version}]})
        run_main(agent_upsert, "--id", "agent-1", "--input-format", "unstructured")
        assert sent_definition(agents)["input_format"]["json_schema"] == ""

    def test_schema_alone_keeps_the_existing_input_format(self, clients, run_main):
        agents, _ = clients
        run_main(agent_upsert, "--id", "agent-1", "--input-schema", '{"type": "string"}')
        assert sent_definition(agents)["input_format"]["format"] == "unstructured"
        assert sent_definition(agents)["input_format"]["json_schema"] == '{"type": "string"}'

    def test_non_schema_output_format_clears_the_schema(self, clients, run_main):
        agents, _ = clients
        run_main(agent_upsert, "--id", "agent-1", "--output-format", "markdown")
        assert sent_definition(agents)["output_format"] == {"format": "markdown", "json_schema": "", "usage_instructions": ""}

    def test_invalid_schema_json_fails_before_writing(self, clients, run_main):
        agents, _ = clients
        with pytest.raises(json.JSONDecodeError):
            run_main(agent_upsert, "--id", "agent-1", "--output-schema", "{not json")
        agents.create_or_update_agent.assert_not_called()


class TestErrors:
    def test_api_error_raises(self, clients, run_main):
        agents, _ = clients
        agents.create_or_update_agent.return_value = err(400, "model not found")
        with pytest.raises(RuntimeError, match="HTTP 400: .*model not found"):
            run_main(agent_upsert, "--id", "agent-1", "--model", "bad")

    def test_no_resource_returned_exits_1(self, clients, run_main):
        agents, _ = clients
        agents.create_or_update_agent.return_value = ok({"resources": []})
        code, _, err_out = run_main(agent_upsert, "--id", "agent-1", "--name", "x")
        assert code == 1 and "no resource returned" in err_out
