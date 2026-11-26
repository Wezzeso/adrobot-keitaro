"""Create demo campaign and offer tables."""

import sqlalchemy as sa
from alembic import op

revision = "0001_campaigns"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    campaigns = op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(160), unique=True, nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("domain_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
    )
    offers = op.create_table(
        "campaign_offers",
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("offer_id", sa.Integer(), primary_key=True),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
    )
    op.bulk_insert(campaigns, [{
        "id": 1, "name": "test_ryabov_demo", "country": "KZ", "domain_id": 1, "group_id": 1, "source_id": 1,
    }])
    op.bulk_insert(offers, [{"campaign_id": 1, "offer_id": 101, "weight": 100, "position": 0}])


def downgrade() -> None:
    op.drop_table("campaign_offers")
    op.drop_table("campaigns")
