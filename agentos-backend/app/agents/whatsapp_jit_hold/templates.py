"""
WhatsApp Cloud API template definitions.

Approve these templates in Meta (category: UTILITY, language: en).
Body placeholders use {{1}}, {{2}}, … — mapped to body parameters in send requests.

Quick-reply buttons:
  - jit_hold_initial_v1:        Option A (id=option_a), Option B (id=option_b)
  - jit_hold_option_a_v2:       CONFIRM (id=confirm), BACK (id=back)
  - jit_hold_option_b_v1:       CONFIRM (id=confirm), BACK (id=back)
  - jit_hold_option_b_back_v1:  Option A (id=option_a), Option B (id=option_b)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    body: str
    footer: str
    buttons: tuple[tuple[str, str], ...]  # (id, title)
    body_value_keys: tuple[str, ...]


TEMPLATE_SPECS: dict[str, TemplateSpec] = {
    "initial": TemplateSpec(
        name="jit_hold_initial_v1",
        body=(
            "Hi {{1}}, we have an update on your order #{{2}}.\n\n"
            "The following items are temporarily on hold and is being sourced from our supply partner.:\n"
            "{{3}}\n\n"
            "The remaining items in your order are ready to ship.\n\n"
            "Here's what you can do:\n\n"
            "Option A — Ship available items now and we'll deliver the held items separately once ready, "
            "or refund them if unresolvable.\n\n"
            "Option B — Hold the entire order until all items are ready.\n\n"
            "Please tap Option A or Option B to avoid further delay. 🙏"
        ),
        footer="~ Team Tata 1mg",
        buttons=(("option_a", "Option A"), ("option_b", "Option B")),
        body_value_keys=("customer_name", "order_id", "held_items"),
    ),
    "option_a": TemplateSpec(
        name="jit_hold_option_a_v2",
        body=(
            "Got it! Here's your split order summary:\n\n"
            "📦 Processing now:\n\n"
            "{{1}}\n\n"
            "🔄 Items on hold:\n\n"
            "{{2}}\n\n"
            "Reply CONFIRM to proceed or BACK to change."
        ),
        footer="~ Team Tata 1mg",
        buttons=(("confirm", "CONFIRM"), ("back", "BACK")),
        body_value_keys=("ship_items", "held_items"),
    ),
    "option_a_done": TemplateSpec(
        name="jit_hold_option_a_done_v1",
        body=(
            "✅ Done! Your order has been split.\n\n"
            "📦 Ship now — updated ETA {{1}}\n"
            "{{2}}\n\n"
            "🔄 Held order(s):\n"
            "{{3}}\n"
            "{{4}}"
        ),
        footer="~ Team Tata 1mg",
        buttons=(),
        body_value_keys=(
            "updated_eta",
            "ship_now_tracking_link",
            "held_orders_status",
            "held_order_tracking_link",
        ),
    ),
    "option_b": TemplateSpec(
        name="jit_hold_option_b_v1",
        body=(
            "Understood! Here's your updated order summary:\n\n"
            "📦 Full order on hold\n"
            "All items will be delivered together.\n\n"
            "🗓️ Revised ETA: {{1}}\n\n"
            "We'll notify you as soon as your order is dispatched.\n\n"
            "Tap CONFIRM to proceed or BACK to change your choice."
        ),
        footer="~ Team Tata 1mg",
        buttons=(("confirm", "CONFIRM"), ("back", "BACK")),
        body_value_keys=("revised_eta",),
    ),
    "option_b_back": TemplateSpec(
        name="jit_hold_option_b_back_v1",
        body=(
            "Here's what you can do:\n\n"
            "Option A — Ship available items now and we'll deliver the held items separately once ready, "
            "or refund them if unresolvable.\n\n"
            "Option B — Hold the entire order until all items are ready. New ETA: {{1}}.\n\n"
            "Please tap Option A or Option B within 2 hours to avoid further delay. 🙏"
        ),
        footer="~ Team Tata 1mg",
        buttons=(("option_a", "Option A"), ("option_b", "Option B")),
        body_value_keys=("revised_eta",),
    ),
    "option_b_done": TemplateSpec(
        name="jit_hold_option_b_done_v1",
        body=(
            "✅ Got it! Your order is on hold and will ship once all items are ready.\n\n"
            "Thank you for your patience! 🙏"
        ),
        footer="~ Team Tata 1mg",
        buttons=(),
        body_value_keys=(),
    ),
}


def format_sku_lines(skus: list[dict[str, Any]], *, held: bool = True) -> str:
    """One line per SKU: 🔴 Name × qty."""
    prefix = "🔴 " if held else "📦 "
    lines: list[str] = []
    for sku in skus:
        name = (sku.get("name") or sku.get("sku_id") or "Item").strip()
        qty = sku.get("qty") or sku.get("quantity") or 0
        try:
            qty_display = int(qty) if float(qty) == int(float(qty)) else qty
        except (TypeError, ValueError):
            qty_display = qty
        lines.append(f"{prefix}{name} × {qty_display}")
    return "\n".join(lines) if lines else "—"


def template_body_values(spec_key: str, context: dict[str, Any]) -> list[str]:
    spec = TEMPLATE_SPECS[spec_key]
    return [str(context.get(k) or "") for k in spec.body_value_keys]


def template_catalog() -> list[dict[str, Any]]:
    """Export for Meta template approval reference."""
    out: list[dict[str, Any]] = []
    for key, spec in TEMPLATE_SPECS.items():
        out.append(
            {
                "key": key,
                "template_name": spec.name,
                "category": "UTILITY",
                "language": "en",
                "body": spec.body,
                "footer": spec.footer,
                "buttons": [{"id": bid, "title": title} for bid, title in spec.buttons],
            }
        )
    return out
