"""Tests for auth.py: credential resolution, the FalconPy version guard, clients, and call_native."""

import types
from unittest.mock import MagicMock, patch

import pytest
import requests

import auth
from conftest import err, ok


def _toml(tmp_path, monkeypatch, text):
    path = tmp_path / "credentials.toml"
    path.write_text(text)
    monkeypatch.setattr(auth, "TOML_CREDENTIALS_PATH_AGENT_WORKS", str(path))


class TestGetCredentials:
    def test_env_vars_win(self, fake_credentials):
        assert auth.get_credentials() == (
            "fake_client_id_1234567890abcdef",
            "fake_secret_abcdef1234567890",
            "https://api.crowdstrike.com",
        )

    def test_default_base_url(self, monkeypatch):
        monkeypatch.setenv("FALCON_CLIENT_ID", "id")
        monkeypatch.setenv("FALCON_CLIENT_SECRET", "secret")
        assert auth.get_credentials()[2] == auth.DEFAULT_BASE_URL

    def test_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("FALCON_CLIENT_ID", "id")
        monkeypatch.setenv("FALCON_CLIENT_SECRET", "secret")
        monkeypatch.setenv("FALCON_BASE_URL", "https://api.us-2.crowdstrike.com/")
        assert auth.get_credentials()[2] == "https://api.us-2.crowdstrike.com"

    def test_env_needs_both_id_and_secret(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FALCON_CLIENT_ID", "id-only")
        _toml(tmp_path, monkeypatch, 'default = "us-1"\n[us-1]\nclient_id = "t"\nclient_secret = "s"\n')
        assert auth.get_credentials()[:2] == ("t", "s")

    def test_exits_with_setup_hint_when_unconfigured(self, capsys):
        with pytest.raises(SystemExit) as exc:
            auth.get_credentials()
        assert exc.value.code == 1
        assert "/crowdstrike-charlotte-ai-agentworks:setup" in capsys.readouterr().err

    def test_toml_default_profile(self, tmp_path, monkeypatch):
        _toml(tmp_path, monkeypatch, 'default = "us-1"\n[us-1]\nclient_id = "a"\nclient_secret = "b"\n'
              'base_url = "https://api.us-2.crowdstrike.com/"\n')
        assert auth.get_credentials() == ("a", "b", "https://api.us-2.crowdstrike.com")

    def test_falcon_profile_overrides_default(self, tmp_path, monkeypatch):
        _toml(tmp_path, monkeypatch, 'default = "us-1"\n[us-1]\nclient_id = "a"\nclient_secret = "b"\n'
              '[eu-1]\nclient_id = "e"\nclient_secret = "f"\nbase_url = "https://api.eu-1.crowdstrike.com"\n')
        monkeypatch.setenv("FALCON_PROFILE", "eu-1")
        assert auth.get_credentials() == ("e", "f", "https://api.eu-1.crowdstrike.com")

    def test_default_table_is_the_profile(self, tmp_path, monkeypatch):
        """A [default] table instead of default = "<name>" must not raise TypeError."""
        _toml(tmp_path, monkeypatch, '[default]\nclient_id = "a"\nclient_secret = "b"\n')
        assert auth.get_credentials()[:2] == ("a", "b")

    @pytest.mark.parametrize("text", [
        'default = "us-1"\n[us-1]\nclient_id = "a"\n',          # missing secret
        'default = "missing"\n[us-1]\nclient_id = "a"\nclient_secret = "b"\n',  # no such profile
        'default = 3\n',                                         # default isn't a name or table
        'default = "us-1"\nus-1 = "not a table"\n',              # profile isn't a table
        "not = [valid toml",                                     # unparseable
    ])
    def test_incomplete_or_bad_toml_falls_through_to_exit(self, tmp_path, monkeypatch, text):
        _toml(tmp_path, monkeypatch, text)
        with pytest.raises(SystemExit):
            auth.get_credentials()

    def test_unreadable_toml_is_skipped(self, tmp_path, monkeypatch):
        path = tmp_path / "credentials.toml"
        path.mkdir()  # a directory where the file should be: isfile() is False
        monkeypatch.setattr(auth, "TOML_CREDENTIALS_PATH_AGENT_WORKS", str(path))
        assert auth._load_toml(str(path)) is None  # pylint: disable=protected-access

    def test_toml_read_error_is_none(self, tmp_path):
        path = tmp_path / "credentials.toml"
        path.write_text("x = 1\n")
        with patch("builtins.open", side_effect=OSError("denied")):
            assert auth._load_toml(str(path)) is None  # pylint: disable=protected-access


class TestFalconPyVersion:
    @pytest.mark.parametrize("raw, expected", [
        ("1.6.6", (1, 6, 6)),
        ("1.6.7.post1", (1, 6, 7)),
        ("2", (2, 0, 0)),
        ("garbage", (0, 0, 0)),
    ])
    def test_parses_version_function(self, raw, expected):
        module = types.SimpleNamespace(version=lambda: raw)
        assert auth._falconpy_version(module) == expected  # pylint: disable=protected-access

    def test_falls_back_to_dunder_version(self):
        module = types.SimpleNamespace(version=lambda: "", __version__="1.7.0")
        assert auth._falconpy_version(module) == (1, 7, 0)  # pylint: disable=protected-access

    def test_version_function_that_raises(self):
        def boom():
            raise ValueError("no")
        module = types.SimpleNamespace(version=boom, __version__="1.6.6")
        assert auth._falconpy_version(module) == (1, 6, 6)  # pylint: disable=protected-access

    def test_too_old_raises_with_floor(self):
        with pytest.raises(RuntimeError, match=r"FalconPy >= 1\.6\.6 required \(found 1\.6\.5\)"):
            auth._check_falconpy_version(types.SimpleNamespace(version=lambda: "1.6.5"))  # pylint: disable=protected-access

    def test_floor_passes(self):
        auth._check_falconpy_version(types.SimpleNamespace(version=lambda: "1.6.6"))  # pylint: disable=protected-access


class TestClients:
    def test_one_auth_object_with_timeout_shared_by_all_clients(self, fake_credentials):
        with patch("falconpy.OAuth2") as oauth, patch("falconpy.KnowledgeBases") as kb, \
                patch("falconpy.Agents") as agents:
            first = auth.get_kb_client()
            again = auth.get_kb_client()
            auth.get_agents_client()
        assert first is again
        oauth.assert_called_once_with(
            client_id="fake_client_id_1234567890abcdef",
            client_secret="fake_secret_abcdef1234567890",
            base_url="https://api.crowdstrike.com",
            timeout=auth.REQUEST_TIMEOUT_SECONDS,
        )
        kb.assert_called_once_with(auth_object=oauth.return_value)
        agents.assert_called_once_with(auth_object=oauth.return_value)

    @pytest.mark.parametrize("factory, class_name", [
        ("get_kb_client", "KnowledgeBases"),
        ("get_kb_files_client", "KnowledgeBaseFiles"),
        ("get_kb_audit_client", "KnowledgeBaseAuditEvents"),
        ("get_agents_client", "Agents"),
        ("get_agent_invocation_client", "AgentInvocation"),
        ("get_stream_client", "Stream"),
        ("get_models_client", "Models"),
        ("get_tools_client", "Tools"),
        ("get_agent_templates_client", "AgentTemplates"),
        ("get_agent_versions_client", "AgentVersions"),
        ("get_spans_client", "Spans"),
    ])
    def test_each_factory_builds_its_service_class(self, fake_credentials, factory, class_name):
        with patch("falconpy.OAuth2"), patch(f"falconpy.{class_name}") as cls:
            assert getattr(auth, factory)() is cls.return_value

    def test_reset_clients_forces_a_new_auth_object(self, fake_credentials):
        with patch("falconpy.OAuth2") as oauth, patch("falconpy.Models"):
            auth.get_models_client()
            auth.reset_clients()
            auth.get_models_client()
        assert oauth.call_count == 2

    def test_old_falconpy_blocks_client_creation(self, fake_credentials):
        with patch("falconpy.version", return_value="1.5.0"), patch("falconpy.OAuth2") as oauth:
            with pytest.raises(RuntimeError, match="FalconPy >= 1.6.6"):
                auth.get_models_client()
        oauth.assert_not_called()


class TestCallNative:
    def test_returns_body(self):
        assert auth.call_native(MagicMock(return_value=ok({"resources": [1]})), x=1) == {"resources": [1]}

    def test_forwards_kwargs(self):
        method = MagicMock(return_value=ok({}))
        auth.call_native(method, parameters={"limit": 5}, ids=["a"])
        method.assert_called_once_with(parameters={"limit": 5}, ids=["a"])

    def test_http_error_raises_with_api_message(self):
        with pytest.raises(RuntimeError, match=r"HTTP 403: .*denied"):
            auth.call_native(MagicMock(return_value=err(403, "denied")))

    def test_error_without_errors_array_keeps_raw_body(self):
        method = MagicMock(return_value={"status_code": 502, "body": "Bad Gateway"})
        with pytest.raises(RuntimeError, match="HTTP 502: Bad Gateway"):
            auth.call_native(method)

    def test_204_with_null_body_returns_the_response(self):
        response = {"status_code": 204, "headers": {}, "body": None}
        assert auth.call_native(MagicMock(return_value=response)) is response

    def test_bytes_pass_through(self):
        assert auth.call_native(MagicMock(return_value=b"file")) == b"file"

    def test_timeout_result_raises_500(self):
        """FalconPy turns a requests timeout into a status-500 result, not an exception."""
        method = MagicMock(return_value=err(500, "read timed out"))
        with pytest.raises(RuntimeError, match="HTTP 500: .*read timed out") as exc:
            auth.call_native(method)
        assert auth.is_transient_error(exc.value)


class TestIsTransientError:
    @pytest.mark.parametrize("exc", [
        RuntimeError("HTTP 429: slow down"),
        RuntimeError("HTTP 500: boom"),
        RuntimeError("HTTP 503: unavailable"),
        requests.ConnectionError("reset"),
        requests.Timeout("slow"),
        ConnectionError("reset"),
        TimeoutError("slow"),
    ])
    def test_transient(self, exc):
        assert auth.is_transient_error(exc)

    @pytest.mark.parametrize("exc", [
        RuntimeError("HTTP 400: bad"),
        RuntimeError("HTTP 403: denied"),
        RuntimeError("HTTP 404: missing"),
        RuntimeError("No invocation found for ID x"),
        KeyError("resources"),
        TypeError("nope"),
    ])
    def test_permanent(self, exc):
        assert not auth.is_transient_error(exc)

    def test_works_without_requests_installed(self, monkeypatch):
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "requests":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        assert auth.is_transient_error(ConnectionError("reset"))
        assert not auth.is_transient_error(ValueError("x"))


class TestFailReasonSuffix:
    def test_reads_reason_from_auth_object(self):
        client = types.SimpleNamespace(auth_object=types.SimpleNamespace(token_fail_reason="invalid_client"))
        assert auth._fail_reason_suffix(client) == ": invalid_client"  # pylint: disable=protected-access

    def test_empty_without_a_reason(self):
        assert auth._fail_reason_suffix(types.SimpleNamespace()) == ""  # pylint: disable=protected-access
