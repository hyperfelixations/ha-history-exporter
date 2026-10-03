"""Contract tests for the GitHub Actions workflows.

Workflows cannot run under pytest, but their security-relevant properties can
be asserted from the YAML: no stored index token, OIDC only where it belongs,
every action pinned, and publishing gated behind a deliberate choice.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
RELEASE = WORKFLOW_DIR / "release.yml"
PUBLISH = WORKFLOW_DIR / "publish.yml"
TEST_PUBLISH = WORKFLOW_DIR / "test-publish.yml"
TESTS = WORKFLOW_DIR / "tests.yml"
SECURITY = WORKFLOW_DIR / "security.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
REQUIREMENTS = ROOT / "requirements-dev.txt"

#: Everything the development, build and audit tooling needs, pinned exactly.
PINNED_TOOLS = {
    "build",
    "mypy",
    "pip-audit",
    "pytest",
    "pytest-cov",
    "pytest-socket",
    "ruff",
    "twine",
    "types-PyYAML",
    "types-requests",
}

#: Secret names that would replace Trusted Publishing with a stored token.
FORBIDDEN_SECRET_PATTERNS = [
    re.compile(r"PYPI_API_TOKEN", re.IGNORECASE),
    re.compile(r"TWINE_PASSWORD", re.IGNORECASE),
    re.compile(r"TWINE_USERNAME", re.IGNORECASE),
    re.compile(r"secrets\.\w*PYPI\w*", re.IGNORECASE),
]

SHA_PIN = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def workflow_files() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml"))


def steps(job: dict) -> list[dict]:
    return job.get("steps", [])


# ── all workflows ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_workflow_is_valid_yaml(path):
    assert isinstance(load(path), dict)


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_workflow_grants_read_only_permissions_by_default(path):
    assert load(path)["permissions"] == {"contents": "read"}


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_no_workflow_stores_an_index_token(path):
    text = path.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_SECRET_PATTERNS:
        assert not pattern.search(text), (
            f"{path.name} refers to {pattern.pattern}; publishing must stay on "
            "Trusted Publishing without a stored token"
        )


# ── release workflow ──────────────────────────────────────────────────────────


def test_release_runs_only_on_manual_dispatch():
    triggers = load(RELEASE)[True]
    assert set(triggers) == {"workflow_dispatch"}


def test_release_candidate_cannot_select_or_publish_to_an_index():
    workflow = load(RELEASE)
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert "publish_to" not in inputs
    assert "publish" not in workflow["jobs"]
    assert "pypa/gh-action-pypi-publish" not in RELEASE.read_text(encoding="utf-8")


def test_release_candidate_always_creates_an_unpublished_github_draft():
    job = load(RELEASE)["jobs"]["create-draft-release"]
    assert "if" not in job
    assert "--draft" in " ".join(step.get("run", "") for step in steps(job))


def test_real_pypi_requires_a_published_github_release():
    triggers = load(PUBLISH)[True]
    assert triggers == {"release": {"types": ["published"]}}
    assert "workflow_dispatch" not in triggers


def test_real_publish_job_uses_only_verified_release_assets():
    jobs = load(PUBLISH)["jobs"]
    assert set(jobs) == {"verify-release", "publish"}
    assert jobs["publish"]["needs"] == "verify-release"
    commands = " ".join(step.get("run", "") for step in steps(jobs["verify-release"]))
    assert "gh release download" in commands
    assert "sha256sum --check" in commands
    assert "python -m twine check" in commands
    assert 'metadata["Name"] == "ha-history-exporter"' in commands
    assert 'metadata["Version"] == expected_version' in commands


def test_real_publish_job_never_builds_its_own_distributions():
    job = load(PUBLISH)["jobs"]["publish"]
    commands = " ".join(step.get("run", "") for step in steps(job))
    assert "python -m build" not in commands
    assert any("download-artifact" in step.get("uses", "") for step in steps(job))


def test_real_publish_job_uses_the_official_action_and_environment():
    job = load(PUBLISH)["jobs"]["publish"]
    assert job["environment"] == "pypi"
    uses = [step.get("uses", "") for step in steps(job)]
    assert any(item.startswith("pypa/gh-action-pypi-publish@") for item in uses)


def test_testpypi_is_manual_and_cannot_select_real_pypi():
    workflow = load(TEST_PUBLISH)
    assert set(workflow[True]) == {"workflow_dispatch"}
    assert "publish_to" not in workflow[True]["workflow_dispatch"]["inputs"]
    job = workflow["jobs"]["publish"]
    assert job["environment"] == "testpypi"
    upload = next(step for step in steps(job) if "pypi-publish" in step.get("uses", ""))
    assert upload["with"]["repository-url"] == "https://test.pypi.org/legacy/"


def test_release_paths_privacy_scan_history_and_distributions():
    release_jobs = load(RELEASE)["jobs"]
    release_validate = " ".join(step.get("run", "") for step in steps(release_jobs["validate"]))
    release_build = " ".join(step.get("run", "") for step in steps(release_jobs["build"]))
    publish_verify = " ".join(
        step.get("run", "") for step in steps(load(PUBLISH)["jobs"]["verify-release"])
    )
    testpypi_build = " ".join(
        step.get("run", "") for step in steps(load(TEST_PUBLISH)["jobs"]["build"])
    )

    assert "tools/privacy_audit.py --repository . --history" in release_validate
    for commands in (release_build, publish_verify, testpypi_build):
        assert "tools/privacy_audit.py" in commands
        assert "--artifact" in commands


def test_every_distribution_path_installs_and_smoke_tests_the_wheel():
    jobs = [
        load(TESTS)["jobs"]["packaging"],
        load(RELEASE)["jobs"]["build"],
        load(TEST_PUBLISH)["jobs"]["build"],
        load(PUBLISH)["jobs"]["verify-release"],
    ]

    for job in jobs:
        commands = " ".join(step.get("run", "") for step in steps(job))
        assert "tools/verify_wheel.py" in commands


def test_only_actual_upload_jobs_receive_oidc():
    for path in workflow_files():
        for name, job in load(path)["jobs"].items():
            has_oidc = job.get("permissions", {}).get("id-token") == "write"
            assert has_oidc == (
                (path == PUBLISH and name == "publish")
                or (path == TEST_PUBLISH and name == "publish")
            ), f"{path.name}:{name}"


def test_release_validates_the_package_version_against_the_input():
    text = RELEASE.read_text(encoding="utf-8")
    assert "__version__" in text
    assert "package_version" in text


def test_beta_versions_must_be_pep440_normalised():
    """1.4.0-beta.1 would be rebuilt as 1.4.0b1 and no longer match."""
    text = RELEASE.read_text(encoding="utf-8")
    assert "(a|b|rc)[0-9]+" in text
    assert "beta\\.[0-9]+" not in text


# ── action pinning ────────────────────────────────────────────────────────────


def test_every_action_is_pinned_to_a_commit_sha():
    """A moving action tag in any workflow would be an unreviewed code change."""
    jobs = {
        f"{path.name}:{name}": job
        for path in workflow_files()
        for name, job in load(path)["jobs"].items()
    }
    used = [step["uses"] for job in jobs.values() for step in steps(job) if "uses" in step]
    assert used
    for reference in used:
        assert SHA_PIN.match(reference), reference


def test_checkout_never_persists_credentials():
    for path in workflow_files():
        for name, job in load(path)["jobs"].items():
            for step in steps(job):
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step.get("with", {}).get("persist-credentials") is False, (
                        f"{path.name}:{name} persists GitHub credentials"
                    )


def test_every_source_validation_path_checks_formatting():
    jobs = [
        load(TESTS)["jobs"]["static"],
        load(RELEASE)["jobs"]["test"],
        load(TEST_PUBLISH)["jobs"]["test"],
    ]

    for job in jobs:
        commands = " ".join(step.get("run", "") for step in steps(job))
        assert "python -m ruff format --check ." in commands


# ── supply chain ──────────────────────────────────────────────────────────────


def run_commands(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in steps(job))


def test_every_tool_requirement_is_pinned_exactly():
    names = set()
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==\d[\w.]*", line)
        assert match, f"requirements-dev.txt: {line!r} is not an exact pin"
        names.add(match.group(1))
    assert names == PINNED_TOOLS


def test_tools_are_installed_only_from_the_pinned_requirements():
    """An unpinned `pip install build twine` would run whatever PyPI serves that day."""
    for path in workflow_files():
        for name, job in load(path)["jobs"].items():
            for command in run_commands(job).splitlines():
                if "pip install" not in command:
                    continue
                assert not re.search(r"\b(build|twine|pip-audit)\b(?!-)", command), (
                    f"{path.name}:{name}: {command.strip()}"
                )


def test_security_workflow_audits_every_push_pull_request_and_week():
    workflow = load(SECURITY)
    triggers = workflow[True]
    assert set(triggers) == {"push", "pull_request", "schedule", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["pull_request"] == {"branches": ["main"]}
    assert len(triggers["schedule"]) == 1


def test_security_workflow_audits_the_runtime_with_the_pinned_tooling():
    commands = run_commands(load(SECURITY)["jobs"]["audit"])
    install = commands.index("-r requirements-dev.txt")
    assert install < commands.index("python tools/audit_runtime.py")


def test_every_build_path_audits_the_runtime_before_building():
    for path in (RELEASE, TEST_PUBLISH):
        commands = run_commands(load(path)["jobs"]["build"])
        assert "python tools/audit_runtime.py" in commands, path.name
        assert commands.index("python tools/audit_runtime.py") < commands.index(
            "python -m build"
        ), path.name


def test_dependabot_watches_pip_and_the_workflow_actions_weekly():
    updates = {
        entry["package-ecosystem"]: entry
        for entry in yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))["updates"]
    }
    assert set(updates) == {"pip", "github-actions"}
    for entry in updates.values():
        assert entry["directory"] == "/"
        assert entry["schedule"] == {"interval": "weekly"}


def test_dependabot_leaves_the_runtime_lower_bounds_alone():
    """The `>=` bounds in pyproject.toml are a user-facing promise, not a pin."""
    pip = next(
        entry
        for entry in yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))["updates"]
        if entry["package-ecosystem"] == "pip"
    )
    assert pip["versioning-strategy"] == "increase-if-necessary"
