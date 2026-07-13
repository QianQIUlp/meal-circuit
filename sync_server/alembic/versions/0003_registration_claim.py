"""make first-user registration durable and race-safe

Revision ID: 0003
Revises: 0002
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "registration_claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    connection = op.get_bind()
    existing = connection.execute(sa.text("SELECT COUNT(*) FROM accounts")).scalar_one()
    if existing:
        connection.execute(
            sa.text("INSERT INTO registration_claims(id, claimed_at) VALUES (1, :claimed_at)"),
            {"claimed_at": datetime.now(timezone.utc)},
        )


def downgrade() -> None:
    op.drop_table("registration_claims")
