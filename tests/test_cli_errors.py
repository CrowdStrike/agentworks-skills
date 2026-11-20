"""Every script that calls the API reports an HTTP error as one clean line and exit 1.

These run the real scripts as separate processes, the way an assistant does, against a
stand-in falconpy whose every API call returns 403. A script without a RuntimeError
handler dumps a Python traceback instead, which an assistant has to dig through to find
the reason.
"""

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_FALCONPY = '''
import importlib.machinery, sys, types

class _Service:
    def __init__(self, **kwargs):
        pass

    def token_expired(self):
        return False

    def __getattr__(self, name):
        return lambda **kwargs: {"status_code": 403, "headers": {},
                                 "body": {"errors": [{"code": 403, "message": "access denied"}]}}

falconpy = types.ModuleType("falconpy")
falconpy.__spec__ = importlib.machinery.ModuleSpec("falconpy", None)
falconpy.version = lambda: "1.6.7"
for name in ("OAuth2", "Agents", "AgentVersions", "AgentInvocation", "Stream", "Models", "Tools",
             "AgentTemplates", "Spans", "KnowledgeBases", "KnowledgeBaseFiles", "KnowledgeBaseAuditEvents"):
    setattr(falconpy, name, _Service)
sys.modules["falconpy"] = falconpy
'''

# (script, arguments that get past argparse to the first API call)
SCRIPTS = [
    ("agents/scripts/agent_get.py", ["--ids", "agent-1"]),
    ("agents/scripts/agent_list.py", []),
    ("agents/scripts/agent_publish.py", ["--id", "agent-1", "--version-id", "ver-1"]),
    ("agents/scripts/agent_search.py", []),
    ("agents/scripts/agent_upsert.py", ["--id", "agent-1", "--name", "x"]),
    ("agents/scripts/analyze_agent.py", ["--agent-id", "agent-1"]),
    ("agents/scripts/compare_versions.py", ["--agent-id", "agent-1", "--version-id-a", "v1",
                                            "--version-id-b", "v2", "--message", "hi"]),
    ("discovery/scripts/models_search.py", []),
    ("discovery/scripts/spans_get.py", ["--ids", "span-1"]),
    ("discovery/scripts/spans_search.py", []),
    ("discovery/scripts/templates_search.py", []),
    ("discovery/scripts/tools_get.py", []),
    ("discovery/scripts/tools_search.py", []),
    ("discovery/scripts/versions_search.py", []),
    ("invocation/scripts/cancel_invocation.py", ["--id", "inv-1"]),
    ("invocation/scripts/get_messages.py", ["--id", "inv-1"]),
    ("invocation/scripts/inspect_invocation.py", ["--invocation-id", "inv-1"]),
    ("invocation/scripts/invoke_agent.py", ["--id", "agent-1", "--message", "hi"]),
    ("invocation/scripts/invoke_version.py", ["--agent-id", "agent-1", "--version-id", "ver-1", "--message", "hi"]),
    ("invocation/scripts/list_invocations.py", ["--agent-id", "agent-1"]),
    ("invocation/scripts/stream_invocation.py", ["--id", "inv-1"]),
    ("knowledge-bases/scripts/kb_audit.py", ["--kb-id", "kb-1"]),
    ("knowledge-bases/scripts/kb_file_download.py", ["--kb-id", "kb-1", "--file-id", "f-1", "--output", "/dev/null"]),
    ("knowledge-bases/scripts/kb_file_upload.py", ["--kb-id", "kb-1", "--file", "{upload}"]),
    ("knowledge-bases/scripts/kb_files_list.py", ["--kb-id", "kb-1"]),
    ("knowledge-bases/scripts/kb_get.py", ["--ids", "kb-1"]),
    ("knowledge-bases/scripts/kb_list.py", []),
    ("knowledge-bases/scripts/kb_search.py", []),
    ("knowledge-bases/scripts/kb_upsert.py", ["--name", "kb"]),
]


@pytest.fixture(scope="module")
def fake_falconpy(tmp_path_factory):
    shim = tmp_path_factory.mktemp("shim")
    (shim / "sitecustomize.py").write_text(FAKE_FALCONPY)
    return shim


def run(script, args, shim, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FALCON_")}
    env.update(PYTHONPATH=str(shim), FALCON_CLIENT_ID="test-id", FALCON_CLIENT_SECRET="test-secret",
               AGENT_WORKS_BOOTSTRAPPED="1", HOME=str(tmp_path))
    return subprocess.run([sys.executable, os.path.join(REPO, "skills", script), *args],
                          capture_output=True, text=True, env=env, check=False, timeout=60)


@pytest.mark.parametrize("script, args", SCRIPTS, ids=[s for s, _ in SCRIPTS])
def test_http_error_is_one_clean_line(script, args, fake_falconpy, tmp_path):
    upload = tmp_path / "upload.txt"
    upload.write_text("content\n")
    result = run(script, [a.format(upload=upload) for a in args], fake_falconpy, tmp_path)
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert "HTTP 403" in result.stderr and "access denied" in result.stderr


def test_every_api_script_is_covered():
    """A new script that calls the API must be added to SCRIPTS above."""
    calling = set()
    for skill in os.listdir(os.path.join(REPO, "skills")):
        scripts = os.path.join(REPO, "skills", skill, "scripts")
        if not os.path.isdir(scripts):
            continue
        for name in os.listdir(scripts):
            if name.endswith(".py"):
                with open(os.path.join(scripts, name), encoding="utf-8") as f:
                    source = f.read()
                if "call_native" in source or "hydrate_spans" in source or "run_entity_search" in source:
                    calling.add(f"{skill}/scripts/{name}")
    # The two span-diagnostic tools report a failed probe and keep going, by design.
    missing = calling - {s for s, _ in SCRIPTS} - {"discovery/scripts/diagnose_spans.py",
                                                   "discovery/scripts/probe_span_filters.py"}
    assert not missing, f"add these to SCRIPTS: {sorted(missing)}"


SELF_TEST_FALCONPY = '''
import importlib.machinery, os, sys, types

class _Auth:
    def __init__(self, **kwargs):
        self.token_fail_reason = os.environ.get("FAKE_FAIL_REASON") or None

class _Service:
    def __init__(self, auth_object=None, **kwargs):
        if os.environ.get("FAKE_RAISE"):
            raise OSError("certificate verify failed")
        self.auth_object = auth_object

    def token_expired(self):
        return bool(os.environ.get("FAKE_FAIL_REASON"))

falconpy = types.ModuleType("falconpy")
falconpy.__spec__ = importlib.machinery.ModuleSpec("falconpy", None)
falconpy.version = lambda: os.environ.get("FAKE_VERSION", "1.6.7")
falconpy.OAuth2 = _Auth
for name in ("Agents", "AgentInvocation", "KnowledgeBases", "KnowledgeBaseFiles", "KnowledgeBaseAuditEvents"):
    setattr(falconpy, name, _Service)
sys.modules["falconpy"] = falconpy
'''


class TestAuthSelfTest:
    """`python common/scripts/auth.py` is what the setup skill runs to check credentials."""

    @pytest.fixture
    def self_test(self, tmp_path):
        (tmp_path / "sitecustomize.py").write_text(SELF_TEST_FALCONPY)

        def run(**env):
            base = {k: v for k, v in os.environ.items() if not k.startswith(("FALCON_", "FAKE_"))}
            base.update(PYTHONPATH=str(tmp_path), HOME=str(tmp_path), **env)
            return subprocess.run([sys.executable, os.path.join(REPO, "common", "scripts", "auth.py")],
                                  capture_output=True, text=True, env=base, check=False, timeout=60)
        return run

    CREDS = {"FALCON_CLIENT_ID": "0123456789abcdef0123456789abcdef", "FALCON_CLIENT_SECRET": "s3cr3t-value-that-must-not-print"}

    def test_success_checks_every_client_and_hides_the_secret(self, self_test):
        result = self_test(**self.CREDS)
        assert result.returncode == 0, result.stderr
        assert result.stdout.count("✓ Authentication successful") == 5
        assert "Client ID : 01234567...cdef" in result.stdout
        assert "s3cr3t" not in result.stdout + result.stderr
        assert "(32 chars)" in result.stdout

    def test_failed_login_shows_the_api_reason_and_exits_1(self, self_test):
        result = self_test(**self.CREDS, FAKE_FAIL_REASON="access denied, invalid client credentials")
        assert result.returncode == 1
        assert "Authentication FAILED: could not obtain token (KnowledgeBases): access denied" in result.stderr
        assert "s3cr3t" not in result.stdout + result.stderr

    def test_connection_failure_is_reported_without_a_traceback(self, self_test):
        result = self_test(**self.CREDS, FAKE_RAISE="1")
        assert result.returncode == 1
        assert "Authentication FAILED: certificate verify failed" in result.stderr
        assert "Traceback" not in result.stderr

    def test_old_falconpy_is_reported(self, self_test):
        result = self_test(**self.CREDS, FAKE_VERSION="1.6.5")
        assert result.returncode == 1
        assert "FalconPy >= 1.6.6 required (found 1.6.5)" in result.stderr

    def test_no_credentials_points_at_setup(self, self_test):
        result = self_test()
        assert result.returncode == 1
        assert "/crowdstrike-charlotte-ai-agentworks:setup" in result.stderr
