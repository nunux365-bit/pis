"""QA matrix for YSER PO post-create reconcile (offline — no SAP).

Scenario IDs map to the sign-off checklist in scripts/integration/run_yser_po_reconcile_qa_live.py.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_procurement_sap_po_yser import _po_update_header


def _line(
    *,
    service: str,
    group: str,
    ref: str,
    short_text: str = "",
    po_item: str = "",
    cc: str = "CC1",
    qty: str = "1",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "service": service,
        "service_group": group,
        "sap_po_service_ref": ref,
        "unit_price": "100",
        "allocations": [{"cost_center": cc, "qty": qty}],
    }
    if short_text:
        row["short_text"] = short_text
    if po_item:
        row["purchase_order_item"] = po_item
    return row


def _hdr() -> dict[str, str]:
    return _po_update_header()


class TestReconcileGate:
    """When reconcile update should / should not run."""

    def test_qa_r01_complete_no_reconcile(self) -> None:
        from app.procurement.sap_po_z_update import yser_po_create_reconcile_needed

        form = {
            "header": _hdr(),
            "lines": [
                _line(service="SVC-A", group="G1", ref="REF-A", po_item="10"),
                _line(service="SVC-B", group="G1", ref="REF-B", po_item="10"),
            ],
        }
        sap = copy.deepcopy(form)
        assert not yser_po_create_reconcile_needed(sap_form=sap, submitted=form)

    def test_qa_r02_allocation_only_no_reconcile(self) -> None:
        from app.procurement.sap_po_z_update import (
            yser_po_create_reconcile_needed,
            yser_po_has_structural_delta,
        )

        sap = {
            "header": _hdr(),
            "lines": [_line(service="SVC-A", group="G1", ref="REF-A", po_item="10")],
        }
        submitted = copy.deepcopy(sap)
        submitted["lines"][0]["allocations"] = [{"cost_center": "CC2", "qty": "1"}]
        assert yser_po_has_structural_delta(sap_form=sap, submitted=submitted)
        assert not yser_po_create_reconcile_needed(sap_form=sap, submitted=submitted)

    def test_qa_r03_missing_one_service_triggers(self) -> None:
        from app.procurement.sap_po_z_update import yser_po_create_reconcile_needed

        sap = {
            "header": _hdr(),
            "lines": [_line(service="SVC-A", group="G1", ref="REF-A", po_item="10")],
        }
        submitted = copy.deepcopy(sap)
        submitted["lines"].append(_line(service="SVC-B", group="G1", ref="REF-B", po_item="10"))
        assert yser_po_create_reconcile_needed(sap_form=sap, submitted=submitted)

    def test_qa_r04_extra_on_sap_does_not_trigger_add(self) -> None:
        """Reconcile only adds missing submitted lines — extra SAP rows are not patched here."""
        from app.procurement.sap_po_z_update import yser_po_create_reconcile_needed

        submitted = {
            "header": _hdr(),
            "lines": [_line(service="SVC-A", group="G1", ref="REF-A", po_item="10")],
        }
        sap = copy.deepcopy(submitted)
        sap["lines"].append(_line(service="SVC-X", group="G1", ref="REF-X", po_item="10"))
        assert not yser_po_create_reconcile_needed(sap_form=sap, submitted=submitted)


class TestPrepareAndStamp:
    """Identity pairing and PO item stamping."""

    def test_qa_p01_stamp_multi_group_sequential(self) -> None:
        from app.procurement.sap_po_z_payload import stamp_yser_po_grouped_item_numbers

        form = {
            "header": _hdr(),
            "lines": [
                _line(service="A", group="G1", ref="R1"),
                _line(service="B", group="G1", ref="R2"),
                _line(service="C", group="G2", ref="R3"),
                _line(service="D", group="G2", ref="R4"),
            ],
        }
        stamp_yser_po_grouped_item_numbers(form, force=True)
        items = [ln["purchase_order_item"] for ln in form["lines"]]
        assert items == ["00010", "00010", "00020", "00020"]

    def test_qa_p02_stamp_g2_item_20_when_sap_only_has_g1(self) -> None:
        from app.procurement.sap_po_z_payload import (
            prepare_yser_po_create_reconcile_forms,
            stamp_yser_po_grouped_item_numbers,
        )

        submitted = {
            "header": _hdr(),
            "lines": [
                _line(service="A", group="G1", ref="R1"),
                _line(service="C", group="G2", ref="R3"),
            ],
        }
        sap = {
            "header": _hdr(),
            "lines": [_line(service="A", group="G1", ref="R1", po_item="10")],
        }
        sub, _ = prepare_yser_po_create_reconcile_forms(submitted, sap_form=sap)
        g2_items = {ln["purchase_order_item"] for ln in sub["lines"] if ln["service_group"] == "G2"}
        assert g2_items == {"00020"}

        form = copy.deepcopy(submitted)
        stamp_yser_po_grouped_item_numbers(form, sap_form=sap, force=True)
        assert form["lines"][1]["purchase_order_item"] == "00020"

    def test_qa_p03_duplicate_service_code_pairs_by_ref(self) -> None:
        from app.procurement.sap_po_z_payload import prepare_yser_po_create_reconcile_forms

        submitted = {
            "header": _hdr(),
            "lines": [
                _line(service="SVC001", group="G1", ref="REF-A", short_text="One"),
                _line(service="SVC001", group="G1", ref="REF-B", short_text="Two"),
            ],
        }
        sap = {
            "header": _hdr(),
            "lines": [
                {
                    "service": "SVC001",
                    "service_group": "G1",
                    "short_text": "One",
                    "purchase_order_item": "10",
                    "allocations": [{"cost_center": "CC1", "qty": "1"}],
                }
            ],
        }
        _, sap_prep = prepare_yser_po_create_reconcile_forms(submitted, sap_form=sap)
        assert sap_prep["lines"][0]["sap_po_service_ref"] == "REF-A"

    def test_qa_p04_pair_by_short_text_when_no_sap_ref(self) -> None:
        from app.procurement.sap_po_z_payload import prepare_yser_po_create_reconcile_forms

        submitted = {
            "header": _hdr(),
            "lines": [
                _line(service="SVC-A", group="G1", ref="REF-A", short_text="Alpha"),
                _line(service="SVC-B", group="G1", ref="REF-B", short_text="Beta"),
            ],
        }
        sap = {
            "header": _hdr(),
            "lines": [
                {
                    "service": "SVC-A",
                    "service_group": "G1",
                    "short_text": "Alpha",
                    "purchase_order_item": "10",
                    "allocations": [{"cost_center": "CC1", "qty": "1"}],
                }
            ],
        }
        _, sap_prep = prepare_yser_po_create_reconcile_forms(submitted, sap_form=sap)
        assert sap_prep["lines"][0].get("sap_po_service_ref") == "REF-A"


class TestCreateReconcilePosts:
    """What SAP update POST bodies look like."""

    def _run(
        self,
        *,
        submitted: dict[str, Any],
        sap: dict[str, Any],
        existing_items: list[Any],
    ) -> list[dict[str, Any]]:
        from app.procurement.sap_po_z_update import build_yser_po_update_posts

        posts, _ = build_yser_po_update_posts(
            submitted=submitted,
            sap_form=sap,
            po_number="4030011999",
            sap_item_count=len(existing_items),
            ticket_id="qa-matrix",
            create_reconcile=True,
            existing_items=existing_items,
        )
        return posts

    def test_qa_c01_same_group_missing_second_service(self) -> None:
        sap = {
            "header": _hdr(),
            "lines": [_line(service="A", group="G1", ref="R1", po_item="10")],
        }
        submitted = copy.deepcopy(sap)
        submitted["lines"].append(_line(service="B", group="G1", ref="R2", po_item="10"))
        posts = self._run(
            submitted=submitted,
            sap=sap,
            existing_items=[type("S", (), {"item_number": "10", "is_deleted": False})()],
        )
        refs = {
            svc.get("PurgDocItemExternalReference")
            for post in posts
            for item in post["d"]["to_PurchaseOrderItem"]["results"]
            for svc in item["to_Services"]["results"]
            if svc.get("IsDeleted") != "X"
        }
        assert refs == {"R2"}
        assert len(posts) == 1

    def test_qa_c02_multi_group_missing_one_per_item(self) -> None:
        sap = {
            "header": _hdr(),
            "lines": [
                _line(service="A", group="G1", ref="R1", po_item="10"),
                _line(service="C", group="G2", ref="R3", po_item="20"),
            ],
        }
        submitted = copy.deepcopy(sap)
        submitted["lines"].insert(1, _line(service="B", group="G1", ref="R2"))
        submitted["lines"].append(_line(service="D", group="G2", ref="R4"))
        posts = self._run(
            submitted=submitted,
            sap=sap,
            existing_items=[
                type("S", (), {"item_number": "10", "is_deleted": False})(),
                type("S", (), {"item_number": "20", "is_deleted": False})(),
            ],
        )
        refs = {
            svc.get("PurgDocItemExternalReference")
            for post in posts
            for item in post["d"]["to_PurchaseOrderItem"]["results"]
            for svc in item["to_Services"]["results"]
            if svc.get("IsDeleted") != "X"
        }
        assert refs == {"R2", "R4"}
        items = {
            item.get("PurchaseOrderItem")
            for post in posts
            for item in post["d"]["to_PurchaseOrderItem"]["results"]
        }
        assert items == {"00010", "00020"}

    def test_qa_c03_new_po_item_whole_group_missing(self) -> None:
        sap = {
            "header": _hdr(),
            "lines": [_line(service="A", group="G1", ref="R1", po_item="10")],
        }
        submitted = copy.deepcopy(sap)
        submitted["lines"].extend(
            [
                _line(service="C", group="G2", ref="R3"),
                _line(service="D", group="G2", ref="R4"),
            ]
        )
        posts = self._run(
            submitted=submitted,
            sap=sap,
            existing_items=[type("S", (), {"item_number": "10", "is_deleted": False})()],
        )
        item20 = [
            item
            for post in posts
            for item in post["d"]["to_PurchaseOrderItem"]["results"]
            if item.get("PurchaseOrderItem") == "00020"
        ]
        assert len(item20) == 1
        refs = {
            svc.get("PurgDocItemExternalReference")
            for svc in item20[0]["to_Services"]["results"]
            if svc.get("IsDeleted") != "X"
        }
        assert refs == {"R3", "R4"}

    def test_qa_c04_no_posts_when_complete(self) -> None:
        form = {
            "header": _hdr(),
            "lines": [_line(service="A", group="G1", ref="R1", po_item="10")],
        }
        posts = self._run(
            submitted=form,
            sap=copy.deepcopy(form),
            existing_items=[type("S", (), {"item_number": "10", "is_deleted": False})()],
        )
        assert posts == []


class TestOrchestration:
    """End-to-end reconcile hook (mocked SAP)."""

    @pytest.mark.asyncio
    async def test_qa_o01_skips_update_when_complete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.procurement import sap_po_client

        form = {
            "header": _hdr(),
            "lines": [_line(service="A", group="G1", ref="R1", po_item="10")],
        }
        update_mock = AsyncMock()
        monkeypatch.setattr(sap_po_client, "_sap_z_update_yser_po_once", update_mock)
        monkeypatch.setattr(
            sap_po_client,
            "_sap_get_po_body",
            AsyncMock(return_value=({"d": {}}, None)),
        )
        monkeypatch.setattr(
            sap_po_client,
            "_credentials_or_error",
            lambda: (("https://example.test", "u", "p"), None),
        )
        monkeypatch.setattr(
            "app.procurement.sap_po_z_payload.form_from_z_po_read",
            lambda _b, **_: copy.deepcopy(form),
        )
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(sap_po_client.httpx, "AsyncClient", lambda **_: mock_client)

        err = await sap_po_client._reconcile_yser_po_services_after_create(
            po_number="4030011888",
            form=form,
            ticket_id="qa-o01",
        )
        assert err is None
        update_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_qa_o02_create_reconcile_noop_does_not_resubmit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.procurement import sap_po_client

        form = {
            "header": _hdr(),
            "lines": [_line(service="A", group="G1", ref="R1", po_item="10")],
        }
        resubmit_mock = MagicMock(side_effect=AssertionError("resubmit must not run"))
        monkeypatch.setattr(
            "app.procurement.sap_po_z_payload.build_z_yser_po_resubmit_plan",
            resubmit_mock,
        )
        monkeypatch.setattr(
            sap_po_client,
            "_credentials_or_error",
            lambda: (("https://example.test", "u", "p"), None),
        )
        monkeypatch.setattr(
            sap_po_client,
            "_sap_get_po_body",
            AsyncMock(return_value=({"d": {"to_PurchaseOrderItem": {"results": []}}}, None)),
        )
        monkeypatch.setattr(
            "app.procurement.sap_po_z_payload.form_from_z_po_read",
            lambda _b, **_: copy.deepcopy(form),
        )
        monkeypatch.setattr(
            "app.procurement.sap_po_z_payload.count_z_po_sap_items",
            lambda _b: 1,
        )
        monkeypatch.setattr(
            "app.procurement.sap_po_z_payload.parse_z_po_items_from_read",
            lambda _b: (
                None,
                [type("S", (), {"item_number": "10", "is_deleted": False})()],
            ),
        )
        monkeypatch.setattr(
            sap_po_client,
            "_request_with_csrf_retry",
            AsyncMock(side_effect=AssertionError("SAP POST must not run")),
        )

        po_id, err = await sap_po_client._sap_z_update_yser_po_once(
            po_number="4030011888",
            form=form,
            ticket_id="qa-o02",
            parent_pr_number=None,
            create_reconcile=True,
        )
        assert err is None
        assert po_id == "4030011888"
        resubmit_mock.assert_not_called()


class TestNormalUpdateUnchanged:
    """Regression: standard update path must not use create_reconcile."""

    def test_qa_u01_normal_update_still_deletes_service(self) -> None:
        from app.procurement.sap_po_z_update import build_yser_po_update_posts

        sap = {
            "header": _hdr(),
            "lines": [
                _line(service="A", group="G1", ref="R1", po_item="10"),
                _line(service="B", group="G1", ref="R2", po_item="10"),
            ],
        }
        submitted = {"header": sap["header"], "lines": [sap["lines"][0]]}
        posts, _ = build_yser_po_update_posts(
            submitted=submitted,
            sap_form=sap,
            po_number="4030011001",
            sap_item_count=1,
            ticket_id="qa-u01",
            create_reconcile=False,
        )
        deleted = any(
            svc.get("IsDeleted") == "X"
            for post in posts
            for item in post["d"]["to_PurchaseOrderItem"]["results"]
            for svc in item["to_Services"]["results"]
        )
        assert deleted
