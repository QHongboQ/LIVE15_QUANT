import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_project_brain_has_compact_bootstrap_and_required_pointers() -> None:
    bootstrap = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    charter = (ROOT / "PROJECT_CHARTER.md").read_text(encoding="utf-8")
    context = (ROOT / "CONTEXT.md").read_text(encoding="utf-8")
    current_state = (ROOT / "CURRENT_STATE.md").read_text(encoding="utf-8")

    estimated_tokens = int(
        (
            len(bootstrap.split())
            + len(charter.split())
            + len(context.split())
            + len(current_state.split())
        )
        * 1.35
    )
    assert estimated_tokens <= 3_000
    for pointer in (
        "PROJECT_CHARTER.md",
        "CONTEXT.md",
        "docs/adr/README.md",
        "CURRENT_STATE.md",
        "BUG_REGISTRY.md",
    ):
        assert pointer in bootstrap


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

    assert "Strategic objective" in charter
    assert "H0" in context and "H1" in context and "H2" in context
    assert "ResearchUniverse" in context and "Training Snapshot" in context
    assert "Runtime owner" in context
    assert "BLOCKED_PENDING_EXTERNAL_CLOSEOUT" in state
    assert "Pyth worker unhealthy" in state
    assert "UNRESOLVED_ACTIVE" in state


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
        "Pyth worker",
        "docs/adr/README.md",
    ):
        assert answer in simulation
