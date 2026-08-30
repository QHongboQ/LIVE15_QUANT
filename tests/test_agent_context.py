import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_project_brain_has_compact_bootstrap_and_required_pointers() -> None:
    bootstrap = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    charter = (ROOT / "PROJECT_CHARTER.md").read_text(encoding="utf-8")
    context = (ROOT / "CONTEXT.md").read_text(encoding="utf-8")
    current_state = (ROOT / "CURRENT_STATE.md").read_text(encoding="utf-8")
    progress = (ROOT / "PROJECT_PROGRESS.md").read_text(encoding="utf-8")

    estimated_tokens = int(
        (
            len(bootstrap.split())
            + len(charter.split())
            + len(context.split())
            + len(current_state.split())
            + len(progress.split())
        )
        * 1.35
    )
    assert estimated_tokens <= 5_000
    for pointer in (
        "PROJECT_CHARTER.md",
        "CONTEXT.md",
        "docs/adr/README.md",
        "CURRENT_STATE.md",
        "PROJECT_PROGRESS.md",
        "BUG_REGISTRY.md",
    ):
        assert pointer in bootstrap


def test_project_brain_budget_is_a_lossless_split_threshold() -> None:
    protocol = (ROOT / "docs" / "agents" / "change-protocol.md").read_text(encoding="utf-8")
    state = (ROOT / "CURRENT_STATE.md").read_text(encoding="utf-8")
    progress = (ROOT / "PROJECT_PROGRESS.md").read_text(encoding="utf-8")
    detail = ROOT / "docs" / "project-brain" / "NOMAD_MIGRATION_STATUS_20260830.md"

    assert "split threshold" in protocol
    assert "semantically compress" in protocol
    assert detail.is_file()
    assert "NOMAD_MIGRATION_STATUS_20260830.md" in state
    assert "NOMAD_MIGRATION_STATUS_20260830.md" in progress


def test_platform_blocker_requires_upstream_resolution_first() -> None:
    protocol = (ROOT / "docs" / "agents" / "change-protocol.md").read_text(encoding="utf-8")
    execution = (ROOT / "docs" / "roadmap" / "UPSTREAM_REPLACEMENT_EXECUTION_001.md").read_text(
        encoding="utf-8"
    )

    for text in (protocol, execution):
        assert "UPSTREAM_OFFICIAL_DOCS = CHECKED" in text
        assert "UPSTREAM_TUTORIALS_EXAMPLES = CHECKED" in text
        assert "UPSTREAM_GITHUB_SOURCE_TESTS = CHECKED" in text
        assert "UPSTREAM_GITHUB_ISSUES_PRS = CHECKED" in text
        assert "STANDARD_UPSTREAM_PATH_FOUND = YES/NO" in text
        assert "UPSTREAM_RESOLUTION_EXHAUSTED = YES/NO" in text
        assert "BLOCKER_ALLOWED = YES/NO" in text

    assert "Local invention is a last-last-last fallback" in protocol
    assert "Custom LIVE15 behavior remains a\nlast-last-last option" in execution


def test_skills_manifest_exposes_required_upstream_workflow_and_preserves_adaptations() -> None:
    manifest = json.loads((ROOT / ".agents" / "skills-manifest.json").read_text(encoding="utf-8"))

    assert manifest["source_repository"] == "https://github.com/mattpocock/skills"
    assert manifest["source_revision"] == "6654f6b60cd9d5be8b54c6fafe44346dabeb3b76"
    assert set(manifest["installed_skills"]["user_invoked"]) >= {
        "setup-matt-pocock-skills",
        "ask-matt",
        "grill-with-docs",
        "to-spec",
        "to-tickets",
        "implement",
        "wayfinder",
        "handoff",
        "wait-what",
    }
    assert set(manifest["installed_skills"]["model_invoked"]) >= {
        "diagnosing-bugs",
        "research",
        "tdd",
        "domain-modeling",
        "codebase-design",
        "code-review",
        "resolving-merge-conflicts",
        "wizard",
        "writing-for-agents",
    }
    for relative_path in manifest["adaptation_preservation"].values():
        assert (ROOT / relative_path).is_file()

    required_names = set(manifest["installed_skills"]["user_invoked"]) | set(
        manifest["installed_skills"]["model_invoked"]
    )
    for name in required_names:
        skill = (ROOT / ".agents" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert f"name: {name}" in skill

    for name in manifest["installed_skills"]["user_invoked"]:
        skill = (ROOT / ".agents" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert "disable-model-invocation: true" in skill


def test_new_session_recovery_questions_have_single_source_answers() -> None:
    charter = (ROOT / "PROJECT_CHARTER.md").read_text(encoding="utf-8")
    context = (ROOT / "CONTEXT.md").read_text(encoding="utf-8")
    state = (ROOT / "CURRENT_STATE.md").read_text(encoding="utf-8")
    progress = (ROOT / "PROJECT_PROGRESS.md").read_text(encoding="utf-8")

    assert "Strategic objective" in charter
    assert "H0" in context and "H1" in context and "H2" in context
    assert "ResearchUniverse" in context and "Training Snapshot" in context
    assert "Runtime owner" in context
    assert "HUMAN_GATE_PENDING_DEPLOYMENT_PROOF" in state
    assert "NO TRAINING_GO" in state
    assert "Current reconciliation basis" in progress
    assert "MERGED != DEPLOYED" in progress


def test_strategy_drift_and_new_session_recovery_contract() -> None:
    simulation = (ROOT / "docs" / "agents" / "context-recovery-simulation.md").read_text(
        encoding="utf-8"
    )

    assert "Strategy drift: dataset shortcut — PASS" in simulation
    assert "Strategy drift: supervisor ownership — PASS" in simulation
    assert "Request explicit strategic/human approval" in simulation
    assert "Mutations: 0" in simulation
    for answer in (
        "ResearchUniverseSnapshot",
        "immutable",
        "separately WinSW-owned services",
        "PROJECT_PROGRESS.md",
        "docs/adr/README.md",
        "UNREVEALED_FROZEN",
        "ST-006",
        "NO TRAINING_GO",
    ):
        assert answer in simulation
