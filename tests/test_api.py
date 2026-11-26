import asyncio

import httpx
from alembic import command
from alembic.config import Config

from app.config import Settings
from app.main import create_app


PACKET = {
    "name": "test_ryabov_automation",
    "country": "kz",
    "offer_id": 102,
    "domain_id": 1,
    "group_id": 1,
    "source_id": 1,
}


def test_create_and_replace_offers(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'campaigns.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "heads")

    async def exercise() -> None:
        app = create_app(Settings(database_url=url))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/catalog")).json()["campaigns"] == [{"id": 1, "name": "test_ryabov_demo"}]
            result = await client.post("/api/campaigns", json=PACKET)
            assert result.status_code == 201
            campaign = result.json()
            assert campaign["country"] == "KZ"
            assert campaign["offers"] == [{"id": 102, "name": "Форма заявки", "weight": 100}]
            assert (await client.post("/api/campaigns", json=PACKET)).status_code == 422
            assert (await client.post("/api/campaigns", json={**PACKET, "country": "ZZ"})).status_code == 422

            endpoint = f"/api/campaigns/{campaign['id']}/offers"
            revised = await client.put(endpoint, json={"offers": [{"id": 103, "weight": 35}, {"id": 102, "weight": 65}]})
            assert revised.status_code == 200
            assert [offer["id"] for offer in revised.json()["offers"]] == [103, 102]
            assert (await client.put(endpoint, json={"offers": [{"id": 103, "weight": 50}] * 2})).status_code == 422
            assert (await client.put(endpoint, json={"offers": [{"id": 999, "weight": 100}]})).status_code == 422
            assert (await client.get(endpoint.removesuffix("/offers"))).json() == revised.json()

            assert (await client.put(endpoint, json={"offers": []})).status_code == 422
            assert (await client.put(endpoint, json={"offers": [{"id": 103, "weight": 90}]})).status_code == 422
            removed = await client.put(endpoint, json={"offers": [{"id": 103, "weight": 100}]})
            assert removed.status_code == 200
            assert removed.json()["offers"] == [{"id": 103, "name": "Страница продукта", "weight": 100}]
            assert (await client.get("/api/campaigns/999")).status_code == 404

    asyncio.run(exercise())


def test_editor_persists_archive_and_pins_through_restart(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'editor.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    endpoint = "/api/campaigns/1/flows/2/offers"

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=create_app(Settings(database_url=url)))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            editor = (await client.get("/api/campaigns/1/editor")).json()
            assert editor["flows"][0]["editable"] is False
            first = editor["flows"][1]
            allocation = [
                {"id": 101, "weight": 25, "pinned": True},
                {"id": 102, "weight": 37},
                {"id": 103, "weight": 38},
            ]
            response = await client.put(endpoint, json={"revision": first["revision"], "offers": allocation})
            assert response.status_code == 200, response.text
            initial = response.json()
            removed = await client.put(endpoint, json={"revision": initial["revision"], "offers": [
                {"id": 101, "weight": 25, "pinned": True}, {"id": 103, "weight": 75},
            ]})
            assert removed.status_code == 200, removed.text

        fresh = httpx.ASGITransport(app=create_app(Settings(database_url=url)))
        async with httpx.AsyncClient(transport=fresh, base_url="http://test") as client:
            saved = (await client.get("/api/campaigns/1/editor")).json()["flows"][1]
            assert [(item["id"], item["weight"], item["pinned"], item["archived"]) for item in saved["offers"]] == [
                (101, 25, True, False), (103, 75, False, False), (102, 0, False, True),
            ]
            restored = await client.put(endpoint, json={"revision": saved["revision"], "offers": allocation})
            assert restored.status_code == 200, restored.text
            assert [item["weight"] for item in restored.json()["offers"]] == [25, 37, 38]

    asyncio.run(exercise())
