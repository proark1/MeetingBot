"""Widen webhook secret column for encrypted values.

Revision ID: 8c4b9a1f2d3e
Revises: 07f53dc60e8c
Create Date: 2026-06-29 07:52:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "8c4b9a1f2d3e"
down_revision = "07f53dc60e8c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("webhooks", schema=None) as batch_op:
        batch_op.alter_column(
            "secret",
            existing_type=sa.String(length=255),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("webhooks", schema=None) as batch_op:
        batch_op.alter_column(
            "secret",
            existing_type=sa.Text(),
            type_=sa.String(length=255),
            existing_nullable=True,
        )
