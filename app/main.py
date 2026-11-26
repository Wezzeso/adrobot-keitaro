import re
from asyncio import Lock
from hashlib import sha256
from pathlib import Path

import pycountry
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.demo import DemoProvider, DemoValidationError
from app.editor import OfferArchive, StaleFlowError

STATIC = Path(__file__).with_name("static")


class CampaignInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    country: str = Field(min_length=2, max_length=2)
    offer_id: int = Field(gt=0)
    domain_id: int = Field(gt=0)
    group_id: int = Field(gt=0)
    source_id: int = Field(gt=0)

    @field_validator("country")
    @classmethod
    def valid_country(cls, value: str) -> str:
        code = value.upper()
        if pycountry.countries.get(alpha_2=code) is None:
            raise ValueError("Укажите код страны ISO 3166-1 alpha-2.")
        return code


class OfferInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    weight: int = Field(gt=0, le=100)


class OffersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offers: list[OfferInput] = Field(min_length=1, max_length=100)

    @field_validator("offers")
    @classmethod
    def valid_allocation(cls, offers: list[OfferInput]) -> list[OfferInput]:
        if len({offer.id for offer in offers}) != len(offers):
            raise ValueError("Офферы в одной кампании должны быть уникальны.")
        if sum(offer.weight for offer in offers) != 100:
            raise ValueError("Суммарный вес офферов должен составлять 100%.")
        return offers


class EditorOffer(OfferInput):
    pinned: bool = False


class EditorOffersInput(OffersInput):
    offers: list[EditorOffer] = Field(min_length=1, max_length=100)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if bool(settings.keitaro_base_url) != bool(settings.keitaro_api_key):
        raise ValueError("Для Keitaro нужны оба значения: KEITARO_BASE_URL и KEITARO_API_KEY.")

    error_types: tuple[type[Exception], ...] = (DemoValidationError,)
    sessions = sessionmaker(create_engine(settings.database_url))
    tracker = "demo"
    if settings.keitaro_base_url:
        if not re.fullmatch(r"test_[a-z0-9]+_", settings.campaign_prefix):
            raise ValueError("Укажите префикс отдельной тестовой области, например test_ryabov_.")
        from app.provider import KeitaroProvider, ProviderError

        provider = KeitaroProvider(
            settings.keitaro_base_url, settings.keitaro_api_key, editor_prefix=settings.campaign_prefix
        )
        error_types += (ProviderError,)
        tracker = sha256(settings.keitaro_base_url.rstrip("/").encode()).hexdigest()
    else:
        provider = DemoProvider(sessions)

    archive = OfferArchive(sessions, tracker)
    editor_lock = Lock()
    app = FastAPI(title="Adrobot")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/catalog")
    async def catalog():
        try:
            result = await provider.catalog()
        except error_types as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        if settings.keitaro_base_url:
            result["campaigns"] = [
                campaign
                for campaign in result["campaigns"]
                if campaign["name"].startswith(settings.campaign_prefix)
            ]
        result["mode"] = "keitaro" if settings.keitaro_base_url else "demo"
        return result

    @app.get("/api/campaigns/{campaign_id}")
    async def get_campaign(campaign_id: int):
        try:
            result = await provider.get_campaign(campaign_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Кампания не найдена.") from error
        except error_types as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        if settings.keitaro_base_url and not result["name"].startswith(settings.campaign_prefix):
            raise HTTPException(status_code=404, detail="Кампания не найдена.")
        return result

    @app.post("/api/campaigns", status_code=201)
    async def create_campaign(body: CampaignInput):
        payload = body.model_dump()
        if settings.keitaro_base_url and not body.name.startswith(settings.campaign_prefix):
            payload["name"] = settings.campaign_prefix + body.name
        try:
            return await provider.create_campaign(payload)
        except DemoValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except error_types as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.put("/api/campaigns/{campaign_id}/offers")
    async def set_offers(campaign_id: int, body: OffersInput):
        try:
            async with editor_lock:
                if settings.keitaro_base_url:
                    await get_campaign(campaign_id)
                return await provider.set_offers(campaign_id, [offer.model_dump() for offer in body.offers])
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Кампания не найдена.") from error
        except DemoValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except error_types as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.get("/api/campaigns/{campaign_id}/editor")
    async def campaign_editor(campaign_id: int):
        try:
            async with editor_lock:
                campaign = await provider.get_editor_campaign(campaign_id)
                campaign["flows"] = await run_in_threadpool(archive.synchronize, campaign_id, campaign["flows"])
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Кампания не найдена.") from error
        except error_types as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return campaign

    @app.put("/api/campaigns/{campaign_id}/flows/{flow_id}/offers")
    async def publish_flow(campaign_id: int, flow_id: int, body: EditorOffersInput):
        offers = [{"id": offer.id, "weight": offer.weight} for offer in body.offers]
        pins = {offer.id for offer in body.offers if offer.pinned}
        try:
            async with editor_lock:
                flow = await provider.set_editor_offers(campaign_id, flow_id, offers, body.revision)
                [saved] = await run_in_threadpool(
                    archive.synchronize, campaign_id, [flow], pins_by_flow={flow_id: pins}
                )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Поток офферов не найден.") from error
        except StaleFlowError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except DemoValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except error_types as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return saved

    return app


app = create_app()
