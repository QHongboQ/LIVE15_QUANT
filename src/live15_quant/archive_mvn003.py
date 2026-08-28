"""Typed, bounded bridge from verified COLD archive evidence to the MVN-003 runner.

This module deliberately performs no archive I/O itself and never starts a runner.  The
Research Data Authority remains the sole archive-selection seam; callers receive the immutable
selection, universe, canonical evidence, and runner input needed for an explicitly bounded run.
"""

from __future__ import annotations

from dataclasses import dataclass

from .archive_research import ArchiveResearchQuery, ArchiveResearchSelection
from .canonical_evidence import CanonicalEvidenceSnapshot, build_canonical_evidence_snapshot
from .research_data_authority import ResearchDataAuthority, ResearchUniverseSnapshot
from .research_runner import ResearchRunInput


@dataclass(frozen=True, slots=True)
class ArchiveMvn003Preparation:
    """Immutable, provenance-preserving input prepared for a future bounded runner invocation."""

    selection: ArchiveResearchSelection
    research_universe: ResearchUniverseSnapshot
    canonical_evidence: CanonicalEvidenceSnapshot
    run_input: ResearchRunInput

    def __post_init__(self) -> None:
        if not self.selection.available:
            raise ValueError("unavailable archive selection cannot prepare an MVN-003 input")
        if self.run_input.research_universe is not self.research_universe:
            raise ValueError("runner input must retain the prepared ResearchUniverseSnapshot")
        if self.run_input.canonical_evidence is not self.canonical_evidence:
            raise ValueError("runner input must retain the prepared CanonicalEvidenceSnapshot")


def prepare_archive_research_run(
    authority: ResearchDataAuthority,
    query: ArchiveResearchQuery,
    *,
    code_git_sha: str,
    experiment_id: str,
    model_family: str,
) -> ArchiveMvn003Preparation:
    """Prepare the sole supported COLD archive → MVN-003 typed input path.

    The authority verifies and materializes the explicit range.  This bridge then derives a
    canonical evidence snapshot solely from that selection and lets ``ResearchRunInput`` enforce
    the existing typed/holdout preflight before returning anything to a caller.
    """

    universe, selection = authority.archive_research_snapshot(query, code_git_sha=code_git_sha)
    evidence = build_canonical_evidence_snapshot(
        experiment_id=experiment_id,
        experiment_cutoff=query.as_of_timestamp,
        records=(selection.canonical_evidence_record(),),
    )
    run_input = ResearchRunInput(
        research_universe=universe,
        canonical_evidence=evidence,
        model_family=model_family,
    )
    return ArchiveMvn003Preparation(selection, universe, evidence, run_input)


__all__ = ["ArchiveMvn003Preparation", "prepare_archive_research_run"]
