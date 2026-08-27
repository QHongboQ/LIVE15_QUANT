import pytest

from live15_quant.service_acl import (
    has_delegation_ace,
    insert_delegation_ace,
    validate_service_name,
)

SID = "S-1-5-21-2328573380-2799867724-567227526-1004"
BASE = "D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)S:(AU;FA;;;WD)"


def test_inserts_before_sacl_and_verifies_exact_ace() -> None:
    result = insert_delegation_ace(BASE, SID)
    assert result.index("(A;;LCRPWPLO;;;") < result.index("S:")
    assert has_delegation_ace(result, SID)


def test_duplicate_is_idempotent_and_unrelated_sddl_preserved() -> None:
    once = insert_delegation_ace(BASE, SID)
    assert insert_delegation_ace(once, SID) == once
    assert "(A;;CCLCSWRPWPDTLOCRRC;;;SY)" in once


def test_malformed_sddl_fails_closed() -> None:
    with pytest.raises(ValueError):
        insert_delegation_ace("D:(A;;BROKEN", SID)


def test_service_scope_is_explicit() -> None:
    validate_service_name("LIVE15RuntimeSupervisor")
    with pytest.raises(ValueError):
        validate_service_name("Spooler")
