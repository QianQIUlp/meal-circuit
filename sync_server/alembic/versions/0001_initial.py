"""Create the opaque sync service schema.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("login_name", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("change_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("login_name"),
    )
    op.create_index("ix_accounts_login_name", "accounts", ["login_name"], unique=True)
    op.create_table(
        "devices",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("account_id", sa.String(length=80), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_devices_account_id", "devices", ["account_id"])
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("account_id", sa.String(length=80), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.String(length=80), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("access_hash", sa.String(length=64), nullable=False),
        sa.Column("refresh_hash", sa.String(length=64), nullable=False),
        sa.Column("previous_refresh_hash", sa.String(length=64), nullable=True),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("access_hash"), sa.UniqueConstraint("refresh_hash"), sa.UniqueConstraint("previous_refresh_hash"),
    )
    for name in ("account_id", "device_id", "access_hash", "refresh_hash", "previous_refresh_hash"):
        op.create_index(f"ix_auth_sessions_{name}", "auth_sessions", [name], unique=name.endswith("hash"))
    op.create_table(
        "recovery_envelopes",
        sa.Column("account_id", sa.String(length=80), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("envelope_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "remote_entities",
        sa.Column("account_id", sa.String(length=80), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("remote_id", sa.String(length=64), primary_key=True),
        sa.Column("server_version", sa.Integer(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("envelope_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "changes",
        sa.Column("account_id", sa.String(length=80), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("remote_id", sa.String(length=64), nullable=False),
        sa.Column("server_version", sa.Integer(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("envelope_json", sa.Text(), nullable=False),
        sa.Column("op_id", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "op_id", name="uq_change_operation"),
    )
    op.create_index("ix_changes_remote_id", "changes", ["remote_id"])
    op.create_table(
        "processed_operations",
        sa.Column("account_id", sa.String(length=80), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("op_id", sa.String(length=80), primary_key=True),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "device_cursors",
        sa.Column("account_id", sa.String(length=80), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("device_id", sa.String(length=80), sa.ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("cursor_value", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "pairings",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("account_id", sa.String(length=80), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by_device_id", sa.String(length=80), nullable=False),
        sa.Column("claim_token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("envelope_json", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pairings_account_id", "pairings", ["account_id"])
    op.create_table(
        "blobs",
        sa.Column("account_id", sa.String(length=80), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("blob_id", sa.String(length=64), primary_key=True),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in (
        "blobs", "pairings", "device_cursors", "processed_operations", "changes",
        "remote_entities", "recovery_envelopes", "auth_sessions", "devices", "accounts",
    ):
        op.drop_table(table)
