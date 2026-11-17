#!/usr/bin/env bash
#
# test-bootstrap-symlink.sh — regression guard for symlink-based skill installs.
#
# Assistants other than Claude Code (Codex, Copilot CLI, Cursor, Antigravity) install
# each skill as a symlink, ~/.agents/skills/<skill>, and run its scripts by path. A
# script launched through that symlink must still find two things in the real repo:
#   1. scripts/python.sh, the managed-venv wrapper _bootstrap.py re-execs through, and
#   2. no os.path.abspath(__file__) anywhere, which would reintroduce the bug below.
#
# The scripts anchor on os.path.realpath(__file__). Where a path keeps its ".." as text
# (the scripts' sys.path insert), the OS resolves it through the real directory either
# way. But where Python collapses ".." itself (os.path.normpath in _bootstrap's
# _python_sh_path), os.path.abspath(__file__) makes it climb the symlink's parents
# instead of the repo's, so the venv wrapper is "not found", the re-exec never runs, and
# the script dies with ModuleNotFoundError: falconpy.
#
# Hermetic: path resolution only. No venv build, no network, no credentials.
set -uo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/bootstrap-symlink.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
PYTHON="${PYTHON:-python3}"

PASS=0
FAIL=0
check() { # check <desc> <rc>
  if [ "$2" -eq 0 ]; then echo "  PASS: $1"; PASS=$((PASS + 1));
  else echo "  FAIL: $1"; FAIL=$((FAIL + 1)); fi
}

# A ~/.agents/skills layout, nested deep enough that a lexical ".." collapse lands on a
# path that doesn't exist.
mkdir -p "$WORK/home/.agents/skills"
ln -s "$REPO/skills/agents" "$WORK/home/.agents/skills/agents"
SYMLINK_SCRIPT="$WORK/home/.agents/skills/agents/scripts/agent_get.py"

echo "== shipped scripts anchor with realpath, not abspath =="
if grep -n "abspath(__file__)" "$REPO"/common/scripts/*.py "$REPO"/skills/*/scripts/*.py >/dev/null 2>&1; then
  check "no abspath(__file__) in shipped scripts" 1
  grep -n "abspath(__file__)" "$REPO"/common/scripts/*.py "$REPO"/skills/*/scripts/*.py | sed "s|$REPO/|    |"
else
  check "no abspath(__file__) in shipped scripts" 0
fi

echo "== _bootstrap resolves python.sh through the symlinked import path =="
"$PYTHON" - "$SYMLINK_SCRIPT" "$REPO" <<'PY'
import os, sys
script, repo = sys.argv[1], os.path.realpath(sys.argv[2])
# The scripts' own sys.path insert, with the literal "..", reached through the symlink.
sys.path.insert(0, os.path.join(os.path.dirname(script), "..", "..", "..", "common", "scripts"))
import _bootstrap
wrapper = os.path.realpath(_bootstrap._python_sh_path())
print(f"    wrapper resolves to: {wrapper}")
sys.exit(0 if wrapper == os.path.join(repo, "scripts", "python.sh") and os.path.exists(wrapper) else 1)
PY
check "_python_sh_path() resolves to the repo's scripts/python.sh" $?

echo ""
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
