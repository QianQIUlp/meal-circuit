"""Add crash-safe account key rotation state.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("accounts")}
    if "active_key_version" not in existing:
        op.add_column("accounts", sa.Column("active_key_version", sa.Integer(), nullable=False, server_default="1"))
    if "rotation_device_id" not in existing:
        op.add_column("accounts", sa.Column("rotation_device_id", sa.String(length=80), nullable=True))
    if "rotation_target_key_version" not in existing:
        op.add_column("accounts", sa.Column("rotation_target_key_version", sa.Integer(), nullable=True))
    if "rotation_started_at" not in existing:
        op.add_column("accounts", sa.Column("rotation_started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("accounts")}
    for name in ("rotation_started_at", "rotation_target_key_version", "rotation_device_id", "active_key_version"):
        if name in existing:
            op.drop_column("accounts", name)
