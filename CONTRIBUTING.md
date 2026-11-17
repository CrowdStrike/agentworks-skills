![CrowdStrike Falcon](/images/cs-logo.png?raw=true)

# Contributing to agentworks-skills

Thanks for your interest in improving `agentworks-skills`. This document covers how to report issues, submit changes, and the bar a change must clear before it merges.

## Reporting Issues

Open an issue on GitHub for bugs, documentation fixes, and enhancement requests. Please format bug reports as an [MCVE](https://stackoverflow.com/help/minimal-reproducible-example) (Minimal, Complete, Verifiable Example) — see [SUPPORT.md](SUPPORT.md). Never include real credentials, customer data, or CID-specific identifiers in an issue.

## Pull Requests

1. Fork the repo and create a feature branch from `main`.
2. Make focused, single-purpose changes — smaller PRs review faster.
3. Run the checks locally and confirm they pass (see Testing below).
4. Update the relevant `SKILL.md`, reference docs, and `CHANGELOG.md` as needed.
5. Open the PR with a clear description of what changed and why.

### Merge Criteria

A change merges when it:

- Passes every CI job: ShellCheck, the hook tests, pytest on Python 3.14 and 3.15, pylint at 10/10, the structural checks, and markdownlint.
- Doesn't lower test coverage. The floor in `.github/workflows/main.yml` only moves up.
- Keeps every Python script self-contained (shared code in `common/scripts/` only).
- Preserves the discipline rules — no hardcoded credentials, and every model, tool, and agent ID comes from a query, never a guess.
- Updates `CHANGELOG.md` for any user-facing change.
- Has at least one maintainer approval.

## Testing

The tests need Python 3.14 or newer. Every API call is mocked, so no credentials are needed.

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements-test.txt

pytest tests/ --cov --cov-fail-under=93    # unit tests; coverage settings are in pyproject.toml
pylint --rcfile=.pylintrc common/scripts/*.py skills/*/scripts/*.py
```

The shell checks need only bash, `jq`, and Python:

```bash
./test-hooks.sh                            # router and Foundry bridge hooks
./test-bootstrap-symlink.sh                # scripts still resolve when a skill is installed as a symlink
shellcheck hooks/*.sh scripts/*.sh && shellcheck --severity=error ./*.sh
npx markdownlint-cli2 "**/*.md" "#.venv"
```

CI runs the same commands, plus structural checks (JSON manifests, SKILL.md frontmatter and size budgets, and one version across every manifest). All of them must pass before a PR is ready for review.

When you change a script, add or update a test in `tests/` first and watch it fail, so you know it can catch the bug. A script that calls the API also belongs in the list in `tests/test_cli_errors.py`, which checks that an HTTP error prints one `ERROR:` line and exits 1. When you change a hook, add a case to `test-hooks.sh`.

## Adding a New Skill

Follow the established pattern so the orchestrator and tests stay consistent:

1. Create `<skill>/SKILL.md` with the standard frontmatter (`name`, `description` with TRIGGER / DO NOT TRIGGER, `version`, `tags`, `author`, `license`, `compatibility`, `metadata.category`).
2. Open the body with a "Read this first" block (role, immediate actions, and MUST NOT).
3. Put executable scripts in `<skill>/scripts/` and import auth from `common/scripts/auth.py`.
4. Put long-form docs in `<skill>/references/` and link them from a Reading Guide table.
5. Add a routing entry to the `agentworks` orchestrator decision tree.
6. Add tests for the new scripts in `tests/`, and list each one in `tests/test_cli_errors.py`.

## Code Standards

- **Python**: docstrings on every script and function; `argparse` for CLI flags; a `--json` mode for machine-readable output where it makes sense.
- **Credentials**: loaded from environment variables or the TOML profile via `auth.py`, never hardcoded.
- **Dependencies**: pin versions, with the documented exception of `crowdstrike-falconpy`, which stays unpinned per CrowdStrike guidance.

---

<p align="center">WE STOP BREACHES</p>
