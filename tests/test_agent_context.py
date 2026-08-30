import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAIN = ROOT / "docs" / "project-brain"


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def estimated_tokens(text: str) -> int:
    """Conservative, dependency-free estimate: one token per four UTF-8 bytes."""
    return math.ceil(len(text.encode("utf-8")) / 4)


def route_estimated_tokens(*relative_paths: str) -> int:
    return sum(estimated_tokens(read(path)) for path in relative_paths)


def test_top_level_agent_and_root_are_compact_recursive_indexes() -> None:
    agents = read("AGENTS.md")
    root = read("docs/project-brain/README.md")

    assert "**Role:** TOP-LEVEL INDEX." in agents
    assert "Git/repo Project Brain is durable; chat history is not." in agents
    assert "ALWAYS-ENTRY" in agents
    assert estimated_tokens(agents) <= 1_250
    assert "**Role:** INDEX." in root
    assert "Each index is recursive" in root
    assert "Git commits and PRs are canonical history" in root


def test_real_recursive_routes_meet_conservative_budget_targets() -> None:
    routes = {
        "planning": (
            "AGENTS.md",
            "docs/project-brain/README.md",
            "docs/project-brain/plan/README.md",
            "docs/project-brain/plan/current-roadmap.md",
        ),
        "capability": (
            "AGENTS.md",
            "docs/project-brain/README.md",
            "docs/project-brain/capabilities/README.md",
            "docs/project-brain/capabilities/records/README.md",
            "docs/project-brain/capabilities/records/recorder/README.md",
            "docs/project-brain/capabilities/records/recorder/truth.md",
        ),
        "dependency": (
            "AGENTS.md",
            "docs/project-brain/README.md",
            "docs/project-brain/dependencies/README.md",
            "docs/project-brain/dependencies/platform/README.md",
            "docs/project-brain/dependencies/platform/runtime-ownership.md",
        ),
        "execution": (
            "AGENTS.md",
            "docs/project-brain/README.md",
            "CURRENT_STATE.md",
            "docs/project-brain/constraints/README.md",
            "docs/project-brain/constraints/execution/README.md",
            "docs/project-brain/constraints/execution/parallel-development.md",
        ),
        "high-risk Recorder execution": (
            "AGENTS.md",
            "docs/project-brain/README.md",
            "CURRENT_STATE.md",
            "PROJECT_CHARTER.md",
            "docs/project-brain/capabilities/README.md",
            "docs/project-brain/capabilities/records/README.md",
            "docs/project-brain/capabilities/records/recorder/README.md",
            "docs/project-brain/capabilities/records/recorder/truth.md",
            "docs/project-brain/constraints/README.md",
            "docs/project-brain/constraints/execution/README.md",
            "docs/project-brain/constraints/execution/parallel-development.md",
        ),
    }

    for name in ("planning", "capability", "dependency", "execution"):
        assert route_estimated_tokens(*routes[name]) <= 3_500

    high_risk_budget = route_estimated_tokens(*routes["high-risk Recorder execution"])
    assert high_risk_budget <= 3_800
    assert high_risk_budget <= 5_000
    assert "PROJECT_CHARTER.md" in routes["high-risk Recorder execution"]


def test_recursive_indexes_are_reachable_compact_and_fact_free() -> None:
    indexes = (
        "README.md",
        "capabilities/README.md",
        "capabilities/records/README.md",
        "capabilities/records/recorder/README.md",
        "capabilities/model-governance/README.md",
        "dependencies/README.md",
        "dependencies/platform/README.md",
        "constraints/README.md",
        "constraints/execution/README.md",
        "plan/README.md",
        "status/README.md",
    )
    for relative_path in indexes:
        text = (BRAIN / relative_path).read_text(encoding="utf-8")
        assert "**Role:** INDEX." in text
        assert "## Update rule" in text
        assert "## Change log" in text
        assert "## Current truth" not in text
        assert len(text.split()) <= 180

    for intermediate in (
        "capabilities/records/README.md",
        "capabilities/records/recorder/README.md",
        "capabilities/model-governance/README.md",
        "dependencies/platform/README.md",
        "constraints/execution/README.md",
    ):
        assert read(f"docs/project-brain/{intermediate}").count(".md`") >= 2

    assert "records/README.md" in read("docs/project-brain/capabilities/README.md")
    assert "recorder/README.md" in read("docs/project-brain/capabilities/records/README.md")
    assert "truth.md" in read("docs/project-brain/capabilities/records/recorder/README.md")
    for removed_flat_leaf in (
        "capabilities/recorder.md",
        "capabilities/reliability.md",
        "capabilities/research-data.md",
        "capabilities/training-and-models.md",
        "dependencies/software-modules.md",
        "dependencies/runtime-ownership.md",
        "constraints/parallel-development.md",
        "constraints/runtime-upstream-boundary.md",
    ):
        assert not (BRAIN / removed_flat_leaf).exists()


def test_authority_leaves_preserve_moved_current_truth() -> None:
    control_center = read("docs/project-brain/capabilities/control-center.md")
    recorder_truth = read("docs/project-brain/capabilities/records/recorder/truth.md")
    throughput = read("docs/project-brain/capabilities/records/recorder/throughput-proof.md")
    reliability = read("docs/project-brain/capabilities/records/reliability.md")
    research = read("docs/project-brain/capabilities/records/research-data.md")
    validation = read("docs/project-brain/capabilities/model-governance/data-and-validation.md")
    training = read("docs/project-brain/capabilities/model-governance/training-and-promotion.md")
    software = read("docs/project-brain/dependencies/platform/software-modules.md")
    runtime = read("docs/project-brain/dependencies/platform/runtime-ownership.md")
    boundary = read("docs/project-brain/constraints/execution/runtime-upstream-boundary.md")
    legacy_receipt = read("docs/project-brain/status/legacy-runtime-receipt.md")

    assert "CONTROL_CENTER_NOMAD_CUTOVER = VERIFIED" in control_center
    assert "rollback only" in control_center
    assert "does not authorize a Recorder migration" in control_center
    assert "Recorder ownership is unchanged" in recorder_truth
    assert "ST-005" in throughput and "60-minute proof" in throughput
    assert "gap002-closure.md" in reliability
    assert "Research coverage comes from the typed" in research
    assert "Only Kalshi finalized settlement" in validation and "fails closed" in validation
    assert "NO TRAINING_GO" in training and "NO TRAINING_STARTED" in training
    assert "Holdout-contamination remediation/replacement" in training
    assert "KalshiGateway / immutable adapter" in software
    assert "ControlCenter is\nNomad-managed" in runtime
    assert "Production writes remain disabled" in boundary
    assert "all three WinSW services running" in legacy_receipt
    assert "no current-main deployment claim" in legacy_receipt


def test_current_roadmap_remains_the_only_sequence_authority() -> None:
    state = read("CURRENT_STATE.md")
    roadmap = read("docs/project-brain/plan/current-roadmap.md")
    progress = read("PROJECT_PROGRESS.md")
    reliability = read("docs/project-brain/capabilities/records/reliability.md")

    assert "## Immediate sequence" not in state
    assert "current-roadmap.md" in state and "current-roadmap.md" in progress
    for expected in (
        "PHASE 1 — COMPLETE",
        "PHASE 2 — COMPLETE / NO-OP",
        "PHASE 3 — COMPLETE",
        "RECORDER_LIFECYCLE_TO_NOMAD",
        "Phase 4A",
        "PHASE 4B — IN PARALLEL",
        "OUT_OF_GAP002_PATH",
    ):
        assert expected in roadmap
    for text in (state, reliability):
        normalized = text.replace("\n", " ")
        assert "frozen" in normalized and "baseline" in normalized


def test_gap002_frozen_baseline_is_declared_without_executing_gap002() -> None:
    closure = read("docs/project-brain/dependencies/gap002-closure.md")
    evidence = read("docs/evidence/GAP002_DEPENDENCY_CLOSURE_DISCOVERY_001.md")
    baseline = read("docs/evidence/GAP002_FROZEN_BASELINE_001.md")

    assert "GAP002_DEPENDENCY_AUDIT_EXECUTED = YES" in closure
    assert "MIGRATE_BEFORE_GAP_SET = NONE" in closure
    assert "RECORDER_NOMAD_MIGRATION_BEFORE_GAP = NOT_REQUIRED" in evidence
    assert "RUNTIME_SUPERVISOR_MIGRATION_BEFORE_GAP = NOT_REQUIRED" in evidence
    assert "PHASE3_COMPLETE = YES" in baseline
    assert "GAP002_EXECUTED = NO" in baseline


def test_progress_history_and_inventory_preserve_lossless_v2_migration() -> None:
    progress = read("PROJECT_PROGRESS.md")
    inventory = read("docs/project-brain/V2_MIGRATION_INVENTORY_001.md")

    assert "Project Brain V2 migration baseline was `c557d52`" in progress
    assert "no fixed SHA is current authority" in progress
    assert "Git commits and PRs are canonical history" in progress
    assert "No durable fact was deleted" in inventory
    assert "Mapping created before the recursive move" in inventory
    assert "capabilities/records/recorder/truth.md" in inventory
    assert "Git commits and PRs remain canonical history" in inventory
    assert "status/legacy-runtime-receipt.md" in inventory
    assert "completed foundation: Terminal V3 / HOT-COLD archive" in inventory
    assert "docs/agents/skills-installation.md" in inventory


def test_high_risk_routing_cannot_be_skipped_for_token_efficiency() -> None:
    agents = read("AGENTS.md")
    constraints = read("docs/project-brain/constraints/README.md")

    for authority in (
        "Production writes",
        "Hard Risk",
        "training/promotion",
        "holdout",
        "Recorder\n  writes/gap/quarantine/resync",
        "settlement labels",
        "deployment/restart",
    ):
        assert authority in agents
    assert "never yield to token efficiency" in agents
    assert "PROJECT_CHARTER.md" in constraints


def test_router_skill_is_recursive_and_stores_no_project_facts() -> None:
    manifest = json.loads(read(".agents/skills-manifest.json"))
    router = read(".agents/skills/live15-project-brain-router/SKILL.md")

    assert "live15-project-brain-router" in manifest["installed_skills"]["model_invoked"]
    assert "first category" in router and "follow its pointers recursively" in router
    assert "Do not hard-code a tree or\nproject facts" in router
    assert "siblings by default" in router
    assert "identified as high-risk by `AGENTS.md`" in router
    assert "## Update routing" in router
    assert "current-root traversal to identify the owner before\nediting" in router
    assert "docs/agents/change-protocol.md" in router
    assert "the single authority for content, structure, ambiguity, and split semantics" in router
    assert "the protocol determines the minimum update surface" in router
    for fact in ("Nomad owns ControlCenter", "NO TRAINING_GO", "kalshi-sdk==12.0.0"):
        assert fact not in router


def test_recursive_project_brain_maintenance_updates_only_the_discovered_owner() -> None:
    protocol = read("docs/agents/change-protocol.md")

    assert "### Recursive Project Brain maintenance" in protocol
    assert "follow one selected index pointer at a time" in protocol
    assert "Indexes own routing; leaves own facts" in protocol
    assert "ownership is ambiguous, **STOP**" in protocol
    assert "update the owning leaf only" in protocol
    assert "Do not synchronize ancestors or siblings" in protocol
    assert "affected child and its direct parent index" in protocol
    assert "only when that higher index's visible routing changes" in protocol
    assert "One durable fact has one authoritative home" in protocol
    assert "duplicate authority is not permitted" in protocol


def test_recursive_split_and_route_health_are_lossless_governance() -> None:
    protocol = read("docs/agents/change-protocol.md")

    assert "split it losslessly into a folder with an INDEX `README.md`" in protocol
    assert "multiple real child authorities" in protocol
    assert "artificial depth limit or meaningless\nsingle-child folder" in protocol
    assert "ceil(len(UTF-8 bytes) / 4)" in protocol
    for route_health_band in (
        "**HEALTHY** is `<=3500`",
        "**WATCH / CONSIDER RECURSIVE SPLIT** is `3501\N{EN DASH}4200`",
        "**SPLIT SOON** is `4201\N{EN DASH}5000`",
        "**HARD\nFAIL** is `>5000`",
    ):
        assert route_health_band in protocol
    assert "never delete durable information to meet a\nbudget" in protocol


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
        assert f"name: {name}" in read(f".agents/skills/{name}/SKILL.md")
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


def test_task_time_official_source_safeguards_remain() -> None:
    policy = read("docs/agents/runtime-official-source-policy.md")
    protocol = read("docs/agents/change-protocol.md")
    execution = read("docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md")

    assert "split threshold" in protocol
    assert "runtime authority to be retrieved by the agent when the task is executed" in policy
    assert "PROMPT_COPIED_VENDOR_PROCEDURE_USED_AS_AUTHORITY = NO" in policy
    assert "must not normally store a step-by-step copy or paraphrase" in policy
    assert "retrieve the current official instructions" in execution
    assert "standing authority for ordinary repo-local engineering" in protocol
    assert "setup-matt-pocock-skills" in protocol
    assert "competing\nproject instruction system" in protocol
    assert "dependency -> pinned dependency/fork -> vendored upstream module" in protocol
    assert "exact observed error text" in protocol
    assert "Respect licenses and attribution" in protocol
