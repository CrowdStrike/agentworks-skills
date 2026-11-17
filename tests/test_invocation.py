"""Tests for the invocation scripts: cancel, get_messages polling, invoke, and stream."""

import json
from unittest.mock import MagicMock

import pytest

import cancel_invocation
import get_messages
import invoke_agent
import invoke_version
import stream_invocation
from conftest import err, ok


class FakeClock:
    """A monotonic clock that only moves when the script sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def invocation(status, conversation=None, inv_id="inv-1"):
    """An invocation entity as get_agent_invocation_v3 returns it."""
    return {"id": inv_id, "agent_id": "agent-1", "status": status,
            "conversation": conversation or [], "ai_trace_id": "trace-1"}


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    for module in (get_messages, cancel_invocation):
        monkeypatch.setattr(module.time, "monotonic", fake.monotonic)
        monkeypatch.setattr(module.time, "sleep", fake.sleep)
    return fake


@pytest.fixture
def inv_client(monkeypatch):
    client = MagicMock()
    for module in (get_messages, cancel_invocation, invoke_agent, invoke_version):
        monkeypatch.setattr(module, "get_agent_invocation_client", lambda: client)
    return client


def statuses(*seq):
    """Responses for get_agent_invocation_v3, one per poll."""
    return [ok({"resources": [invocation(s)]}) if isinstance(s, str) else s for s in seq]


class TestGetMessagesWait:
    def test_returns_once_terminal(self, inv_client, clock, run_main):
        inv_client.get_agent_invocation_v3.side_effect = statuses("processing", "running", "completed")
        code, out, _ = run_main(get_messages, "--id", "inv-1", "--wait", "--json")
        assert code == 0
        assert json.loads(out)["resources"][0]["status"] == "completed"
        assert inv_client.get_agent_invocation_v3.call_count == 3
        assert clock.sleeps == [5, 10]

    def test_cancelling_is_still_in_flight(self, inv_client, clock, run_main):
        """Regression: an allow-list of in-flight statuses missed 'cancelling' and returned early."""
        inv_client.get_agent_invocation_v3.side_effect = statuses("processing", "cancelling", "cancelled")
        _, out, _ = run_main(get_messages, "--id", "inv-1", "--wait", "--json")
        assert json.loads(out)["resources"][0]["status"] == "cancelled"
        assert inv_client.get_agent_invocation_v3.call_count == 3

    def test_unrecognized_status_keeps_polling_until_timeout(self, inv_client, clock, run_main):
        inv_client.get_agent_invocation_v3.return_value = ok({"resources": [invocation("brand_new_state")]})
        code, _, err_out = run_main(get_messages, "--id", "inv-1", "--wait", "--timeout", "30")
        assert code == 0
        assert "still 'brand_new_state' after 30s" in err_out
        assert clock.now - 1000.0 == pytest.approx(30)

    def test_backoff_caps_at_20s(self, inv_client, clock, run_main):
        inv_client.get_agent_invocation_v3.return_value = ok({"resources": [invocation("processing")]})
        run_main(get_messages, "--id", "inv-1", "--wait", "--timeout", "100")
        assert clock.sleeps[:5] == [5, 10, 20, 20, 20]

    def test_nonexistent_id_fails_fast(self, inv_client, clock, run_main):
        """Regression: the API answers a well-formed unknown ID with 200 and empty resources;
        treating that as 'unknown, keep waiting' polled a typo for the full timeout."""
        inv_client.get_agent_invocation_v3.return_value = ok({"resources": []})
        with pytest.raises(RuntimeError, match="No invocation found for ID inv-typo"):
            run_main(get_messages, "--id", "inv-typo", "--wait")
        assert inv_client.get_agent_invocation_v3.call_count == 1
        assert not clock.sleeps

    def test_permanent_http_error_is_not_retried(self, inv_client, clock, run_main):
        inv_client.get_agent_invocation_v3.return_value = err(403, "missing scope")
        with pytest.raises(RuntimeError, match="HTTP 403"):
            run_main(get_messages, "--id", "inv-1", "--wait")
        assert inv_client.get_agent_invocation_v3.call_count == 1

    def test_transient_errors_are_retried(self, inv_client, clock, run_main):
        inv_client.get_agent_invocation_v3.side_effect = [
            err(503, "busy"), err(500, "read timed out"), ok({"resources": [invocation("completed")]}),
        ]
        code, out, err_out = run_main(get_messages, "--id", "inv-1", "--wait", "--json")
        assert code == 0
        assert json.loads(out)["resources"][0]["status"] == "completed"
        assert err_out.count("transient error") == 2

    def test_network_exception_is_retried(self, inv_client, clock, run_main):
        inv_client.get_agent_invocation_v3.side_effect = [ConnectionError("reset"),
                                                          ok({"resources": [invocation("failed")]})]
        _, out, _ = run_main(get_messages, "--id", "inv-1", "--wait", "--json")
        assert json.loads(out)["resources"][0]["status"] == "failed"


class TestGetMessagesOutput:
    def test_prints_conversation(self, inv_client, run_main):
        convo = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": {"answer": 42}}]
        inv_client.get_agent_invocation_v3.return_value = ok({"resources": [invocation("completed", convo)]})
        code, out, _ = run_main(get_messages, "--id", "inv-1")
        assert code == 0
        assert "Conversation (2 messages)" in out
        assert "[ASSISTANT]" in out and '{"answer": 42}' in out

    def test_truncates_long_messages(self, inv_client, run_main):
        convo = [{"role": "assistant", "content": "x" * 300}]
        inv_client.get_agent_invocation_v3.return_value = ok({"resources": [invocation("completed", convo)]})
        _, out, _ = run_main(get_messages, "--id", "inv-1")
        assert "x" * 200 + "..." in out and "x" * 201 not in out

    def test_running_invocation_has_no_messages_yet(self, inv_client, run_main):
        inv_client.get_agent_invocation_v3.return_value = ok({"resources": [invocation("processing")]})
        _, out, _ = run_main(get_messages, "--id", "inv-1")
        assert "No messages yet" in out

    def test_without_wait_an_unknown_id_says_so(self, inv_client, run_main):
        inv_client.get_agent_invocation_v3.return_value = ok({"resources": []})
        _, out, _ = run_main(get_messages, "--id", "inv-typo")
        assert "No invocation found" in out


class TestCancel:
    def test_sends_cancelled_status(self, inv_client, clock, run_main):
        """Regression: the cancel body was {"action": ...}, which cancels nothing."""
        inv_client.update_agent_invocation.return_value = ok({}, status=200)
        inv_client.get_agent_invocation_v3.side_effect = statuses("cancelling", "cancelled")
        code, out, _ = run_main(cancel_invocation, "--id", "inv-1", "--json")
        inv_client.update_agent_invocation.assert_called_once_with(id="inv-1", status="cancelled")
        assert code == 0
        assert json.loads(out) == {"id": "inv-1", "cancel_requested": True, "status": "cancelled"}

    def test_reports_still_cancelling_after_the_wait(self, inv_client, clock, run_main):
        inv_client.update_agent_invocation.return_value = ok({})
        inv_client.get_agent_invocation_v3.return_value = ok({"resources": [invocation("cancelling")]})
        code, out, err_out = run_main(cancel_invocation, "--id", "inv-1")
        assert code == 0
        assert "Status: cancelling" in out
        assert "still 'cancelling' after 10s" in err_out

    def test_cancel_error_raises_and_skips_the_status_read(self, inv_client, clock, run_main):
        inv_client.update_agent_invocation.return_value = err(404, "invocation not found")
        with pytest.raises(RuntimeError, match="HTTP 404"):
            run_main(cancel_invocation, "--id", "inv-typo")
        inv_client.get_agent_invocation_v3.assert_not_called()


@pytest.mark.parametrize("module, args, method", [
    (invoke_agent, ["--id", "agent-1"], "invoke_published_agent_external_v1"),
    (invoke_version, ["--agent-id", "agent-1", "--version-id", "ver-1"], "invoke_agent_version_external_v1"),
])
class TestInvoke:
    def test_prints_invocation_and_trace_ids(self, inv_client, run_main, module, args, method):
        getattr(inv_client, method).return_value = ok({"resources": [invocation("queued", inv_id="inv-9")]})
        code, out, _ = run_main(module, *args, "--message", "hello")
        assert code == 0
        assert "Invocation ID: inv-9" in out
        assert "AI TraceID: trace-1" in out

    def test_sends_the_message_as_a_message_object(self, inv_client, run_main, module, args, method):
        getattr(inv_client, method).return_value = ok({"resources": [invocation("queued")]})
        run_main(module, *args, "--message", "hello")
        body = getattr(inv_client, method).call_args.kwargs["body"]
        assert {"role": "user", "content": "hello"} in body["messages"]

    def test_deadline_below_90s_is_rejected_before_any_call(self, inv_client, run_main, module, args, method):
        code, _, err_out = run_main(module, *args, "--message", "hi", "--deadline-seconds", "89")
        assert code == 1
        assert "at least 90 seconds" in err_out
        getattr(inv_client, method).assert_not_called()

    def test_max_cost_above_10000_is_rejected(self, inv_client, run_main, module, args, method):
        code, _, err_out = run_main(module, *args, "--message", "hi", "--max-cost", "10001")
        assert code == 1
        assert "must not exceed 10000" in err_out
        getattr(inv_client, method).assert_not_called()

    def test_http_error_raises(self, inv_client, run_main, module, args, method):
        getattr(inv_client, method).return_value = err(400, "agent is not published")
        with pytest.raises(RuntimeError, match="agent is not published"):
            run_main(module, *args, "--message", "hi")


class TestStream:
    SSE = (b'event: message\ndata: {"role": "assistant", "content": "partial"}\n\n'
           b'data: plain text line\n\n'
           b'data: [DONE]\n\n')

    def test_parse_sse_decodes_json_text_and_drops_done(self):
        assert stream_invocation.parse_sse(self.SSE) == [
            {"event": "message", "data": {"role": "assistant", "content": "partial"}},
            {"event": "message", "data": "plain text line"},
        ]

    def test_parse_sse_joins_multiline_data_and_crlf(self):
        raw = "event: delta\r\ndata: a\r\ndata: b\r\n\r\n"
        assert stream_invocation.parse_sse(raw) == [{"event": "delta", "data": "a\nb"}]

    def test_parse_sse_ignores_blocks_without_data(self):
        assert not stream_invocation.parse_sse(": keepalive\n\nevent: ping\n\n")

    def test_main_prints_events(self, monkeypatch, run_main):
        client = MagicMock()
        client.stream_invocation_response_v1.return_value = self.SSE
        monkeypatch.setattr(stream_invocation, "get_stream_client", lambda: client)
        code, out, _ = run_main(stream_invocation, "--id", "inv-1")
        client.stream_invocation_response_v1.assert_called_once_with(parameters={"id": "inv-1"})
        assert code == 0
        assert "Events (2)" in out and "partial" in out

    def test_main_with_no_events(self, monkeypatch, run_main):
        client = MagicMock()
        client.stream_invocation_response_v1.return_value = b""
        monkeypatch.setattr(stream_invocation, "get_stream_client", lambda: client)
        _, out, _ = run_main(stream_invocation, "--id", "inv-1")
        assert "No streamed messages" in out
