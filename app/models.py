from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    country: Mapped[str] = mapped_column(String(2))
    domain_id: Mapped[int]
    group_id: Mapped[int]
    source_id: Mapped[int]
    offers: Mapped[list["CampaignOffer"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan",
        order_by="CampaignOffer.position",
    )


class CampaignOffer(Base):
    __tablename__ = "campaign_offers"

    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    offer_id: Mapped[int] = mapped_column(primary_key=True)
    weight: Mapped[int]
    position: Mapped[int]
    campaign: Mapped[Campaign] = relationship(back_populates="offers")


class TrackedOffer(Base):
    __tablename__ = "tracked_offers"

    tracker: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[int] = mapped_column(primary_key=True)
    flow_id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    pinned: Mapped[bool]
