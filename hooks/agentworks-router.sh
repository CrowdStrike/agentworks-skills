#!/usr/bin/env bash
# agentworks-router.sh - Advisory skill routing for Charlotte AI AgentWorks.
#
# UserPromptSubmit: classify ONLY the user's prompt. On a match, drop a session-scoped marker
#                   and add a routing hint to the model's context.
# PreToolUse:       (matcher: Bash|Skill) if a marker is present, nudge once for that prompt.
#                   Loading any skill ends the reminders.
#
# The marker is scoped to the session and reset on every prompt, so a detection never carries
# into a later prompt. It is NOT removed in PreToolUse: agentworks-foundry-bridge.sh reads it too
# and Claude Code runs matching hooks in parallel, so a sidecar file records that the reminder
# was already given.
#
# Hints are emitted as JSON on stdout (hookSpecificOutput.additionalContext, or additional_context
# on Cursor; see host-output.sh) -- stderr from a hook that exits 0 is never shown to the model.
# Always exits 0 (advisory only, never blocks).

set +e  # Advisory hook -- never fail

# Needs jq to read the hook payload and emit JSON; without it, stay silent.
command -v jq >/dev/null 2>&1 || exit 0

HOOK_DIR="$(dirname "${BASH_SOURCE[0]}")"
# shellcheck disable=SC1091
source "$HOOK_DIR/host-output.sh"
# shellcheck disable=SC1091
source "$HOOK_DIR/state-dir.sh"

INPUT=$(cat)
# Input that isn't JSON gets no reply, not a jq parse error on stderr.
printf '%s' "$INPUT" | jq -e . >/dev/null 2>&1 || exit 0
EVENT=$(printf '%s' "$INPUT" | jq -r '.hook_event_name // empty')
# Cursor names these beforeSubmitPrompt and preToolUse, and sends conversation_id.
case "$EVENT" in
    beforeSubmitPrompt) EVENT=UserPromptSubmit ;;
    preToolUse) EVENT=PreToolUse ;;
esac
# Keep only filename-safe characters so the ID can't escape the marker filename.
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // .conversation_id // "nosession"' | tr -cd 'A-Za-z0-9_-')
# Markers live in a private per-user directory (state-dir.sh). Without a trusted one, markers are
# skipped: the prompt hint still goes out, but there is no PreToolUse nudge or Foundry bridge note.
STATE_DIR=$(agentworks_state_dir) || STATE_DIR=""
MARKER="${STATE_DIR:+$STATE_DIR/agentworks-router-${SESSION_ID:-nosession}}"
NUDGED="${MARKER:+$MARKER.nudged}"
FOUNDRY_MARKER="${MARKER:+$MARKER.foundry}"

HINT='[agentworks] This looks like a Charlotte AI AgentWorks request. Relevant skills:
  - crowdstrike-charlotte-ai-agentworks:agentworks (orchestrator)
  - crowdstrike-charlotte-ai-agentworks:agents (agents created and managed through the API)
  - crowdstrike-charlotte-ai-agentworks:invocation (invoke agents, messages, traces)
  - crowdstrike-charlotte-ai-agentworks:knowledge-bases (KB operations)
  - crowdstrike-charlotte-ai-agentworks:discovery (models/tools/templates/spans)'

case "$EVENT" in
    UserPromptSubmit)
        PROMPT=$(printf '%s' "$INPUT" | jq -r '.prompt // .user_prompt // .query // empty')
        if [[ -n "$MARKER" ]]; then
            rm -f "$MARKER" "$NUDGED" "$FOUNDRY_MARKER"
            # Prune markers left by sessions that ended more than a day ago.
            find "$STATE_DIR" -maxdepth 1 -name 'agentworks-router-*' -mmin +1440 -delete 2>/dev/null
        fi
        # Agents and knowledge bases defined inside a Falcon Foundry app (manifest.yml,
        # ai.agents, `foundry agents create`) belong to foundry-skills, not this plugin. Leave
        # a Foundry marker (read by agentworks-foundry-bridge.sh) when the prompt is about one.
        if printf '%s' "$PROMPT" | grep -qiE '(foundry|manifest\.yml|ai\.agents|ai\.knowledge_bases)'; then
            if printf '%s' "$PROMPT" | grep -qiE '(agent|knowledge.?base)'; then
                [[ -n "$FOUNDRY_MARKER" ]] && : > "$FOUNDRY_MARKER"
            fi
            exit 0
        fi
        if printf '%s' "$PROMPT" | grep -qiE '(agent.?works|agentic.?studio|charlotte.{0,20}(agent|works)|knowledge.?base|(invoke|create|publish|analy[sz]e).{0,30}agent)'; then
            [[ -n "$MARKER" ]] && : > "$MARKER"
            emit_advisory "UserPromptSubmit" "$HINT"
        fi
        ;;
    PreToolUse)
        [[ -n "$MARKER" && -f "$MARKER" ]] || exit 0
        TOOL=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty')
        # A skill call is the goal: stop reminding, but leave the marker for the bridge hook.
        if [[ "$TOOL" == "Skill" ]]; then
            touch "$NUDGED"
            exit 0
        fi
        # Advisory nudge, once per detected prompt -- never block tools.
        [[ -f "$NUDGED" ]] && exit 0
        touch "$NUDGED"
        emit_advisory "PreToolUse" "$HINT
Load the matching skill before running agentworks scripts."
        ;;
esac

exit 0
