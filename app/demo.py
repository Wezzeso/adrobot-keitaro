from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app.editor import StaleFlowError, offer_revision
from app.models import Campaign, CampaignOffer

CATALOG = {
    "offers": {101: "HTML-лендинг", 102: "Форма заявки", 103: "Страница продукта"},
    "domains": {1: "demo.example"},
    "groups": {1: "Тестовые кампании"},
    "sources": {1: "Тестовый источник"},
}
OFFER_FLOW = 2


class DemoValidationError(ValueError):
    pass


def as_options(items: dict[int, str]) -> list[dict]:
    return [{"id": item_id, "name": name} for item_id, name in items.items()]


def as_campaign(campaign: Campaign) -> dict:
    return {
        "id": campaign.id,
        "name": campaign.name,
        "country": campaign.country,
        "offers": [
            {
                "id": offer.offer_id,
                "name": CATALOG["offers"][offer.offer_id],
                "weight": offer.weight,
            }
            for offer in campaign.offers
        ],
    }


def require_choices(name: str, ids: Iterable[int]) -> None:
    invalid = [item_id for item_id in ids if item_id not in CATALOG[name]]
    if invalid:
        raise DemoValidationError(f"Неизвестное значение {name}: {invalid[0]}.")


def offer_flow(offers: list[dict]) -> dict:
    return {
        "id": OFFER_FLOW,
        "name": "Поток офферов",
        "accepts_offers": True,
        "managed": True,
        "revision": offer_revision([(offer["id"], offer["weight"]) for offer in offers]),
        "offers": offers,
    }


class DemoProvider:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    async def catalog(self) -> dict:
        return await run_in_threadpool(self._catalog)

    def _catalog(self) -> dict:
        with self.sessions() as db:
            campaigns = db.scalars(select(Campaign).order_by(Campaign.id)).all()
            return {
                "campaigns": [{"id": campaign.id, "name": campaign.name} for campaign in campaigns],
                **{name: as_options(items) for name, items in CATALOG.items()},
            }

    async def get_campaign(self, campaign_id: int) -> dict:
        return await run_in_threadpool(self._get_campaign, campaign_id)

    async def get_editor_campaign(self, campaign_id: int) -> dict:
        campaign = await self.get_campaign(campaign_id)
        return {
            "id": campaign["id"],
            "name": campaign["name"],
            "flows": [
                {"id": 1, "name": "Поток по стране", "accepts_offers": False, "managed": False},
                offer_flow(campaign["offers"]),
            ],
        }

    def _get_campaign(self, campaign_id: int) -> dict:
        with self.sessions() as db:
            campaign = db.get(Campaign, campaign_id)
            if campaign is None:
                raise KeyError(campaign_id)
            return as_campaign(campaign)

    async def create_campaign(self, payload: dict) -> dict:
        return await run_in_threadpool(self._create_campaign, payload)

    def _create_campaign(self, payload: dict) -> dict:
        require_choices("offers", [payload["offer_id"]])
        require_choices("domains", [payload["domain_id"]])
        require_choices("groups", [payload["group_id"]])
        require_choices("sources", [payload["source_id"]])
        with self.sessions.begin() as db:
            campaign = Campaign(
                name=payload["name"],
                country=payload["country"],
                domain_id=payload["domain_id"],
                group_id=payload["group_id"],
                source_id=payload["source_id"],
                offers=[CampaignOffer(offer_id=payload["offer_id"], weight=100, position=0)],
            )
            db.add(campaign)
            try:
                db.flush()
            except IntegrityError as error:
                raise DemoValidationError("Кампания с таким названием уже есть.") from error
            return as_campaign(campaign)

    async def set_offers(self, campaign_id: int, offers: list[dict]) -> dict:
        return await run_in_threadpool(self._set_offers, campaign_id, offers)

    async def set_editor_offers(
        self, campaign_id: int, flow_id: int, offers: list[dict], revision: str
    ) -> dict:
        if flow_id != OFFER_FLOW:
            raise KeyError(flow_id)
        campaign = await run_in_threadpool(self._set_offers, campaign_id, offers, revision=revision)
        return offer_flow(campaign["offers"])

    def _set_offers(self, campaign_id: int, offers: list[dict], *, revision: str | None = None) -> dict:
        require_choices("offers", [offer["id"] for offer in offers])
        with self.sessions.begin() as db:
            campaign = db.get(Campaign, campaign_id)
            if campaign is None:
                raise KeyError(campaign_id)
            if revision is not None and revision != offer_revision([
                (offer.offer_id, offer.weight) for offer in campaign.offers
            ]):
                raise StaleFlowError()
            existing = {offer.offer_id: offer for offer in campaign.offers}
            updated = []
            for position, item in enumerate(offers):
                offer = existing.get(item["id"])
                if offer is None:
                    offer = CampaignOffer(offer_id=item["id"])
                offer.weight = item["weight"]
                offer.position = position
                updated.append(offer)
            campaign.offers = updated
            db.flush()
            return as_campaign(campaign)
