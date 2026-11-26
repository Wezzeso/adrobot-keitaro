from hashlib import sha256
from json import dumps

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import TrackedOffer


class StaleFlowError(ValueError):
    """The tracker allocation changed since it was loaded."""

    def __init__(self):
        super().__init__("Офферы в трекере изменились. Обновите потоки и проверьте состав перед публикацией.")


def offer_revision(allocation: list[tuple[int, int]]) -> str:
    return sha256(dumps(sorted(allocation)).encode()).hexdigest()


class OfferArchive:
    def __init__(self, sessions: sessionmaker[Session], tracker: str):
        self.sessions = sessions
        self.tracker = tracker

    def synchronize(
        self, campaign_id: int, flows: list[dict], *, pins_by_flow: dict[int, set[int]] | None = None
    ) -> list[dict]:
        with self.sessions.begin() as db:
            return [
                self._synchronize_flow(db, campaign_id, flow, (pins_by_flow or {}).get(flow["id"]))
                for flow in flows
            ]

    def _synchronize_flow(self, db: Session, campaign_id: int, flow: dict, pins: set[int] | None) -> dict:
        if not flow["accepts_offers"]:
            return {**flow, "editable": False, "offers": []}

        saved = {
            record.offer_id: record
            for record in db.scalars(
                select(TrackedOffer)
                .where(
                    TrackedOffer.tracker == self.tracker,
                    TrackedOffer.campaign_id == campaign_id,
                    TrackedOffer.flow_id == flow["id"],
                )
                .order_by(TrackedOffer.offer_id)
            )
        }
        active_ids = {offer["id"] for offer in flow["offers"]}
        active = []
        for offer in flow["offers"]:
            offer_id = offer["id"]
            record = saved.get(offer_id)
            if record is None:
                record = TrackedOffer(
                    tracker=self.tracker, campaign_id=campaign_id, flow_id=flow["id"],
                    offer_id=offer_id, name=offer["name"], pinned=False,
                )
                db.add(record)
            record.name = offer["name"]
            if pins is not None:
                record.pinned = offer_id in pins
            active.append({**offer, "pinned": record.pinned, "archived": False})

        archived = []
        for offer_id, record in saved.items():
            if offer_id not in active_ids:
                record.pinned = False
                archived.append({"id": offer_id, "name": record.name, "weight": 0, "pinned": False, "archived": True})

        return {
            **flow,
            "editable": bool(flow["managed"] or active or archived),
            "offers": active + archived,
        }
