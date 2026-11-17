#!/usr/bin/env bash
#
# test-hooks.sh
#
# Unit tests for the agentworks-skills hook scripts:
#   - hooks/agentworks-router.sh         (intent detection, once-per-prompt nudge)
#   - hooks/agentworks-foundry-bridge.sh (cross-plugin advisory with foundry-skills)
#   - hooks/state-dir.sh                 (private per-user marker directory)
#
# Each test feeds hook JSON on stdin and checks stdout, stderr, exit code, and marker
# state. Hermetic: markers live in a temp dir and HOME is a temp dir, so nothing on
# the machine running the tests (installed plugins, real sessions) affects the result.
# Exits 1 if any test fails.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROUTER="$SCRIPT_DIR/hooks/agentworks-router.sh"
BRIDGE="$SCRIPT_DIR/hooks/agentworks-foundry-bridge.sh"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

AGENTWORKS_HOOK_STATE_DIR="$WORK/state"
export AGENTWORKS_HOOK_STATE_DIR
HOME="$WORK/home"
export HOME
mkdir -p "$HOME"
unset CURSOR_PLUGIN_ROOT

if [ -t 1 ]; then
  GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; NC=$'\033[0m'
else
  GREEN=""; RED=""; NC=""
fi

PASS=0
FAIL=0
pass() { echo "  ${GREEN}PASS${NC}: $1"; PASS=$((PASS + 1)); }
fail() { echo "  ${RED}FAIL${NC}: $1"; FAIL=$((FAIL + 1)); }

# run_hook <script> <json>: sets OUT, ERR and RC
run_hook() {
  OUT=$(printf '%s' "$2" | bash "$1" 2>"$WORK/stderr")
  RC=$?
  ERR=$(cat "$WORK/stderr")
}

# context: the advisory text from either host's output shape, or empty
context() { printf '%s' "$OUT" | jq -r '.hookSpecificOutput.additionalContext // .additional_context // empty' 2>/dev/null; }

assert_contains() { if printf '%s' "$2" | grep -qF -- "$3"; then pass "$1"; else fail "$1 (missing: $3)"; fi; }
assert_empty() { if [ -z "$2" ]; then pass "$1"; else fail "$1 (got: $2)"; fi; }
assert_rc0() { if [ "$RC" -eq 0 ]; then pass "$1 exits 0"; else fail "$1 exits 0 (got $RC)"; fi; }
assert_file() { if [ -e "$2" ]; then pass "$1"; else fail "$1 (no $2)"; fi; }
assert_no_file() { if [ ! -e "$2" ]; then pass "$1"; else fail "$1 ($2 exists)"; fi; }

prompt() { printf '{"hook_event_name":"UserPromptSubmit","session_id":"%s","prompt":"%s"}' "$1" "$2"; }
tool() { printf '{"hook_event_name":"PreToolUse","session_id":"%s","tool_name":"%s","tool_input":{}}' "$1" "$2"; }
skill() { printf '{"hook_event_name":"PreToolUse","session_id":"%s","tool_name":"Skill","tool_input":{"skill":"%s"}}' "$1" "$2"; }
marker() { echo "$AGENTWORKS_HOOK_STATE_DIR/agentworks-router-$1"; }

echo ""
echo "Testing agentworks-router.sh"
echo "────────────────────────────"

run_hook "$ROUTER" "$(prompt s1 'Create a Charlotte AI agent that triages detections')"
assert_contains "AgentWorks prompt emits the skill hint" "$(context)" "crowdstrike-charlotte-ai-agentworks:agentworks"
assert_file "AgentWorks prompt writes a marker" "$(marker s1)"
assert_rc0 "AgentWorks prompt"

for p in "ask Charlotte about this detection" "fix the README typo" "invoke the build script"; do
  run_hook "$ROUTER" "$(prompt s2 "$p")"
  assert_empty "no hint for: $p" "$(context)"
  assert_no_file "no marker for: $p" "$(marker s2)"
done

run_hook "$ROUTER" "$(prompt s3 'add an agent to my Foundry app manifest.yml')"
assert_empty "Foundry-app prompt gets no AgentWorks hint" "$(context)"
assert_file "Foundry-app prompt writes the Foundry marker" "$(marker s3).foundry"
assert_no_file "Foundry-app prompt writes no AgentWorks marker" "$(marker s3)"

run_hook "$ROUTER" "$(tool s1 Bash)"
assert_contains "first Bash call after a match nudges" "$(context)" "Load the matching skill"
run_hook "$ROUTER" "$(tool s1 Bash)"
assert_empty "second Bash call does not nudge again" "$(context)"

run_hook "$ROUTER" "$(prompt s4 'publish my agent')"
run_hook "$ROUTER" "$(tool s4 Skill)"
assert_empty "a Skill call is not nudged" "$(context)"
assert_file "a Skill call ends the nudges" "$(marker s4).nudged"
run_hook "$ROUTER" "$(tool s4 Bash)"
assert_empty "no nudge after a skill was loaded" "$(context)"

run_hook "$ROUTER" "$(tool s-none Bash)"
assert_empty "no marker means no nudge" "$(context)"

run_hook "$ROUTER" "$(prompt s1 'fix the README typo')"
assert_no_file "a new prompt clears the old marker" "$(marker s1)"
assert_no_file "a new prompt clears the nudge flag" "$(marker s1).nudged"

run_hook "$ROUTER" "$(prompt '../../escape' 'create an agent')"
if find "$AGENTWORKS_HOOK_STATE_DIR" -maxdepth 1 -name 'agentworks-router-escape' | grep -q .; then
  pass "session id is sanitized into the state dir"
else
  fail "session id is sanitized into the state dir"
fi
assert_no_file "session id cannot escape the state dir" "$WORK/escape"

OUT=$(printf '%s' '{"hook_event_name":"beforeSubmitPrompt","conversation_id":"c1","prompt":"create an agent"}' | CURSOR_PLUGIN_ROOT=1 bash "$ROUTER" 2>/dev/null)
assert_contains "Cursor gets top-level additional_context" "$(printf '%s' "$OUT" | jq -r '.additional_context // empty')" "agentworks"
assert_empty "Cursor gets no nested hookSpecificOutput" "$(printf '%s' "$OUT" | jq -r '.hookSpecificOutput // empty')"

echo ""
echo "Testing agentworks-foundry-bridge.sh"
echo "────────────────────────────────────"

FOUNDRY_PROMPT=$(prompt b1 'add an agent to my Foundry app manifest.yml')

run_hook "$ROUTER" "$FOUNDRY_PROMPT"
run_hook "$BRIDGE" "$(skill b1 crowdstrike-charlotte-ai-agentworks:agents)"
assert_contains "Foundry prompt + AgentWorks skill, Foundry not installed: suggests installing it" "$(context)" "install crowdstrike-falcon-foundry"
assert_rc0 "bridge"

for name in agents invocation discovery agentworks knowledge-bases; do
  run_hook "$BRIDGE" "$(skill b1 "$name")"
  assert_contains "un-namespaced '$name' skill gets the note" "$(context)" "Cross-plugin note"
done

mkdir -p "$HOME/.claude/plugins"
echo '{"plugins":{"crowdstrike-falcon-foundry@marketplace":[{}]}}' > "$HOME/.claude/plugins/installed_plugins.json"
run_hook "$BRIDGE" "$(skill b1 crowdstrike-charlotte-ai-agentworks:agents)"
assert_contains "Foundry installed: routes to ai-agents-development" "$(context)" "crowdstrike-falcon-foundry:ai-agents-development"
rm -rf "$HOME/.claude"

run_hook "$ROUTER" "$(prompt b2 'create an agent that triages detections')"
run_hook "$BRIDGE" "$(skill b2 crowdstrike-falcon-foundry:ai-agents-development)"
assert_contains "AgentWorks prompt + Foundry skill: points back to this plugin" "$(context)" "use the crowdstrike-charlotte-ai-agentworks skills"

run_hook "$BRIDGE" "$(skill b-none crowdstrike-charlotte-ai-agentworks:agents)"
assert_empty "no markers: AgentWorks skill gets no note" "$(context)"
run_hook "$BRIDGE" "$(skill b-none crowdstrike-falcon-foundry:ai-agents-development)"
assert_empty "no markers: Foundry skill gets no note" "$(context)"
run_hook "$BRIDGE" "$(skill b1 setup)"
assert_empty "unrelated skill gets no note" "$(context)"

run_hook "$BRIDGE" '{"hook_event_name":"PreToolUse","session_id":"b1","tool_name":"Skill","tool_input":"agents"}'
assert_rc0 "non-object tool_input"
assert_empty "non-object tool_input writes nothing to stderr" "$ERR"

echo ""
echo "Testing robustness"
echo "──────────────────"

for hook in "$ROUTER" "$BRIDGE"; do
  name=$(basename "$hook")
  run_hook "$hook" "not json"
  assert_rc0 "$name on non-JSON input"
  assert_empty "$name on non-JSON input: stdout" "$OUT"
  assert_empty "$name on non-JSON input: stderr" "$ERR"

  OUT=$(printf '%s' "$(prompt r1 'create an agent')" | PATH=/nonexistent /bin/bash "$hook" 2>&1); RC=$?
  assert_rc0 "$name without jq"
  assert_empty "$name without jq: output" "$OUT"
done

echo ""
echo "Testing state-dir.sh"
echo "────────────────────"

# shellcheck source=hooks/state-dir.sh
source "$SCRIPT_DIR/hooks/state-dir.sh"

D="$WORK/fresh"
OUT=$(AGENTWORKS_HOOK_STATE_DIR="$D" agentworks_state_dir); RC=$?
assert_rc0 "creating a fresh state dir"
assert_contains "returns the created path" "$OUT" "$D"
PERMS=$(stat -c '%a' "$D" 2>/dev/null || stat -f '%Lp' "$D")  # GNU first: Linux `stat -f` means filesystem status and doesn't fail
if [ "$PERMS" = "700" ]; then pass "state dir is mode 700"; else fail "state dir is mode 700 (got $PERMS)"; fi

mkdir -p "$WORK/target"
ln -s "$WORK/target" "$WORK/link"
AGENTWORKS_HOOK_STATE_DIR="$WORK/link" agentworks_state_dir >/dev/null; RC=$?
if [ "$RC" -ne 0 ]; then pass "a symlinked state dir is refused"; else fail "a symlinked state dir is refused"; fi

AGENTWORKS_HOOK_STATE_DIR="$WORK/link" bash "$ROUTER" <<<"$(prompt sl 'create an agent')" >/dev/null 2>&1; RC=$?
assert_rc0 "router with an untrusted state dir"
assert_no_file "no marker is written through a symlinked state dir" "$WORK/target/agentworks-router-sl"
OUT=$(AGENTWORKS_HOOK_STATE_DIR="$WORK/link" bash "$ROUTER" <<<"$(prompt sl 'create an agent')" 2>/dev/null)
assert_contains "the hint is still emitted without a state dir" "$(context)" "agentworks"

STALE="$AGENTWORKS_HOOK_STATE_DIR/agentworks-router-old"
touch "$STALE"
touch -t 202001010000 "$STALE"
run_hook "$ROUTER" "$(prompt fresh 'fix the README typo')"
assert_no_file "markers older than a day are pruned" "$STALE"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
