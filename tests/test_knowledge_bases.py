"""Tests for the knowledge-base scripts."""

import json
from unittest.mock import MagicMock

import pytest

import discovery_helpers
import kb_audit
import kb_file_download
import kb_file_upload
import kb_files_list
import kb_get
import kb_list
import kb_search
import kb_upsert
from conftest import err, ok


def kb(kb_id, name, updated_at="2026-10-05T14:03:09Z", **extra):
    """A knowledge base entity as EntitiesKnowledgeBasesV1 returns it."""
    return {"id": kb_id, "name": name, "description": f"{name} docs", "updated_at": updated_at,
            "created_at": "2026-09-01T00:00:00Z", "files_count": 2,
            "updated_by": {"name": "Ana Silva", "uuid": "u-1"}, **extra}


@pytest.fixture
def kb_client(monkeypatch):
    client = MagicMock()
    for module in (kb_list, kb_get, kb_search, kb_upsert):
        monkeypatch.setattr(module, "get_kb_client", lambda: client)
    return client


@pytest.fixture
def files_client(monkeypatch):
    client = MagicMock()
    for module in (kb_files_list, kb_file_upload, kb_file_download):
        monkeypatch.setattr(module, "get_kb_files_client", lambda: client)
    return client


def serve(client, entities, report_total=True):
    """Make the client page through `entities` the way the query + entities endpoints do."""
    def query(parameters):
        off, lim = parameters["offset"], parameters["limit"]
        body = {"resources": [e["id"] for e in entities[off:off + lim]]}
        if report_total:
            body["meta"] = {"pagination": {"total": len(entities)}}
        return ok(body)
    by_id = {e["id"]: e for e in entities}
    client.QueriesKnowledgeBasesV1.side_effect = query
    client.EntitiesKnowledgeBasesV1.side_effect = lambda ids: ok({"resources": [by_id[i] for i in ids]})


def rows(out):
    return json.loads(out)["rows"]


class TestKbList:
    def test_missing_scope_is_an_error_not_an_empty_list(self, kb_client, run_main):
        """Regression: a 403 was swallowed and reported as "No knowledge bases found", so the
        model concluded the KB didn't exist."""
        kb_client.QueriesKnowledgeBasesV1.return_value = err(403, "missing scope")
        with pytest.raises(RuntimeError, match="HTTP 403"):
            run_main(kb_list)

    def test_pages_past_100(self, kb_client, run_main):
        serve(kb_client, [kb(f"kb-{i}", f"KB {i:03d}") for i in range(250)])
        _, out, _ = run_main(kb_list, "--json", "--limit", "500")
        assert json.loads(out)["total_matched"] == 250
        assert [c.kwargs["parameters"]["offset"] for c in kb_client.QueriesKnowledgeBasesV1.call_args_list] == [0, 100, 200]

    def test_pages_past_100_when_the_api_reports_no_total(self, kb_client, run_main):
        """Without meta.pagination.total, the listing must keep paging until a short page."""
        serve(kb_client, [kb(f"kb-{i}", f"KB {i:03d}") for i in range(250)], report_total=False)
        _, out, _ = run_main(kb_list, "--json", "--limit", "500")
        assert json.loads(out)["total_matched"] == 250

    def test_query_window_keeps_rows_and_warns_without_breaking_json(self, kb_client, run_main, monkeypatch):
        monkeypatch.setattr(discovery_helpers, "QUERY_RESULT_WINDOW", 100)
        serve(kb_client, [kb(f"kb-{i}", f"KB {i:03d}") for i in range(101)], report_total=False)
        code, out, err_out = run_main(kb_list, "--json", "--limit", "500")
        assert code == 0 and len(rows(out)) == 100
        assert kb_client.QueriesKnowledgeBasesV1.call_count == 1
        assert "may be incomplete" in err_out

    def test_sorts_newest_first_by_default(self, kb_client, run_main):
        serve(kb_client, [kb("a", "Alpha", "2026-10-01T00:00:00Z"), kb("b", "Beta", "2026-10-09T00:00:00Z")])
        _, out, _ = run_main(kb_list, "--json")
        assert [r["id"] for r in rows(out)] == ["b", "a"]

    def test_sort_by_name_ascending(self, kb_client, run_main):
        serve(kb_client, [kb("b", "Beta"), kb("a", "Alpha")])
        _, out, _ = run_main(kb_list, "--json", "--sort", "name", "--order", "asc")
        assert [r["name"] for r in rows(out)] == ["Alpha", "Beta"]

    def test_filters(self, kb_client, run_main):
        serve(kb_client, [kb("a", "Playbooks", files_count=5), kb("b", "Runbooks", files_count=0),
                          kb("c", "Notes", description="incident playbook drafts")])
        assert [r["id"] for r in rows(run_main(kb_list, "--json", "--name-contains", "PLAY")[1])] == ["a"]
        assert sorted(r["id"] for r in rows(run_main(kb_list, "--json", "--contains", "playbook")[1])) == ["a", "c"]
        assert [r["id"] for r in rows(run_main(kb_list, "--json", "--min-files", "3")[1])] == ["a"]
        assert len(rows(run_main(kb_list, "--json", "--modified-by", "ana")[1])) == 3
        assert not rows(run_main(kb_list, "--json", "--modified-by", "bob")[1])

    def test_null_name_and_description_do_not_crash(self, kb_client, run_main):
        """The API can return a key with a null value; .get(key, "") then yields None."""
        serve(kb_client, [kb("a", None, description=None), kb("b", "Beta")])
        code, out, _ = run_main(kb_list, "--json", "--contains", "beta")
        assert code == 0
        assert [r["id"] for r in rows(out)] == ["b"]

    def test_null_updated_at_does_not_crash_date_filters_or_sorting(self, kb_client, run_main):
        serve(kb_client, [kb("a", "Alpha", None), kb("b", "Beta", "2026-10-09T00:00:00Z")])
        code, out, _ = run_main(kb_list, "--json", "--modified-since", "2026-10-01")
        assert code == 0
        assert [r["id"] for r in rows(out)] == ["b"]
        assert run_main(kb_list, "--json")[0] == 0

    def test_modified_since_compares_instants_not_strings(self, kb_client, run_main):
        """2026-10-05T23:30:00-06:00 is 2026-10-06T05:30Z, so it is on/after Oct 6 UTC even
        though its text sorts before "2026-10-06"."""
        serve(kb_client, [kb("late", "Late", "2026-10-05T23:30:00-06:00"), kb("early", "Early", "2026-10-05T10:00:00Z")])
        _, out, _ = run_main(kb_list, "--json", "--modified-since", "2026-10-06")
        assert [r["id"] for r in rows(out)] == ["late"]

    def test_modified_before_is_exclusive(self, kb_client, run_main):
        serve(kb_client, [kb("a", "A", "2026-10-06T00:00:00Z"), kb("b", "B", "2026-10-05T23:59:59Z")])
        _, out, _ = run_main(kb_list, "--json", "--modified-before", "2026-10-06")
        assert [r["id"] for r in rows(out)] == ["b"]

    def test_unparseable_date_is_rejected_before_any_api_call(self, kb_client, run_main):
        serve(kb_client, [kb("a", "Alpha")])
        code, _, err_out = run_main(kb_list, "--modified-since", "Sept 1")
        assert code != 0
        assert "Sept 1" in err_out
        kb_client.QueriesKnowledgeBasesV1.assert_not_called()

    def test_pagination_trailer(self, kb_client, run_main):
        serve(kb_client, [kb(f"kb-{i}", f"KB {i:02d}") for i in range(47)])
        _, out, _ = run_main(kb_list, "--sort", "name", "--order", "asc")
        assert "Showing 1-20 of 47 matching KB(s)." in out
        assert "MORE_RESULTS_AVAILABLE next_offset=20 remaining=27" in out
        _, out, _ = run_main(kb_list, "--offset", "40")
        assert "Showing 41-47 of 47" in out and "MORE_RESULTS_AVAILABLE" not in out

    def test_json_paging_fields(self, kb_client, run_main):
        serve(kb_client, [kb(f"kb-{i}", f"KB {i}") for i in range(5)])
        page = json.loads(run_main(kb_list, "--json", "--limit", "2", "--offset", "2")[1])
        assert (page["total_matched"], page["has_more"], page["next_offset"], len(page["rows"])) == (5, True, 4, 2)
        last = json.loads(run_main(kb_list, "--json", "--limit", "2", "--offset", "4")[1])
        assert (last["has_more"], last["next_offset"]) == (False, None)

    def test_long_descriptions_are_truncated_in_the_table(self, kb_client, run_main):
        serve(kb_client, [kb("a", "Alpha", description="d" * 100)])
        _, out, _ = run_main(kb_list)
        assert "d" * 80 + "..." in out and "d" * 81 not in out

    def test_no_matches(self, kb_client, run_main):
        serve(kb_client, [kb("a", "Alpha")])
        assert "No knowledge bases found matching filters." in run_main(kb_list, "--name-contains", "zzz")[1]


class TestKbGetAndSearch:
    def test_get_prints_entities(self, kb_client, run_main):
        kb_client.EntitiesKnowledgeBasesV1.return_value = ok({"resources": [kb("kb-1", "Playbooks")]})
        code, out, _ = run_main(kb_get, "--ids", "kb-1", "kb-2")
        kb_client.EntitiesKnowledgeBasesV1.assert_called_once_with(ids=["kb-1", "kb-2"])
        assert code == 0 and "Playbooks" in out and "kb-1" in out

    def test_get_error_raises_before_printing_json(self, kb_client, run_main, capsys):
        """Regression: --json printed the error body and exited 0."""
        kb_client.EntitiesKnowledgeBasesV1.return_value = err(404, "not found")
        with pytest.raises(RuntimeError, match="HTTP 404"):
            run_main(kb_get, "--ids", "kb-x", "--json")
        assert capsys.readouterr().out == ""

    def test_search_passes_the_filter(self, kb_client, run_main):
        kb_client.QueriesKnowledgeBasesV1.return_value = ok({"resources": ["kb-1"], "meta": {"pagination": {"total": 1}}})
        code, out, _ = run_main(kb_search, "--filter", "name:'Playbooks'", "--limit", "5")
        params = kb_client.QueriesKnowledgeBasesV1.call_args.kwargs["parameters"]
        assert params["filter"] == "name:'Playbooks'" and params["limit"] == 5
        assert code == 0 and "kb-1" in out


class TestKbUpsert:
    def test_create(self, kb_client, run_main):
        kb_client.EntitiesKnowledgeBasesCreateV1.return_value = ok({"resources": [kb("kb-9", "New")]})
        code, out, _ = run_main(kb_upsert, "--name", "New", "--description", "d")
        kb_client.EntitiesKnowledgeBasesCreateV1.assert_called_once_with(body={"name": "New", "description": "d"})
        kb_client.EntitiesKnowledgeBasesUpdateV1.assert_not_called()
        assert code == 0 and "kb-9" in out

    def test_create_needs_a_name(self, kb_client, run_main):
        code, _, err_out = run_main(kb_upsert, "--description", "d")
        assert code == 1 and "--name is required" in err_out
        kb_client.EntitiesKnowledgeBasesCreateV1.assert_not_called()

    def test_update_carries_over_the_name_it_was_not_given(self, kb_client, run_main):
        """The API replaces the KB and requires a name, so a description-only update must
        resend the existing name instead of blanking it."""
        kb_client.EntitiesKnowledgeBasesV1.return_value = ok({"resources": [kb("kb-1", "Playbooks")]})
        kb_client.EntitiesKnowledgeBasesUpdateV1.return_value = ok({"resources": [kb("kb-1", "Playbooks")]})
        run_main(kb_upsert, "--id", "kb-1", "--description", "new text")
        kb_client.EntitiesKnowledgeBasesUpdateV1.assert_called_once_with(
            body={"id": "kb-1", "name": "Playbooks", "description": "new text"})

    def test_update_carries_over_the_description(self, kb_client, run_main):
        kb_client.EntitiesKnowledgeBasesV1.return_value = ok({"resources": [kb("kb-1", "Playbooks")]})
        kb_client.EntitiesKnowledgeBasesUpdateV1.return_value = ok({"resources": [kb("kb-1", "Renamed")]})
        run_main(kb_upsert, "--id", "kb-1", "--name", "Renamed")
        assert kb_client.EntitiesKnowledgeBasesUpdateV1.call_args.kwargs["body"] == {
            "id": "kb-1", "name": "Renamed", "description": "Playbooks docs"}

    def test_update_with_both_fields_skips_the_read(self, kb_client, run_main):
        kb_client.EntitiesKnowledgeBasesUpdateV1.return_value = ok({"resources": [kb("kb-1", "N")]})
        run_main(kb_upsert, "--id", "kb-1", "--name", "N", "--description", "")
        kb_client.EntitiesKnowledgeBasesV1.assert_not_called()
        assert kb_client.EntitiesKnowledgeBasesUpdateV1.call_args.kwargs["body"]["description"] == ""

    def test_update_of_unknown_kb_exits_without_writing(self, kb_client, run_main):
        kb_client.EntitiesKnowledgeBasesV1.return_value = ok({"resources": []})
        code, _, err_out = run_main(kb_upsert, "--id", "kb-typo", "--description", "d")
        assert code == 1 and "knowledge base kb-typo not found" in err_out
        kb_client.EntitiesKnowledgeBasesUpdateV1.assert_not_called()


class TestKbFiles:
    def test_upload_sends_flat_kwargs_with_the_open_file(self, files_client, run_main, tmp_path):
        """FalconPy reads flat kwargs only; a data=/files= wrapper silently drops the file."""
        path = tmp_path / "playbook.md"
        path.write_text("# Playbook\n")
        seen = {}

        def create(**kwargs):
            seen.update(kwargs, content=kwargs["file"].read())
            return ok({"resources": [{"id": "f-1", "knowledge_base_id": "kb-1", "name": "playbook.md"}]})

        files_client.EntitiesKnowledgeBaseFilesCreateV1.side_effect = create
        code, out, _ = run_main(kb_file_upload, "--kb-id", "kb-1", "--file", str(path), "--description", "ops")
        assert code == 0 and "File ID: f-1" in out
        assert (seen["knowledge_base_id"], seen["file_name"], seen["file_description"]) == ("kb-1", "playbook.md", "ops")
        assert seen["content"] == b"# Playbook\n"

    def test_upload_of_a_missing_file_makes_no_call(self, files_client, run_main, tmp_path):
        code, _, err_out = run_main(kb_file_upload, "--kb-id", "kb-1", "--file", str(tmp_path / "nope.txt"))
        assert code == 1 and "File not found" in err_out
        files_client.EntitiesKnowledgeBaseFilesCreateV1.assert_not_called()

    def test_download_writes_the_bytes(self, files_client, run_main, tmp_path):
        files_client.entities_knowledge_base_files_download_v1.return_value = b"hello\nworld"
        out_path = tmp_path / "out.txt"
        code, out, _ = run_main(kb_file_download, "--kb-id", "kb-1", "--file-id", "f-1", "--output", str(out_path))
        files_client.entities_knowledge_base_files_download_v1.assert_called_once_with(
            parameters={"knowledge_base_id": "kb-1", "id": "f-1"})
        assert code == 0 and out_path.read_bytes() == b"hello\nworld"
        assert "11 bytes" in out

    def test_download_of_a_parsed_body_fails_without_writing(self, files_client, run_main, tmp_path):
        """If the endpoint ever sends a Content-Type, FalconPy parses the body into a dict; the
        script must refuse to write that as the file."""
        files_client.entities_knowledge_base_files_download_v1.return_value = ok({"a": 1})
        out_path = tmp_path / "out.json"
        code, _, err_out = run_main(kb_file_download, "--kb-id", "kb-1", "--file-id", "f-1", "--output", str(out_path))
        assert code == 1 and "Unexpected download response" in err_out
        assert not out_path.exists()

    def test_download_of_an_empty_file(self, files_client, run_main, tmp_path):
        files_client.entities_knowledge_base_files_download_v1.return_value = b""
        out_path = tmp_path / "out.txt"
        code, _, err_out = run_main(kb_file_download, "--kb-id", "kb-1", "--file-id", "f-1", "--output", str(out_path))
        assert code == 1 and "No file content returned" in err_out
        assert not out_path.exists()

    def test_files_list_needs_the_kb_id_on_both_calls(self, files_client, run_main):
        files_client.QueriesKnowledgeBaseFilesV1.return_value = ok({"resources": ["f-1"], "meta": {"pagination": {"total": 1}}})
        files_client.EntitiesKnowledgeBaseFilesV1.return_value = ok({"resources": [
            {"id": "f-1", "knowledge_base_id": "kb-1", "name": "a.pdf", "size": 2048, "content_type": "application/pdf",
             "updated_at": "2026-10-05T14:03:09Z", "updated_by": {"name": "Ana"}}]})
        code, out, _ = run_main(kb_files_list, "--kb-id", "kb-1")
        assert files_client.QueriesKnowledgeBaseFilesV1.call_args.kwargs["parameters"]["knowledge_base_id"] == "kb-1"
        files_client.EntitiesKnowledgeBaseFilesV1.assert_called_once_with(knowledge_base_id="kb-1", ids=["f-1"])
        assert code == 0 and "a.pdf" in out

    def test_files_list_pages_without_a_reported_total(self, files_client, run_main):
        ids = [f"f-{i}" for i in range(150)]

        def query(parameters):
            off, lim = parameters["offset"], parameters["limit"]
            return ok({"resources": ids[off:off + lim]})

        files_client.QueriesKnowledgeBaseFilesV1.side_effect = query
        files_client.EntitiesKnowledgeBaseFilesV1.side_effect = lambda knowledge_base_id, ids: ok({"resources": [
            {"id": i, "name": None, "size": 1, "updated_at": None, "updated_by": None} for i in ids]})
        code, out, _ = run_main(kb_files_list, "--kb-id", "kb-1", "--json")
        assert code == 0
        assert json.loads(out)["total_matched"] == 150

    def test_files_query_window_keeps_rows_and_warns_without_breaking_json(self, files_client, run_main, monkeypatch):
        monkeypatch.setattr(discovery_helpers, "QUERY_RESULT_WINDOW", 100)
        files_client.QueriesKnowledgeBaseFilesV1.return_value = ok({"resources": [f"f-{i}" for i in range(100)]})
        files_client.EntitiesKnowledgeBaseFilesV1.side_effect = lambda knowledge_base_id, ids: ok({
            "resources": [{"id": i, "name": i} for i in ids],
        })
        code, out, err_out = run_main(kb_files_list, "--kb-id", "kb-1", "--json")
        assert code == 0 and len(rows(out)) == 100
        files_client.QueriesKnowledgeBaseFilesV1.assert_called_once_with(
            parameters={"knowledge_base_id": "kb-1", "limit": 100, "offset": 0})
        assert "may be incomplete" in err_out

    def test_files_list_name_filter_skips_a_file_with_a_null_name(self, files_client, run_main):
        files_client.QueriesKnowledgeBaseFilesV1.return_value = ok({"resources": ["f-1", "f-2"]})
        files_client.EntitiesKnowledgeBaseFilesV1.return_value = ok({"resources": [
            {"id": "f-1", "name": None}, {"id": "f-2", "name": "Runbook.PDF"}]})
        code, out, _ = run_main(kb_files_list, "--kb-id", "kb-1", "--json", "--name-contains", "runbook")
        assert code == 0
        assert [r["id"] for r in json.loads(out)["rows"]] == ["f-2"]

    def test_audit_queries_by_kb(self, monkeypatch, run_main):
        client = MagicMock()
        client.CombinedKnowledgeBaseAuditEventsV1.return_value = ok({"resources": [
            {"id": "e-1", "operation_type": "FILE_UPLOAD", "user_uuid": "u-1",
             "timestamp": "2026-10-05T14:03:09Z", "details": {"file_name": "a.pdf"}}],
            "meta": {"pagination": {"total": 7}}})
        monkeypatch.setattr(kb_audit, "get_kb_audit_client", lambda: client)
        code, out, _ = run_main(kb_audit, "--kb-id", "kb-1", "--filter", 'operation_type:"FILE_UPLOAD"', "--limit", "1")
        assert client.CombinedKnowledgeBaseAuditEventsV1.call_args.kwargs["parameters"] == {
            "knowledge_base_id": "kb-1", "limit": 1, "offset": 0, "filter": 'operation_type:"FILE_UPLOAD"'}
        assert code == 0
        assert "Found 1 audit event(s) (total: 7)" in out
        assert "Operation: FILE_UPLOAD" in out and "User: u-1" in out
