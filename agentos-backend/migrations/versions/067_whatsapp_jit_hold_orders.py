"""WhatsApp JIT-hold parent orders for Responder list.

Revision ID: 067_whatsapp_jit_hold_orders
Revises: 066_crl_overrides_contract_rate_line
"""

from alembic import op

revision = "067_whatsapp_jit_hold_orders"
down_revision = "066_crl_overrides_contract_rate_line"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS whatsapp_jit_hold_orders (
            parent_order_id TEXT PRIMARY KEY,
            status VARCHAR(16) NOT NULL DEFAULT 'triggered',
            triggered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_whatsapp_jit_hold_orders_status
              CHECK (status IN ('triggered', 'split_done', 'kept_original'))
        );
        """
    )
    op.execute(
        """
        ALTER TABLE whatsapp_jit_hold_orders
          ALTER COLUMN status SET DEFAULT 'triggered';
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_whatsapp_jit_hold_orders_triggered_at;")
    op.execute("DROP INDEX IF EXISTS ix_whatsapp_jit_hold_orders_status_triggered;")
    op.execute(
        """
        CREATE INDEX ix_whatsapp_jit_hold_orders_triggered_at
        ON whatsapp_jit_hold_orders (triggered_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_whatsapp_jit_hold_orders_status_triggered
        ON whatsapp_jit_hold_orders (status, triggered_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_whatsapp_jit_hold_orders_status_triggered;")
    op.execute("DROP INDEX IF EXISTS ix_whatsapp_jit_hold_orders_triggered_at;")
    op.execute("DROP TABLE IF EXISTS whatsapp_jit_hold_orders;")
