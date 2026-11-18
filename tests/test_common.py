"""Tests for the shared helpers in common/scripts: formatting, _bootstrap, discovery_helpers."""

import json
import os
from datetime import timezone
import sys
from unittest.mock import MagicMock

import pytest

import _bootstrap
import discovery_helpers as dh
import formatting
from conftest import err, ok


class TestHumanizeDuration:
    @pytest.mark.parametrize("ms, expected", [
        (None, "—"), (float("nan"), "—"), (-1, "—"),
        (0, "0ms"), (850, "850ms"), (999.9, "999ms"),
        (1000, "1.0s"), (1500, "1.5s"), (59999, "60.0s"),
        (60000, "1m00s"), (61000, "1m01s"), (3_725_000, "62m05s"),
    ])
    def test_formats(self, ms, expected):
        assert formatting.humanize_duration(ms) == expected


class TestFormatTimestamp:
    def test_formats_zulu(self):
        assert formatting.format_timestamp("2026-10-05T14:03:09Z") == "Oct. 05, 2026 14:03:09"

    def test_custom_format(self):
        assert formatting.format_timestamp("2026-10-05T14:03:09+00:00", fmt="%Y-%m-%d") == "2026-10-05"

    @pytest.mark.parametrize("ts", [None, ""])
    def test_empty(self, ts):
        assert formatting.format_timestamp(ts) == "(unknown)"
        assert formatting.format_timestamp(ts, empty="—") == "—"

    def test_unparseable_passes_through(self):
        assert formatting.format_timestamp("yesterday") == "yesterday"


class TestFormatModifiedBy:
    @pytest.mark.parametrize("actor, expected", [
        (None, "(unknown)"),
        ({}, "(unknown)"),
        ({"name": "Ana", "uuid": "u1"}, "Ana"),
        ({"api_client_id": "abc123"}, "abc123 (API client)"),
        ({"uuid": "u1"}, "u1"),
        ({"cid": "c1"}, "c1"),
        ({"first_name": "Ana"}, "(unknown)"),
    ])
    def test_renders(self, actor, expected):
        assert formatting.format_modified_by(actor) == expected


class TestBootstrap:
    def test_python_sh_path_is_the_repo_wrapper(self):
        root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        assert os.path.realpath(_bootstrap._python_sh_path()) == os.path.join(root, "scripts", "python.sh")  # pylint: disable=protected-access

    def test_returns_when_falconpy_present(self, monkeypatch):
        execv = MagicMock()
        monkeypatch.setattr(os, "execv", execv)
        _bootstrap.ensure_deps(__file__)
        execv.assert_not_called()

    def test_reexecs_the_real_script_through_wrapper_when_falconpy_missing(self, monkeypatch, tmp_path):
        """A script launched through a symlinked skill dir re-execs by its real path."""
        real = tmp_path / "real" / "script.py"
        real.parent.mkdir()
        real.write_text("")
        (tmp_path / "linked").symlink_to(real.parent)
        execv = MagicMock()
        monkeypatch.setattr(os, "execv", execv)
        monkeypatch.setattr(_bootstrap, "find_spec", lambda name: None)
        monkeypatch.setattr(sys, "argv", ["script.py", "--ids", "a"])
        _bootstrap.ensure_deps(str(tmp_path / "linked" / "script.py"))
        wrapper = _bootstrap._python_sh_path()  # pylint: disable=protected-access
        execv.assert_called_once_with(wrapper, [wrapper, str(real.resolve()), "--ids", "a"])
        assert os.environ[_bootstrap._GUARD_ENV] == "1"  # pylint: disable=protected-access

    def test_no_loop_when_already_reexeced(self, monkeypatch):
        execv = MagicMock()
        monkeypatch.setattr(os, "execv", execv)
        monkeypatch.setattr(_bootstrap, "find_spec", lambda name: None)
        monkeypatch.setenv(_bootstrap._GUARD_ENV, "1")  # pylint: disable=protected-access
        _bootstrap.ensure_deps(__file__)
        execv.assert_not_called()

    def test_no_wrapper_means_natural_import_error(self, monkeypatch):
        execv = MagicMock()
        monkeypatch.setattr(os, "execv", execv)
        monkeypatch.setattr(_bootstrap, "find_spec", lambda name: None)
        monkeypatch.setattr(_bootstrap, "_python_sh_path", lambda: "/nonexistent/python.sh")
        _bootstrap.ensure_deps(__file__)
        execv.assert_not_called()


@pytest.fixture
def spans_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(dh, "get_spans_client", lambda: client)
    monkeypatch.setattr(dh.time, "sleep", lambda s: None)
    return client


class TestHydrateSpans:
    def test_empty_makes_no_call(self, spans_client):
        assert not dh.hydrate_spans([])
        spans_client.entities_spans_v1.assert_not_called()

    def test_batches(self, spans_client):
        spans_client.entities_spans_v1.side_effect = lambda ids: ok({"resources": [{"id": i} for i in ids]})
        spans = dh.hydrate_spans([str(i) for i in range(120)], batch_size=50)
        assert [len(c.kwargs["ids"]) for c in spans_client.entities_spans_v1.call_args_list] == [50, 50, 20]
        assert len(spans) == 120

    def test_retries_transient_then_succeeds(self, spans_client, capsys):
        spans_client.entities_spans_v1.side_effect = [err(503, "busy"), ok({"resources": [{"id": "a"}]})]
        assert dh.hydrate_spans(["a"]) == [{"id": "a"}]
        assert "retry 1/2" in capsys.readouterr().err

    def test_permanent_error_raises_without_retry(self, spans_client):
        spans_client.entities_spans_v1.return_value = err(403, "denied")
        with pytest.raises(RuntimeError, match=r"Failed to hydrate span batch 1 \(1 of 1 spans\): HTTP 403"):
            dh.hydrate_spans(["a"])
        assert spans_client.entities_spans_v1.call_count == 1

    def test_gives_up_after_attempts(self, spans_client):
        spans_client.entities_spans_v1.return_value = err(500, "down")
        with pytest.raises(RuntimeError, match="HTTP 500"):
            dh.hydrate_spans(["a"])
        assert spans_client.entities_spans_v1.call_count == dh.HYDRATE_ATTEMPTS


class TestQuerySpanIds:
    def test_pages_at_the_api_limit(self, spans_client):
        def page(parameters):
            off, lim = int(parameters["offset"]), int(parameters["limit"])
            return ok({"resources": [f"s{i}" for i in range(off, min(off + lim, 1200))],
                       "meta": {"pagination": {"total": 1200}}})
        spans_client.queries_spans_v1.side_effect = page
        ids = dh.query_span_ids("trace_id:'t'", max_results=1200)
        calls = [c.kwargs["parameters"] for c in spans_client.queries_spans_v1.call_args_list]
        assert [(c["offset"], c["limit"]) for c in calls] == [("0", "500"), ("500", "500"), ("1000", "200")]
        assert all(int(c["limit"]) <= dh.SPAN_QUERY_PAGE_SIZE for c in calls)
        assert ids == [f"s{i}" for i in range(1200)]

    def test_stops_at_total(self, spans_client):
        spans_client.queries_spans_v1.return_value = ok({"resources": ["a", "b"], "meta": {"pagination": {"total": 2}}})
        assert dh.query_span_ids("f", max_results=1000) == ["a", "b"]
        assert spans_client.queries_spans_v1.call_count == 1

    def test_short_page_ends_when_total_missing(self, spans_client):
        spans_client.queries_spans_v1.return_value = ok({"resources": ["a"]})
        assert dh.query_span_ids("f", max_results=1000) == ["a"]

    def test_clamped_page_continues_when_total_reported(self, spans_client):
        """The API may return fewer than asked for; with a total, keep paging."""
        spans_client.queries_spans_v1.side_effect = [
            ok({"resources": ["a", "b"], "meta": {"pagination": {"total": 3}}}),
            ok({"resources": ["c"], "meta": {"pagination": {"total": 3}}}),
        ]
        assert dh.query_span_ids("f", max_results=10) == ["a", "b", "c"]

    def test_empty_page_stops(self, spans_client):
        spans_client.queries_spans_v1.return_value = ok({"resources": [], "meta": {"pagination": {"total": 99}}})
        assert not dh.query_span_ids("f", max_results=10)

    def test_errors_propagate(self, spans_client):
        spans_client.queries_spans_v1.return_value = err(400, "bad filter")
        with pytest.raises(RuntimeError, match="HTTP 400"):
            dh.query_span_ids("f", max_results=10)


class TestEnsureSpanTimeFilter:
    @pytest.mark.parametrize("given, expected", [
        ("", "start_time:>='now-24h'"),
        ("   ", "start_time:>='now-24h'"),
        ("trace_id:'t'", "(trace_id:'t')+start_time:>='now-24h'"),
        ("trace_id:'t'+start_time:>='now-7d'", "trace_id:'t'+start_time:>='now-7d'"),
        ("END_TIME:<='now'", "END_TIME:<='now'"),
    ])
    def test_adds_bound_only_when_missing(self, given, expected):
        assert dh._ensure_span_time_filter(given) == expected  # pylint: disable=protected-access


class TestRunEntitySearch:
    def _run(self, monkeypatch, capsys, argv, response, require_time_filter=False):
        query = MagicMock(return_value=ok(response))
        monkeypatch.setattr(sys, "argv", ["x.py", *argv])
        dh.run_entity_search("span", "spans", query, require_time_filter=require_time_filter)
        return query, capsys.readouterr()

    def test_lists_ids(self, monkeypatch, capsys):
        query, out = self._run(monkeypatch, capsys, ["--limit", "2"],
                               {"resources": ["a", "b"], "meta": {"pagination": {"total": 9}}})
        query.assert_called_once_with(parameters={"limit": 2, "offset": 0})
        assert "Found 2 span ID(s) (total: 9):" in out.out and "  b" in out.out

    def test_adds_time_filter_for_spans(self, monkeypatch, capsys):
        query, out = self._run(monkeypatch, capsys, ["--filter", "trace_id:'t'"], {"resources": []}, True)
        assert query.call_args.kwargs["parameters"]["filter"] == "(trace_id:'t')+start_time:>='now-24h'"
        assert "Added default time filter" in out.err
        assert "No spans found." in out.out

    def test_json_prints_the_response_body_instead_of_the_listing(self, monkeypatch, capsys):
        body = {"resources": ["a", "b"], "meta": {"pagination": {"total": 9}}}
        _, out = self._run(monkeypatch, capsys, ["--json"], body)
        assert json.loads(out.out) == body
        assert "Found" not in out.out

    def test_help_never_builds_a_client(self, monkeypatch):
        query = MagicMock()
        monkeypatch.setattr(sys, "argv", ["x.py", "--help"])
        with pytest.raises(SystemExit):
            dh.run_entity_search("span", "spans", query)
        query.assert_not_called()


class TestParseTimestamp:
    @pytest.mark.parametrize("value, expected_utc", [
        ("2026-10-06", "2026-10-06T00:00:00+00:00"),
        ("2026-10-06T05:30:00Z", "2026-10-06T05:30:00+00:00"),
        ("2026-10-05T23:30:00-06:00", "2026-10-06T05:30:00+00:00"),
        ("2026-10-06T05:30:00.123456+00:00", "2026-10-06T05:30:00.123456+00:00"),
    ])
    def test_parses_to_an_aware_instant(self, value, expected_utc):
        parsed = formatting.parse_timestamp(value)
        assert parsed.tzinfo is not None
        assert parsed.astimezone(timezone.utc).isoformat() == expected_utc

    @pytest.mark.parametrize("value", [None, "", "Sept 1", "last week", 20261006])
    def test_unparseable_is_none(self, value):
        assert formatting.parse_timestamp(value) is None


class TestFetchAllIds:
    @staticmethod
    def _query(ids, report_total):
        def query(parameters):
            off, lim = parameters["offset"], parameters["limit"]
            body = {"resources": ids[off:off + lim]}
            if report_total:
                body["meta"] = {"pagination": {"total": len(ids)}}
            return ok(body)
        return MagicMock(side_effect=query)

    @pytest.mark.parametrize("report_total", [True, False])
    @pytest.mark.parametrize("count, calls", [(0, 1), (1, 1), (99, 1), (100, 2), (101, 2), (250, 3)])
    def test_returns_every_id(self, count, calls, report_total):
        ids = [f"id-{i}" for i in range(count)]
        query = self._query(ids, report_total)
        assert dh.fetch_all_ids(query) == ids
        # With a reported total, an exact multiple of the page size needs no extra call.
        expected = calls - 1 if report_total and count == 100 else calls
        assert query.call_count == expected

    def test_extra_parameters_ride_along_on_every_page(self):
        query = self._query([f"f-{i}" for i in range(150)], False)
        dh.fetch_all_ids(query, knowledge_base_id="kb-1")
        assert [c.kwargs["parameters"] for c in query.call_args_list] == [
            {"knowledge_base_id": "kb-1", "limit": 100, "offset": 0},
            {"knowledge_base_id": "kb-1", "limit": 100, "offset": 100},
        ]

    def test_error_propagates(self):
        with pytest.raises(RuntimeError, match="HTTP 403"):
            dh.fetch_all_ids(MagicMock(return_value=err(403, "missing scope")))

    def test_a_server_that_ignores_offset_cannot_loop_forever(self):
        """Same full page every time, no total: stop once a page adds nothing new."""
        query = MagicMock(return_value=ok({"resources": [f"id-{i}" for i in range(100)]}))
        assert len(dh.fetch_all_ids(query)) == 100
        assert query.call_count == 2

    @pytest.mark.parametrize("report_total", [True, False])
    @pytest.mark.parametrize("count", [9999, 10000, 10001])
    def test_stops_at_the_api_window_without_discarding_ids(self, count, report_total, capsys):
        """Agent and KB queries reject offset + limit above 10,000, even for an empty page."""
        ids = [f"id-{i}" for i in range(count)]

        def query(parameters):
            off, lim = parameters["offset"], parameters["limit"]
            assert off + lim <= 10000
            body = {"resources": ids[off:off + lim]}
            if report_total:
                body["meta"] = {"pagination": {"total": count}}
            return ok(body)

        assert dh.fetch_all_ids(query) == ids[:10000]
        warning = capsys.readouterr().err
        if count > 10000 or (count == 10000 and not report_total):
            assert "10,000-result query window" in warning
            assert "may be incomplete" in warning
        else:
            assert not warning

    def test_last_page_is_reduced_to_fit_the_window(self, capsys):
        query = self._query([f"id-{i}" for i in range(10000)], False)
        assert len(dh.fetch_all_ids(query, page_size=300)) == 10000
        assert query.call_args.kwargs["parameters"] == {"limit": 100, "offset": 9900}
        assert all(c.kwargs["parameters"]["offset"] + c.kwargs["parameters"]["limit"] <= 10000
                   for c in query.call_args_list)
        assert "may be incomplete" in capsys.readouterr().err

    def test_short_pages_with_a_total_advance_by_the_number_returned(self):
        ids = [f"id-{i}" for i in range(150)]

        def query(parameters):
            off = parameters["offset"]
            return ok({"resources": ids[off:off + 50], "meta": {"pagination": {"total": len(ids)}}})

        client = MagicMock(side_effect=query)
        assert dh.fetch_all_ids(client) == ids
        assert [c.kwargs["parameters"]["offset"] for c in client.call_args_list] == [0, 50, 100]


class TestModifiedWithin:
    BOUNDS = {"modified_since": formatting.parse_timestamp("2026-10-06"),
              "modified_before": formatting.parse_timestamp("2026-10-07")}

    @pytest.mark.parametrize("updated_at, expected", [
        ("2026-10-06T00:00:00Z", True),           # since is inclusive
        ("2026-10-05T23:59:59Z", False),
        ("2026-10-05T23:30:00-06:00", True),      # 05:30Z on the 6th
        ("2026-10-06T23:59:59Z", True),
        ("2026-10-07T00:00:00Z", False),          # before is exclusive
        ("", False), (None, False), ("garbage", False),
    ])
    def test_both_bounds(self, updated_at, expected):
        assert formatting.modified_within(updated_at, self.BOUNDS) is expected

    def test_no_bounds_accepts_anything(self):
        assert formatting.modified_within(None, {}) is True
        assert formatting.modified_within("garbage", {}) is True

    def test_one_bound(self):
        since = {"modified_since": self.BOUNDS["modified_since"]}
        assert formatting.modified_within("2030-01-01T00:00:00Z", since) is True
        assert formatting.modified_within("2020-01-01T00:00:00Z", since) is False
