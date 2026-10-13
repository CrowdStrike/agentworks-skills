# Releasing

Checklist for maintainers publishing a new version of the Charlotte AI AgentWorks Skills plugin.
This is a maintainer-only task — contributors don't need to do any of this to get a PR merged.

Run `./release.sh` from a clean `main` (interactive major/minor/patch bump, or
`./release.sh --version X.Y.Z`). It:

1. Bumps the version in `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`,
   `plugin.json`, `.codex-plugin/plugin.json`, `.cursor-plugin/plugin.json`, and
   `.agents/plugins/marketplace.json` (`plugins[].version` and `plugins[].source.ref`).
2. Updates `version`/`updated` in each `SKILL.md` and dates the `## [X.Y.Z] - TBD` entry in
   [`CHANGELOG.md`](../CHANGELOG.md). Add that `TBD` entry (and the
   `[X.Y.Z]: .../releases/tag/vX.Y.Z` link at the bottom) before running the script.
3. Commits on a `release/vX.Y.Z` branch, pushes it, and opens the PR.

After the PR merges, tag and publish the release:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

## Codex / OpenAI Plugins bundle

The OpenAI Plugins portal rejects plugins that contain `hooks/`, so the Codex ZIP is built
separately from the repo:

```bash
./release.sh --bundle vX.Y.Z
```

This archives only the paths in `BUNDLE_PATHS` (no `hooks/`, no Claude/Cursor manifests) and
validates it before writing `crowdstrike-charlotte-ai-agentworks-X.Y.Z.zip`: a single plugin
root, square icons, `displayName`/`shortDescription` of at most 30 characters, an HTTPS
`supportURL`, at least one skill, and every path the skills reference. Upload the ZIP at
<https://platform.openai.com/plugins>.

Public directory submission is currently blocked for skills-only plugins; a remote MCP server on
a public HTTPS endpoint is required. Until then, distribute through the repo marketplace
(`.agents/plugins/marketplace.json`), where hooks are allowed.
