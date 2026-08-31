"""Pure helpers for narrow LIVE15 Windows service delegation transactions."""

from __future__ import annotations

import re

LIVE15_SERVICES = frozenset({"LIVE15Recorder", "LIVE15ControlCenter"})
_SID = re.compile(r"^S-1-5-21-(?:\d+-){2}\d+-\d+$")


def validate_service_name(name: str) -> None:
    if name not in LIVE15_SERVICES:
        raise ValueError("service is outside LIVE15 delegation scope")


def delegation_ace(sid: str) -> str:
    if not _SID.fullmatch(sid):
        raise ValueError("target SID must be explicit and domain-scoped")
    return f"(A;;LCRPWPLO;;;{sid})"


def insert_delegation_ace(sddl: str, sid: str) -> str:
    """Insert exactly one ACE in DACL, before SACL, rejecting malformed SDDL."""
    ace = delegation_ace(sid)
    compact = re.sub(r"\s+", "", sddl or "")
    if not compact.startswith("D:"):
        raise ValueError("SDDL must contain a DACL")
    sacl = compact.find("S:")
    dacl = compact[2 : sacl if sacl >= 0 else None]
    if not dacl or dacl.count("(") != dacl.count(")"):
        raise ValueError("malformed DACL")
    if ace in dacl:
        return compact
    return (
        compact[: sacl if sacl >= 0 else len(compact)]
        + ace
        + compact[sacl if sacl >= 0 else len(compact) :]
    )


def has_delegation_ace(sddl: str, sid: str) -> bool:
    return delegation_ace(sid) in re.sub(r"\s+", "", sddl or "")
