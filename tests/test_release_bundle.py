"""release.sh --bundle refuses to produce an archive the OpenAI plugin portal would reject.

Each test builds a tiny plugin repo in a temp directory, runs the real release.sh on it,
and checks the exit status, the reason printed, and whether a zip was left behind.
"""

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INTERFACE = {
    "displayName": "CrowdStrike AgentWorks",
    "shortDescription": "Build and manage AI agents",
    "composerIcon": "./assets/logo.png",
    "logo": "./assets/logo.png",
    "supportURL": "https://example.com/support",
}


def bundle(tmp_path, interface=None, skill_md: str | None = "# Example\n", extra=None, without=()):
    """Build a minimal plugin repo in tmp_path and run release.sh --bundle on it."""
    shutil.copy2(REPO / "release.sh", tmp_path / "release.sh")
    for d in (".codex-plugin", "assets", "skills/example", "references", "common", "scripts"):
        (tmp_path / d).mkdir(parents=True)
    manifest = {"interface": INTERFACE if interface is None else interface}
    (tmp_path / ".codex-plugin" / "plugin.json").write_text(json.dumps(manifest))
    shutil.copy2(REPO / "assets" / "crowdstrike-logo.png", tmp_path / "assets" / "logo.png")
    if skill_md is not None:
        (tmp_path / "skills" / "example" / "SKILL.md").write_text(skill_md)
    files = {"references/guide.md": "x\n", "common/README.md": "x\n", "scripts/python.sh": "x\n",
             "requirements.txt": "x\n", "LICENSE": "x\n", "README.md": "x\n", **(extra or {})}
    for name, text in files.items():
        if name not in without:
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "Test bundle")
    result = subprocess.run(["bash", "release.sh", "--bundle", "HEAD"],
                            cwd=tmp_path, capture_output=True, text=True, check=False)
    return result, list(tmp_path.glob("*.zip")), list(tmp_path.glob("*.partial"))


def rejected(result, zips, partials, reason):
    """The bundle failed for `reason` and left neither a zip nor a partial file behind."""
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert reason in output, output
    assert "Do not upload it" in output
    assert not zips and not partials


def test_a_valid_plugin_produces_a_zip_without_hooks(tmp_path):
    result, zips, partials = bundle(tmp_path, extra={"hooks/hooks.json": "{}\n", ".claude-plugin/plugin.json": "{}\n"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(zips) == 1 and not partials
    names = zipfile.ZipFile(zips[0]).namelist()
    assert ".codex-plugin/plugin.json" in names and "skills/example/SKILL.md" in names
    assert "references/guide.md" in names
    assert not [n for n in names if n.startswith(("hooks/", ".claude-plugin/"))]


@pytest.mark.parametrize("field", ["displayName", "shortDescription"])
def test_listing_text_over_30_characters_is_rejected(tmp_path, field):
    interface = dict(INTERFACE, **{field: "x" * 31})
    rejected(*bundle(tmp_path, interface), f"interface.{field} is 31 characters")


@pytest.mark.parametrize("field", ["displayName", "shortDescription"])
def test_listing_text_of_exactly_30_characters_is_accepted(tmp_path, field):
    result, zips, _ = bundle(tmp_path, dict(INTERFACE, **{field: "x" * 30}))
    assert result.returncode == 0 and len(zips) == 1


@pytest.mark.parametrize("field", ["displayName", "shortDescription", "composerIcon", "logo"])
def test_a_missing_interface_field_is_rejected(tmp_path, field):
    interface = {k: v for k, v in INTERFACE.items() if k != field}
    rejected(*bundle(tmp_path, interface), f"interface.{field} missing")


@pytest.mark.parametrize("url", [None, "", "http://example.com/support", "example.com"])
def test_support_url_must_be_https(tmp_path, url):
    interface = {k: v for k, v in INTERFACE.items() if k != "supportURL"}
    if url is not None:
        interface["supportURL"] = url
    rejected(*bundle(tmp_path, interface), "interface.supportURL must be a non-empty HTTPS URL")


def test_an_icon_that_is_not_in_the_archive_is_rejected(tmp_path):
    interface = dict(INTERFACE, logo="./images/logo.png")
    rejected(*bundle(tmp_path, interface), "interface.logo -> ./images/logo.png is not in the archive")


def test_a_skill_link_to_a_file_outside_the_bundle_is_rejected(tmp_path):
    """A skill that links to a path BUNDLE_PATHS doesn't ship would be a dead link on Codex."""
    skill = "# Example\n\nSee [the guide](../../docs/guide.md).\n"
    rejected(*bundle(tmp_path, skill_md=skill, extra={"docs/guide.md": "x\n"}), "skills/example/SKILL.md -> ../../docs/guide.md")


def test_a_skill_link_into_references_resolves(tmp_path):
    skill = "# Example\n\nSee [the guide](../../references/guide.md).\n"
    result, zips, _ = bundle(tmp_path, skill_md=skill)
    assert result.returncode == 0 and len(zips) == 1


def test_a_plugin_without_skills_is_rejected(tmp_path):
    rejected(*bundle(tmp_path, skill_md=None, extra={"skills/notes.md": "x\n"}), "No skills/<name>/SKILL.md found")


def test_interface_screenshots_are_rejected(tmp_path):
    interface = dict(INTERFACE, screenshots=["./assets/logo.png"])
    rejected(*bundle(tmp_path, interface), "something a skills-only plugin may not ship")


def test_an_unknown_ref_is_rejected(tmp_path):
    bundle(tmp_path)
    for leftover in tmp_path.glob("*.zip"):
        leftover.unlink()
    result = subprocess.run(["bash", "release.sh", "--bundle", "v9.9.9"],
                            cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "Ref not found: v9.9.9" in result.stdout + result.stderr
    assert not list(tmp_path.glob("*.zip"))


def test_this_repo_bundles_cleanly(tmp_path):
    """The real plugin, at the commit under test, passes its own bundle checks."""
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(REPO), str(clone)], check=True, capture_output=True)
    # Bundle the working tree's files, not just the last commit, so an uncommitted break is caught.
    for path in ("release.sh", ".codex-plugin", "skills", "references", "common", "scripts", "assets",
                 "requirements.txt", "README.md", "LICENSE"):
        src, dst = REPO / path, clone / path
        if src.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, dst)
    for args in (["add", "-A"], ["-c", "user.name=Test", "-c", "user.email=test@example.com",
                                 "commit", "-qm", "Working tree", "--allow-empty"]):
        subprocess.run(["git", *args], cwd=clone, check=True, capture_output=True)
    result = subprocess.run(["bash", "release.sh", "--bundle", "HEAD"],
                            cwd=clone, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    names = zipfile.ZipFile(next(clone.glob("*.zip"))).namelist()
    assert "references/fql-syntax.md" in names
    assert not [n for n in names if n.startswith("hooks/")]


@pytest.mark.parametrize("missing", ["requirements.txt", "common/README.md", "scripts/python.sh"])
def test_a_runtime_dependency_missing_from_the_ref_is_rejected(tmp_path, missing):
    """common/, scripts/ and requirements.txt are used at runtime but no markdown link points at
    them, so nothing else would notice if they were left out."""
    result, zips, partials = bundle(tmp_path, without=(missing,))
    assert result.returncode != 0
    assert not zips and not partials

