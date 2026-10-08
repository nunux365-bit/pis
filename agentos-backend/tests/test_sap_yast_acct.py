"""YAST shared account assignment (category A / asset)."""

from app.procurement.sap_yast_acct import (
    ACCOUNT_ASSIGNMENT_ASSET,
    allocations_from_sap_acct_node,
    material_account_assignment_category,
)


def test_yast_category_a() -> None:
    assert material_account_assignment_category("YAST", "") == ACCOUNT_ASSIGNMENT_ASSET
    assert material_account_assignment_category("YUNB", "") == "K"


def test_allocations_from_sap_yast_qty_without_cc() -> None:
    node = {
        "results": [
            {
                "MasterFixedAsset": "7100001182",
                "CostCenter": "",
                "Quantity": "5.000",
            }
        ]
    }
    allocs, asset = allocations_from_sap_acct_node(node, document_type="YAST")
    assert asset == "7100001182"
    assert len(allocs) == 1
    assert allocs[0]["asset"] == "7100001182"
    assert allocs[0]["qty"] == "5.000"
    assert "cost_center" not in allocs[0]


def test_allocations_from_sap_yast_multi_asset() -> None:
    node = {
        "results": [
            {"MasterFixedAsset": "7100001182", "Quantity": "2.000"},
            {"MasterFixedAsset": "003500008160", "Quantity": "3.000"},
        ]
    }
    allocs, asset = allocations_from_sap_acct_node(node, document_type="YAST")
    assert asset == "7100001182"
    assert allocs == [
        {"asset": "7100001182", "qty": "2.000"},
        {"asset": "003500008160", "qty": "3.000"},
    ]


def test_asset_codes_equal_ignores_leading_zeros() -> None:
    from app.procurement.sap_yast_acct import asset_codes_equal

    assert asset_codes_equal("003500008160", "3500008160")
    assert asset_codes_equal("3500008160", "003500008160")
    assert not asset_codes_equal("003500008160", "003500008161")

