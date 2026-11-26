"""Remember offer pins and archives independently of the tracker."""

import sqlalchemy as sa
from alembic import op

revision = "0002_tracked_offers"
down_revision = "0001_campaigns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tracked_offers",
        sa.Column("tracker", sa.String(64), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), primary_key=True),
        sa.Column("flow_id", sa.Integer(), primary_key=True),
        sa.Column("offer_id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tracked_offers")
