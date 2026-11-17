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
    result = run(script, args, fake_falconpy, tmp_path)
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
    missing = calling - {s for s, _ in SCRIPTS} - {"discovery/scripts/diagnose_spans.py",
                                                   "discovery/scripts/probe_span_filters.py",
                                                   "knowledge-bases/scripts/kb_file_upload.py"}
    assert not missing, f"add these to SCRIPTS: {sorted(missing)}"
