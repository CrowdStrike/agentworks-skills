"""
Shared test fixtures for agentworks-skills tests.

All tests mock FalconPy at the service-class boundary, so no CrowdStrike API
credentials or network access are needed.
"""

import glob
import os
import sys

import pytest

# Put common/scripts and every skills/*/scripts directory on sys.path so tests import
# the modules by name, the way the scripts import each other at runtime.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_DIRS = [os.path.join(_ROOT, "common", "scripts")] + sorted(
    glob.glob(os.path.join(_ROOT, "skills", "*", "scripts"))
)
for _d in _SCRIPT_DIRS:
    if _d not in sys.path:
        sys.path.insert(0, _d)

import auth  # noqa: E402  # pylint: disable=wrong-import-position  # must follow the sys.path setup


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Keep real credentials, a real TOML profile, and cached clients out of every test."""
    for var in (
        "FALCON_CLIENT_ID",
        "FALCON_CLIENT_SECRET",
        "FALCON_BASE_URL",
        "FALCON_PROFILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "AGENT_WORKS_BOOTSTRAPPED",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(auth, "TOML_CREDENTIALS_PATH_AGENT_WORKS", "/nonexistent/credentials.toml")
    auth.reset_clients()
    yield
    auth.reset_clients()


@pytest.fixture
def fake_credentials(monkeypatch):
    """Set fake credentials for tests that need auth to resolve."""
    monkeypatch.setenv("FALCON_CLIENT_ID", "fake_client_id_1234567890abcdef")
    monkeypatch.setenv("FALCON_CLIENT_SECRET", "fake_secret_abcdef1234567890")
    monkeypatch.setenv("FALCON_BASE_URL", "https://api.crowdstrike.com")


def ok(body, status=200):
    """A FalconPy service-class response with the given body."""
    return {"status_code": status, "headers": {}, "body": body}


def err(status, message):
    """A FalconPy error response carrying one API error."""
    return {"status_code": status, "headers": {}, "body": {"errors": [{"code": status, "message": message}], "resources": []}}


@pytest.fixture
def run_main(monkeypatch, capsys):
    """Run a script module's main() with argv, returning (exit_code, stdout, stderr)."""

    def _run(module, *argv):
        monkeypatch.setattr(sys, "argv", [module.__file__, *argv])
        code = 0
        try:
            module.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run
