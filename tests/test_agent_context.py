import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAIN = ROOT / "docs" / "project-brain"


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_project_brain_v2_uses_intent_based_always_entry() -> None:
    agents = read("AGENTS.md")
    router = read("docs/project-brain/README.md")

    assert "ALWAYS-ENTRY" in agents
    assert "docs/project-brain/README.md" in agents
    assert "fixed five-document bootstrap" not in agents
    for intent in (
        "strategy/permanent authority",
        "vocabulary",
        "current overall state",
        "task/status ledger",
        "what a feature does",
        "what depends on what / ownership / topology",
        "what we plan to do",
        "what constraints apply to executing work",
        "durable architecture decisions",
        "bugs/regressions",
        "audit receipts",
    ):
        assert intent in router

    compact_files = (
        "AGENTS.md",
        "CONTEXT.md",
        "CURRENT_STATE.md",
        "PROJECT_PROGRESS.md",
        "docs/project-brain/README.md",
    )
    assert sum(len(read(path).split()) for path in compact_files) <= 5_000


def test_project_brain_v2_indexes_and_authority_leaves_are_reachable() -> None:
    expected = (
        "README.md",
        "capabilities/README.md",
        "capabilities/recorder.md",
        "capabilities/control-center.md",
        "capabilities/reliability.md",
        "capabilities/research-data.md",
        "capabilities/training-and-models.md",
        "dependencies/README.md",
        "dependencies/runtime-ownership.md",
        "dependencies/software-modules.md",
        "dependencies/gap002-closure.md",
        "plan/README.md",
        "plan/current-roadmap.md",
        "constraints/README.md",
        "constraints/parallel-development.md",
        "constraints/runtime-upstream-boundary.md",
        "status/README.md",
    )
    for relative_path in expected:
        assert (BRAIN / relative_path).is_file()

    for relative_path in expected[2:]:
        text = (BRAIN / relative_path).read_text(encoding="utf-8")
        assert "## Update rule" in text
        assert "## Change log" in text


def test_plan_owns_current_execution_sequence_and_gap002_dual_lane_strategy() -> None:
    state = read("CURRENT_STATE.md")
    roadmap = read("docs/project-brain/plan/current-roadmap.md")
    progress = read("PROJECT_PROGRESS.md")

    assert "## Immediate sequence" not in state
    assert "current-roadmap.md" in state
    assert "GAP002 dependency-closure audit" in roadmap
    assert "PHASE 4A" in roadmap
    assert "PHASE 4B — IN PARALLEL" in roadmap
    assert "OUT_OF_GAP002_PATH" in roadmap
    assert "GAP002_DEPENDENCY_AUDIT_EXECUTED = NO" in roadmap
    assert "current-roadmap.md" in progress


def test_lossless_context_migration_keeps_current_safety_and_runtime_truth() -> None:
    state = read("CURRENT_STATE.md")
    recorder = read("docs/project-brain/capabilities/recorder.md")
    control_center = read("docs/project-brain/capabilities/control-center.md")
    training = read("docs/project-brain/capabilities/training-and-models.md")
    execution = read("docs/project-brain/constraints/runtime-upstream-boundary.md")

    assert "CONTROL_CENTER_NOMAD_CUTOVER = VERIFIED" in control_center
    assert "Nomad" in control_center and "rollback only" in control_center
    assert "Recorder ownership is unchanged" in control_center
    assert "does not authorize a Recorder migration" in control_center
    assert "ST-005" in recorder and "measured proof" in recorder
    assert "NO TRAINING_GO" in training and "NO TRAINING_STARTED" in training
    assert "holdout-contamination remediation/replacement" in training
    assert "Production writes remain disabled" in execution
    assert "MERGED != DEPLOYED" in state and "DEPLOYED != VERIFIED" in state


def test_high_risk_routing_cannot_be_skipped_for_token_efficiency() -> None:
    agents = read("AGENTS.md")
    constraints = read("docs/project-brain/constraints/README.md")

    for authority in (
        "Production writes",
        "Hard Risk",
        "training/promotion",
        "holdout",
        "Recorder writes/gap/quarantine/resync",
        "settlement labels",
        "deployment/restart",
    ):
        assert authority in agents
    assert "Token efficiency may never bypass" in agents
    assert "PROJECT_CHARTER.md" in constraints


def test_router_skill_routes_categories_without_storing_project_facts() -> None:
    manifest = json.loads(read(".agents/skills-manifest.json"))
    router_path = ROOT / ".agents" / "skills" / "live15-project-brain-router" / "SKILL.md"
    router = router_path.read_text(encoding="utf-8")

    assert "live15-project-brain-router" in manifest["installed_skills"]["model_invoked"]
    assert "name: live15-project-brain-router" in router
    assert "capabilities/README.md" in router
    assert "dependencies/README.md" in router
    assert "plan/README.md" in router
    assert "constraints/README.md" in router
    assert "Nomad owns ControlCenter" not in router
    assert "NO TRAINING_GO" not in router


def test_platform_blocker_requires_upstream_resolution_first() -> None:
    protocol = read("docs/agents/change-protocol.md")
    execution = read("docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md")

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


def test_context_recovery_and_skill_provenance_remain_available() -> None:
    simulation = read("docs/agents/context-recovery-simulation.md")
    manifest = json.loads(read(".agents/skills-manifest.json"))

    assert "Intent-based Project Brain V2" in simulation
    assert "PROJECT_BRAIN_V2" in simulation
    assert "Request explicit strategic/human approval" in simulation
    assert "Mutations: 0" in simulation
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
    required_names = set(manifest["installed_skills"]["user_invoked"]) | set(
        manifest["installed_skills"]["model_invoked"]
    )
    for name in required_names:
        skill = read(f".agents/skills/{name}/SKILL.md")
        assert f"name: {name}" in skill
    for name in manifest["installed_skills"]["user_invoked"]:
        assert "disable-model-invocation: true" in read(f".agents/skills/{name}/SKILL.md")
    for relative_path in manifest["adaptation_preservation"].values():
        assert (ROOT / relative_path).is_file()
    for answer in (
        "ResearchUniverseSnapshot",
        "H0/H1/H2",
        "ControlCenter is Nomad-owned",
        "NO TRAINING_GO",
        "Git commits + PRs",
    ):
        assert answer in simulation


def test_lossless_inventory_and_task_time_official_source_safeguards_remain() -> None:
    inventory = read("docs/project-brain/V2_MIGRATION_INVENTORY_001.md")
    policy = read("docs/agents/runtime-official-source-policy.md")
    protocol = read("docs/agents/change-protocol.md")

    assert "No durable fact was deleted" in inventory
    assert "ControlCenter Nomad ownership" in inventory
    assert "training/holdout gates" in inventory
    assert "split threshold" in protocol
    assert "runtime authority to be retrieved by the agent when the task is executed" in policy
    assert "PROMPT_COPIED_VENDOR_PROCEDURE_USED_AS_AUTHORITY = NO" in policy
    assert "runtime-official-source-policy.md" in protocol
    execution = read("docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md")
    assert "runtime-official-source-policy.md" in execution
    assert "must not normally store a step-by-step copy or paraphrase" in policy
    assert "retrieve the current official instructions" in execution
    for path in (
        "docs/project-brain/capabilities/README.md",
        "docs/project-brain/dependencies/README.md",
        "docs/project-brain/plan/README.md",
        "docs/project-brain/constraints/README.md",
        "docs/project-brain/status/README.md",
    ):
        assert len(read(path).split()) <= 180
