import json
import math
import re
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
    assert "did not itself authorize Recorder migration" in control_center
    assert "separate runtime-ownership authority" in control_center
    assert "Recorder process lifecycle is Nomad-owned" in recorder_truth
    assert "ST-005" in throughput and "60-minute proof" in throughput
    assert "standalone `ST-005` custom-throughput optimization task is retired" in throughput
    assert "No historical 60-minute proof is claimed to have passed" in throughput
    assert "gap002-closure.md" in reliability
    assert "Research coverage comes from the typed" in research
    assert "Only Kalshi finalized settlement" in validation and "fails closed" in validation
    assert "NO TRAINING_GO" in training and "NO TRAINING_STARTED" in training
    assert "Holdout-contamination remediation/replacement" in training
    assert "KalshiGateway / immutable adapter" in software
    assert (
        "ControlCenter,\nRecorder, and the verified `kalshi_sdk_ws_shadow` lifecycle are "
        "Nomad-managed" in runtime
    )
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
        "GAP002` is **CLOSED / PASS**",
        "Normal feature/model expansion is temporarily paused",
        "upstream-consolidation freeze",
        "owner resolution -> freeze legacy generic owner",
        "Project Brain authority consolidation COMPLETE",
        "Runtime/Lifecycle consolidation COMPLETE / VERIFIED",
        "Web Application Shell COMPLETE / VERIFIED",
        "**NEXT:** select the bounded **VECTOR TELEMETRY** replacement responsibility",
    ):
        assert expected in roadmap
    current_roadmap = roadmap.split("## Change log", maxsplit=1)[0]
    for stale in ("Phase 4A", "PHASE 4B", "EXECUTION_PREREQUISITE_PENDING"):
        assert stale not in current_roadmap
        assert stale not in state
        assert stale not in reliability


def test_gap002_historical_baseline_is_preserved_after_durable_closeout() -> None:
    closure = read("docs/project-brain/dependencies/gap002-closure.md")
    evidence = read("docs/evidence/GAP002_DEPENDENCY_CLOSURE_DISCOVERY_001.md")
    baseline = read("docs/evidence/GAP002_FROZEN_BASELINE_001.md")
    first_acceptance = read("docs/evidence/GAP002_FINAL_EVIDENCE_AND_VERDICT_001.md")
    second_acceptance = read("docs/evidence/GAP002_SECOND_PRODUCTION_ACCEPTANCE_001.md")

    assert "GAP002_DEPENDENCY_AUDIT_EXECUTED = YES" in closure
    assert "MIGRATE_BEFORE_GAP_SET = NONE" in closure
    assert "RECORDER_NOMAD_MIGRATION_BEFORE_GAP = NOT_REQUIRED" in evidence
    assert "RUNTIME_SUPERVISOR_MIGRATION_BEFORE_GAP = NOT_REQUIRED" in evidence
    assert "PHASE3_COMPLETE = YES" in baseline
    assert "GAP002_EXECUTED = NO" in baseline
    assert "GAP002 closed/pass" in closure
    assert "No Phase-4 execution route remains active" in closure
    assert "`GAP002 = FAIL`" in first_acceptance
    assert "`GAP002 = PASS`" in second_acceptance
    assert "`FIRST_PRODUCTION_GAP002 = FAIL`" in second_acceptance


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


def test_replacement_first_resolution_preserves_bounded_scope() -> None:
    protocol = read("docs/agents/change-protocol.md")

    assert (
        "approved replacement is selected, run that bounded reversible path first and observe it"
        in protocol
    )
    assert "If a concrete failure remains, diagnose its owner" in protocol
    assert (
        "Perform the full Upstream Reuse First search below when\n"
        "selecting a new owner or considering a new local implementation" in protocol
    )
    assert "a Checker finding is validation feedback against the original task contract" in protocol
    assert "it does not expand the task or become a blocker automatically" in protocol
    assert (
        "targeted upstream evidence needed for the exact observed error text, API, or version"
        in protocol
    )


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
        "runtime-ownership.json",
        "training/promotion authority",
        "Git commits + PRs",
    ):
        assert answer in simulation


def test_single_authority_consolidation_contracts() -> None:
    agents = read("AGENTS.md")
    root = read("docs/project-brain/README.md")
    plan_index = read("docs/project-brain/plan/README.md")
    roadmap = read("docs/project-brain/plan/current-roadmap.md")
    progress = read("PROJECT_PROGRESS.md")
    state = read("CURRENT_STATE.md")
    design_index = read("docs/roadmap/README.md")

    assert "docs/project-brain/README.md" in agents
    assert "plan/README.md" in root
    assert "current-roadmap.md" in plan_index
    assert "../../roadmap/README.md" in plan_index
    assert "The sole current execution-sequence authority" in roadmap
    assert "**NEXT:**" in roadmap

    claims = []
    for path in ROOT.rglob("*.md"):
        if "node_modules" in path.parts:
            continue
        if "The sole current execution-sequence authority" in path.read_text(encoding="utf-8"):
            claims.append(path.relative_to(ROOT).as_posix())
    assert claims == ["docs/project-brain/plan/current-roadmap.md"]

    assert "INDEX_ONLY / NON-CURRENT" in design_index
    assert "## Immediate sequence" not in progress
    assert "**NEXT:**" not in progress
    assert "**NEXT:**" not in state

    authority_path = "docs/project-brain/plan/current-roadmap.md"
    historical_prefixes = (
        "docs/evidence/",
        "docs/deployment/",
        "docs/baselines/",
    )
    historical_names = (
        "docs/project-brain/PROJECT_PROGRESS_DETAIL_",
        "docs/project-brain/NOMAD_OVERNIGHT_HANDOFF_",
        "docs/project-brain/NOMAD_MIGRATION_STATUS_",
        "docs/project-brain/V2_MIGRATION_INVENTORY_",
    )
    current_directive = re.compile(r"(?im)^(?:\*\*)?(?:NEXT|ACTIVE|PLANNED)(?:\*\*)?\s*(?::|—|-)")
    historical_phase_claim = re.compile(r"(?i)\((?:active|planned after[^)]*)\)")
    scanned = []
    for markdown_path in ROOT.rglob("*.md"):
        if "node_modules" in markdown_path.parts:
            continue
        path = markdown_path.relative_to(ROOT).as_posix()
        if path.startswith(historical_prefixes) or path.startswith(historical_names):
            continue
        scanned.append(path)
        text = markdown_path.read_text(encoding="utf-8")
        if path == authority_path:
            assert current_directive.search(text)
            continue
        assert not current_directive.search(text), path
        assert not historical_phase_claim.search(text), path
    assert len(scanned) > 20
    assert authority_path in scanned


def test_compact_progress_has_only_current_or_future_gated_rows() -> None:
    progress = read("PROJECT_PROGRESS.md")
    assert "## Active and gated work\n\n### Current Production runtime authority" in progress
    active = progress.split("## Active and gated work", maxsplit=1)[1].split(
        "## Planning route", maxsplit=1
    )[0]

    assert "| VECTOR-TELEMETRY | PLANNED / NEXT |" in active
    assert "| WEB-APPLICATION-SHELL | PLANNED / NEXT |" not in active
    assert "| TRN-001 | BLOCKED / HOLDOUT_CONTAMINATION_REMEDIATION_REQUIRED |" in active
    for historical_or_superseded in (
        "WS-RESYNC-001 + GAP-002",
        "SHADOW-REC-001",
        "NOMAD-POC-SECURE-001",
        "NOMAD-POC-VALIDATE-001",
        "NOMAD-MIGRATION-STATUS-20260830",
        "NOMAD-CONTROL-CENTER-CUTOVER-FINAL-001",
        "GITHUB-ACTIONS-PUBLIC-20260830",
        "H2-TRAIN-003",
        "ST-005",
        "DEP-001",
        "DEP-ROOT-HYGIENE-PREVENT-001",
    ):
        assert historical_or_superseded not in active

    st005 = next(line for line in progress.splitlines() if line.startswith("| ST-005 |"))
    assert "CANCELLED / SUPERSEDED" in st005
    assert "BLOCKED" not in st005


def test_design_reference_index_is_recursive_and_non_current() -> None:
    root = read("docs/project-brain/README.md")
    plan_index = read("docs/project-brain/plan/README.md")
    design_index = read("docs/roadmap/README.md")

    assert "plan/README.md" in root
    assert "../../roadmap/README.md" in plan_index
    assert "INDEX_ONLY / NON-CURRENT" in design_index
    assert "Current execution ordering is owned only by" in design_index
    assert "current NEXT/ACTIVE/PLANNED task" in design_index

    legacy_root_name = "PROJECT_" + "ROADMAP.md"
    assert not (ROOT / legacy_root_name).exists()
    for markdown_path in ROOT.rglob("*.md"):
        assert legacy_root_name not in markdown_path.read_text(encoding="utf-8")


def test_next_names_one_concrete_responsibility_class() -> None:
    roadmap = read("docs/project-brain/plan/current-roadmap.md")

    next_section = roadmap.split("**NEXT:**", maxsplit=1)[1].split(
        "Candidate-specific boundaries", maxsplit=1
    )[0]
    assert "VECTOR TELEMETRY" in next_section
    assert "Vector is the default" in next_section
    assert "Recorder hot path" in next_section
    assert (
        "does not begin a Vector POC, authorize adoption,\n"
        "or authorize Production mutation" in next_section
    )


def test_web_reconciliation_records_one_completed_owner_and_one_next_candidate() -> None:
    progress = read("PROJECT_PROGRESS.md")
    state = read("CURRENT_STATE.md")
    control_center = read("docs/project-brain/capabilities/control-center.md")
    replacement_matrix = read("docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md")

    assert "| WEB-APPLICATION-SHELL | COMPLETE / VERIFIED |" in progress
    assert "| VECTOR-TELEMETRY | PLANNED / NEXT |" in progress
    assert "Web Application Shell replacement are **COMPLETE / VERIFIED**" in state
    assert "generic replacement class is **VECTOR TELEMETRY**" in state
    assert "packaged React terminal is the sole ControlCenter web owner" in control_center
    assert "Web Application Shell are **ADOPTED / PRODUCTION VERIFIED**" in replacement_matrix
    assert "Vector is the next default telemetry candidate" in replacement_matrix


def test_task_status_route_is_one_child_at_a_time() -> None:
    root = read("docs/project-brain/README.md")
    status_index = read("docs/project-brain/status/README.md")
    task_route = next(line for line in root.splitlines() if line.startswith("| task/status |"))

    assert "`status/README.md`" in task_route
    assert "PROJECT_PROGRESS.md" not in task_route
    assert "../../../PROJECT_PROGRESS.md" in status_index
    assert "task-closeout.md" in status_index


def test_existing_owner_first_precedes_upstream_reuse_first() -> None:
    protocol = read("docs/agents/change-protocol.md")
    agents = read("AGENTS.md")
    closeout = read("docs/project-brain/status/task-closeout.md")

    owner_position = protocol.index("## Existing Owner First")
    upstream_position = protocol.index("## Platform-owned failure gate")
    assert owner_position < upstream_position
    assert "Existing Owner First precedes Upstream Reuse First" in agents
    for field in (
        "EXISTING_AUTHORITY_FOUND",
        "EXISTING_CAPABILITY_FOUND",
        "EXISTING_IMPLEMENTATION_FOUND",
        "EXISTING_PLAN_FOUND",
        "WHY_EXISTING_OWNER_CANNOT_BE_USED",
        "WHY_NEW_OWNER_IS_REQUIRED",
    ):
        assert field in protocol
    assert "LIVE15-specific implementation last-last-last" in protocol
    assert closeout.index("Existing\nOwner First") < closeout.index("Upstream Reuse First")


def test_upstream_consolidation_is_subtractive_and_classified() -> None:
    roadmap = read("docs/project-brain/plan/current-roadmap.md")
    matrix = read("docs/roadmap/UPSTREAM_REPLACEMENT_MATRIX_001.md")
    execution = read("docs/roadmap/UPSTREAM_REPLACEMENT_EXECUTION_001.md")
    throughput = read("docs/project-brain/capabilities/records/recorder/throughput-proof.md")

    for classification in (
        "MUST_REPLACE",
        "CONDITIONAL",
        "RESEARCH_ONLY",
        "KEEP_LOCAL",
        "DO_NOT_INTRODUCE",
    ):
        assert f"**{classification}**" in matrix
    for lifecycle_step in (
        "NAVIGATION / OWNER RESOLUTION",
        "FREEZE LEGACY GENERIC IMPLEMENTATION",
        "REPLACE ONE RESPONSIBILITY",
        "VERIFY REPLACEMENT",
        "RETIRE CORRESPONDING OLD OWNER",
        "FINAL DEEP CLEAN",
    ):
        assert lifecycle_step in execution
    assert "Current execution ordering is owned only by" in " ".join(matrix.split())
    assert "Current execution ordering is owned only by" in " ".join(execution.split())
    assert "Normal feature/model expansion is temporarily paused" in roadmap
    assert "does not authorize DuckDB, Polars, Arrow, NATS" in throughput


def test_legacy_planning_cannot_masquerade_as_current_authority() -> None:
    legacy_files = {
        "docs/roadmap/ROADMAP_001_DATA_TRAINING_ADAPTATION.md": "DESIGN_REFERENCE / NON-CURRENT",
        "docs/roadmap/ROADMAP_002_MODEL_FACTOR_DECISION.md": "DESIGN_REFERENCE / NON-CURRENT",
        "docs/roadmap/ROADMAP_003_RUNTIME_OPERATIONAL_ASSURANCE.md": (
            "HISTORICAL DESIGN_REFERENCE / NON-CURRENT"
        ),
    }
    for path, marker in legacy_files.items():
        text = read(path)
        assert marker in text
        assert "secure isolated service boundary (active)" not in text
        assert "POC operational proof (planned after N1)" not in text
    assert "current approved execution sequence" not in read("docs/roadmap/README.md")


def test_current_recorder_runtime_owner_is_machine_readable_and_consistent() -> None:
    ownership = json.loads(read("deploy/windows/runtime-ownership.json"))
    recorder = next(
        item for item in ownership["components"] if item["component"] == "LIVE15Recorder"
    )
    assert recorder["owner_type"] == "NOMAD_MANAGED"
    assert recorder["owner_id"] == "Nomad:live15-recorder"
    assert recorder["restart_authority"] == "Nomad:live15-recorder"
    assert "Nomad allocation" in recorder["process_source"]

    for component in ("pyth", "coinbase"):
        worker = next(item for item in ownership["components"] if item["component"] == component)
        assert worker["restart_authority"] == "LIVE15Recorder then Nomad:live15-recorder"

    narrative = read("docs/runtime_ownership_and_self_healing.md")
    adr = read("docs/adr/0003-runtime-ownership.md")
    assert "owned only\nby `deploy/windows/runtime-ownership.json`" in narrative
    assert (
        "Recorder, ControlCenter, and `kalshi_sdk_ws_shadow` resolve to Nomad lifecycle ownership."
        in narrative
    )
    assert "Recorder and RuntimeSupervisor\nremain independently WinSW-owned" not in narrative
    assert "Current owner\nvalues are resolved from `deploy/windows/runtime-ownership.json`" in adr
    assert "Windows/WinSW owns service lifecycle" not in adr


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
