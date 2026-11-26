import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app
from app.provider import KeitaroProvider, ProviderError


API = "/admin_api/v1"
API_KEY = "fixture-key"
CAMPAIGN = {"id": 41, "name": "AdRobot example", "state": "disabled", "token": "fixture-secret"}
OFFER_1 = {"offer_id": 11, "share": 100, "state": "active"}
FLOW = {
    "id": 51,
    "campaign_id": 41,
    "comments": "adrobot:managed-offers:v1:41",
    "schema": "landings",
    "type": "default",
    "state": "active",
    "filters": [],
    "offers": [OFFER_1],
}
REDIRECT = {
    "id": 50,
    "campaign_id": 41,
    "name": "AdRobot country redirect",
    "schema": "redirect",
    "type": "regular",
    "position": 1,
    "state": "active",
    "action_type": "http",
    "action_payload": "https://www.google.com/",
    "filters": [{"name": "country", "mode": "accept", "payload": ["KZ"]}],
}
NEW_CAMPAIGN = {
    "name": "AdRobot example",
    "country": "kz",
    "offer_id": 11,
    "domain_id": 21,
    "group_id": 31,
    "source_id": 71,
}


class TrackerFixture:
    def __init__(self):
        self.requests = []
        self.routes = {}
        self.route("GET", "/campaigns?offset=0&limit=100", [CAMPAIGN])
        self.route("GET", "/campaigns/41", CAMPAIGN)
        self.route("GET", "/campaigns/41/streams", [FLOW])
        self.route(
            "GET",
            "/offers",
            [
                {"id": 11, "name": "Offer A", "action_payload": "fixture-private-url"},
                {"id": 12, "name": "Offer B", "state": "active"},
                {"id": 13, "name": "Disabled offer", "state": "disabled"},
            ],
        )
        self.route("GET", "/domains", [{"id": 21, "name": "fixture.example"}])
        self.route("GET", "/groups?type=campaigns", [{"id": 31, "name": "Group A"}])
        self.route("GET", "/traffic_sources", [{"id": 71, "name": "Source A"}])

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def handle_request(self):
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size)) if size else None
                auth = self.headers.get("Api-Key")
                self_fixture.requests.append((self.command, self.path, body, auth))
                status, headers, response = self_fixture.routes.get(
                    (self.command, self.path), (404, {}, {"error": "unknown fixture route"})
                )
                if callable(response):
                    response = response(body)
                encoded = json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except BrokenPipeError:
                    pass

            do_GET = handle_request
            do_POST = handle_request
            do_PUT = handle_request

        self_fixture = self
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def route(self, method: str, path: str, response, *, status=200, headers=None):
        self.routes[(method, API + path)] = (status, headers or {}, response)

    def writes(self):
        return [
            (method, path.removeprefix(API), body)
            for method, path, body, _ in self.requests
            if method in {"POST", "PUT"}
        ]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ProviderContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = TrackerFixture()
        self.addCleanup(self.fixture.close)
        self.provider = KeitaroProvider(self.fixture.url, API_KEY, timeout=1)

    async def test_catalog_routes_auth_and_response_redaction(self):
        provider = KeitaroProvider(self.fixture.url + API + "/", API_KEY, timeout=1)
        self.assertEqual(
            await provider.catalog(),
            {
                "campaigns": [{"id": 41, "name": "AdRobot example"}],
                "offers": [{"id": 11, "name": "Offer A"}, {"id": 12, "name": "Offer B"}],
                "domains": [{"id": 21, "name": "fixture.example"}],
                "groups": [{"id": 31, "name": "Group A"}],
                "sources": [{"id": 71, "name": "Source A"}],
            },
        )
        self.assertEqual(len(self.fixture.requests), 5)
        self.assertTrue(all(key == API_KEY for _, _, _, key in self.fixture.requests))

    async def test_api_creates_both_routes_and_only_edits_fallback(self):
        campaign = {**CAMPAIGN, "name": "test_ryabov_AdRobot example"}
        flows = []
        self.fixture.route("POST", "/campaigns", campaign)
        self.fixture.route("GET", "/campaigns/41", campaign)
        self.fixture.route("GET", "/campaigns/41/streams", flows)

        def add_flow(body):
            flow = {**body, "id": 50 + len(flows)}
            flows.append(flow)
            return flow

        def update_fallback(body):
            flows[1].update(body)
            return flows[1]

        self.fixture.route("POST", "/streams", add_flow)
        self.fixture.route("PUT", "/streams/51", update_fallback)
        app = create_app(Settings(keitaro_base_url=self.fixture.url, keitaro_api_key=API_KEY))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/campaigns", json=NEW_CAMPAIGN)
            self.assertEqual(created.status_code, 201, created.text)
            self.assertEqual(created.json()["name"], campaign["name"])
            self.assertNotIn("token", created.json())
            self.assertEqual(flows[0], REDIRECT)
            self.assertEqual(flows[1]["offers"], [OFFER_1])

            updated = await client.put(
                "/api/campaigns/41/offers", json={"offers": [{"id": 11, "weight": 70}, {"id": 12, "weight": 30}]}
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            details = await client.get("/api/campaigns/41")
            self.assertEqual(details.json(), updated.json())
            self.assertEqual([offer["weight"] for offer in details.json()["offers"]], [70, 30])
            self.assertEqual(flows[0], REDIRECT)
            self.assertEqual(flows[1]["filters"], [])
            self.assertEqual(
                [(method, path) for method, path, _ in self.fixture.writes()],
                [("POST", "/campaigns"), ("POST", "/streams"), ("POST", "/streams"), ("PUT", "/streams/51")],
            )

    async def test_campaign_create_posts_disabled_campaign_redirect_and_fallback(self):
        self.fixture.route("POST", "/campaigns", CAMPAIGN)

        def create_flow(body):
            if body["type"] == "regular":
                # The API can return serialized filter payloads.
                return {**REDIRECT, "filters": [{**REDIRECT["filters"][0], "payload": '["KZ"]'}]}
            return {**body, "id": 51}

        self.fixture.route("POST", "/streams", create_flow)
        result = await self.provider.create_campaign(NEW_CAMPAIGN)
        self.assertEqual(result, {
            "id": 41,
            "name": "AdRobot example",
            "offers": [{"id": 11, "name": "Offer A", "weight": 100}],
        })
        expected_campaign = {
            "name": "AdRobot example",
            "domain_id": 21,
            "group_id": "31",
            "traffic_source_id": 71,
            "state": "disabled",
            "type": "position",
        }
        [campaign_write, redirect_write, flow_write] = self.fixture.writes()
        method, path, body = campaign_write
        self.assertEqual((method, path), ("POST", "/campaigns"))
        self.assertRegex(body.pop("alias"), r"^adrobot-[0-9a-f]{32}$")
        self.assertEqual(body, expected_campaign)
        self.assertEqual(
            redirect_write, ("POST", "/streams", {key: value for key, value in REDIRECT.items() if key != "id"})
        )
        self.assertEqual(
            flow_write,
            (
                "POST",
                "/streams",
                {
                    "campaign_id": 41,
                    "name": "AdRobot offers",
                    "comments": "adrobot:managed-offers:v1:41",
                    "schema": "landings",
                    "type": "default",
                    "position": 2,
                    "state": "active",
                    "action_type": "http",
                    "landings": [],
                    "filters": [],
                    "offers": [OFFER_1],
                },
            ),
        )

    async def test_unknown_offer_fails_before_any_write(self):
        with self.assertRaisesRegex(ProviderError, "selected offer is unavailable"):
            await self.provider.create_campaign({**NEW_CAMPAIGN, "offer_id": 999})
        self.assertEqual(self.fixture.writes(), [])

    async def test_partial_creation_reports_existing_campaign_without_retrying(self):
        self.fixture.route("POST", "/campaigns", CAMPAIGN)
        self.fixture.route("POST", "/streams", {"error": "private upstream text"}, status=400)
        with self.assertRaisesRegex(ProviderError, "campaign 41 was created disabled"):
            await self.provider.create_campaign(NEW_CAMPAIGN)
        self.assertEqual(
            [(method, path) for method, path, _ in self.fixture.writes()],
            [("POST", "/campaigns"), ("POST", "/streams")],
        )

    async def test_wrong_redirect_is_reported_before_creating_fallback(self):
        self.fixture.route("POST", "/campaigns", CAMPAIGN)
        self.fixture.route("POST", "/streams", {**REDIRECT, "action_payload": ""})
        with self.assertRaisesRegex(ProviderError, "campaign 41 was created disabled"):
            await self.provider.create_campaign(NEW_CAMPAIGN)
        self.assertEqual(
            [(method, path) for method, path, _ in self.fixture.writes()],
            [("POST", "/campaigns"), ("POST", "/streams")],
        )

    async def test_unconfirmed_fallback_reports_disabled_campaign(self):
        self.fixture.route("POST", "/campaigns", CAMPAIGN)
        self.fixture.route(
            "POST", "/streams",
            lambda body: REDIRECT if body["type"] == "regular" else {**body, "id": 51, "offers": []},
        )
        with self.assertRaisesRegex(ProviderError, "campaign 41 was created disabled"):
            await self.provider.create_campaign(NEW_CAMPAIGN)
        self.assertEqual(
            [(method, path) for method, path, _ in self.fixture.writes()],
            [("POST", "/campaigns"), ("POST", "/streams"), ("POST", "/streams")],
        )

    async def test_managed_allocation_changes_only_offers_and_ignores_existing_flow(self):
        other_flow = {**FLOW, "id": 61, "comments": "Another owner's flow"}
        self.fixture.route("GET", "/campaigns/41/streams", [REDIRECT, other_flow, FLOW])
        self.fixture.route("PUT", "/streams/51", lambda body: {**FLOW, **body})
        details = await self.provider.get_campaign(41)
        self.assertEqual(details["offers"], [{"id": 11, "name": "Offer A", "weight": 100}])
        self.assertNotIn("token", details)
        result = await self.provider.set_offers(41, [{"id": 11, "weight": 70}, {"id": 12, "weight": 30}])
        self.assertEqual(result["offers"], [
            {"id": 11, "name": "Offer A", "weight": 70},
            {"id": 12, "name": "Offer B", "weight": 30},
        ])
        self.assertEqual(
            self.fixture.writes(),
            [
                (
                    "PUT",
                    "/streams/51",
                    {"offers": [
                        {"offer_id": 11, "share": 70, "state": "active"},
                        {"offer_id": 12, "share": 30, "state": "active"},
                    ]},
                ),
            ],
        )

    async def test_legacy_managed_regular_offer_can_still_be_edited(self):
        old_flow = {**FLOW, "type": "regular", "filters": REDIRECT["filters"]}
        self.fixture.route("GET", "/campaigns/41/streams", [old_flow])
        self.fixture.route("PUT", "/streams/51", lambda body: {**old_flow, **body})
        result = await self.provider.set_offers(41, [{"id": 12, "weight": 100}])
        self.assertEqual(result["offers"], [{"id": 12, "name": "Offer B", "weight": 100}])

    async def test_unowned_and_ambiguous_flows_are_not_mutated(self):
        other_flow = {**FLOW, "id": 61, "comments": "Another owner's flow"}
        self.fixture.route("GET", "/campaigns/41/streams", [other_flow])
        with self.assertRaisesRegex(ProviderError, "no AdRobot-managed offer flow"):
            await self.provider.set_offers(41, [{"id": 11, "weight": 100}])
        self.fixture.route("GET", "/campaigns/41/streams", [FLOW, {**FLOW, "id": 52}])
        with self.assertRaisesRegex(ProviderError, "Multiple Keitaro flows"):
            await self.provider.set_offers(41, [{"id": 11, "weight": 100}])
        self.assertEqual(self.fixture.writes(), [])

    async def test_invalid_weight_or_disabled_offer_does_not_mutate(self):
        with self.assertRaisesRegex(ProviderError, "total 100 percent"):
            await self.provider.set_offers(41, [{"id": 11, "weight": 99}])
        with self.assertRaisesRegex(ProviderError, "selected offers are unavailable"):
            await self.provider.set_offers(41, [{"id": 13, "weight": 100}])
        self.assertEqual(self.fixture.writes(), [])

    async def test_editor_detects_external_removal_and_edits_the_selected_existing_stream(self):
        campaign = {**CAMPAIGN, "name": "test_ryabov_existing"}
        selected = {**FLOW, "comments": "Previously created in Keitaro", "offers": [dict(OFFER_1)]}
        sibling = {**FLOW, "id": 52, "comments": "Other offer stream", "offers": [dict(OFFER_1)]}
        self.fixture.route("GET", "/campaigns/41", campaign)
        self.fixture.route("GET", "/campaigns/41/streams", [REDIRECT, selected, sibling])

        def put_offers(body):
            selected.update(body)
            # Some adapters acknowledge a write without repeating the stream body.
            return {"success": True}

        self.fixture.route("PUT", "/streams/51", put_offers)
        with TemporaryDirectory() as directory:
            url = f"sqlite:///{directory}/editor.db"
            with patch.dict(os.environ, {"DATABASE_URL": url}):
                command.upgrade(Config("alembic.ini"), "head")
            app = create_app(Settings(database_url=url, keitaro_base_url=self.fixture.url, keitaro_api_key=API_KEY))
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                editor_url = "/api/campaigns/41/editor"
                publish_url = "/api/campaigns/41/flows/51/offers"
                loaded = (await client.get(editor_url)).json()
                assert [flow["editable"] for flow in loaded["flows"]] == [False, True, True]
                assert loaded["flows"][0]["offers"] == []
                assert "action_payload" not in loaded["flows"][0]
                original = loaded["flows"][1]

                selected["offers"] = [{"offer_id": 12, "share": 100, "state": "active"}]
                rejected = await client.put(publish_url, json={
                    "revision": original["revision"], "offers": [{"id": 11, "weight": 100}],
                })
                assert rejected.status_code == 409, rejected.text
                assert self.fixture.writes() == []

                fresh = (await client.get(editor_url)).json()["flows"][1]
                assert [(item["id"], item["archived"]) for item in fresh["offers"]] == [(12, False), (11, True)]
                saved = await client.put(publish_url, json={
                    "revision": fresh["revision"],
                    "offers": [{"id": 12, "weight": 25, "pinned": True}, {"id": 11, "weight": 75}],
                })
                assert saved.status_code == 200, saved.text
                assert saved.json()["offers"][0]["pinned"] is True
                assert sibling["offers"] == [OFFER_1]
                assert [(verb, path) for verb, path, _ in self.fixture.writes()] == [("PUT", "/streams/51")]

    async def test_auth_error_is_safe_and_redirect_is_rejected(self):
        self.fixture.route("GET", "/campaigns/41", {"secret": API_KEY}, status=401)
        with self.assertRaisesRegex(ProviderError, "denied access") as denied:
            await self.provider.get_campaign(41)
        self.assertNotIn(API_KEY, str(denied.exception))
        self.fixture.route("GET", "/campaigns/41", {}, status=302, headers={"Location": "/unexpected"})
        with self.assertRaisesRegex(ProviderError, "redirect"):
            await self.provider.get_campaign(41)
        self.assertNotIn("/unexpected", [path for _, path, _, _ in self.fixture.requests])

    async def test_timeout_is_reported_without_a_retry(self):
        respond = threading.Event()

        def slow_campaign(_):
            respond.wait(1)
            return CAMPAIGN

        self.fixture.route("GET", "/campaigns/41", slow_campaign)
        provider = KeitaroProvider(self.fixture.url, API_KEY, timeout=0.05)
        try:
            with self.assertRaisesRegex(ProviderError, "Keitaro timed out"):
                await provider.get_campaign(41)
        finally:
            respond.set()
        self.assertEqual(
            [path for _, path, _, _ in self.fixture.requests].count(API + "/campaigns/41"),
            1,
        )

    def test_plain_http_requires_loopback(self):
        with self.assertRaisesRegex(ProviderError, "must use HTTPS"):
            KeitaroProvider("http://nonlocal.example", API_KEY)


if __name__ == "__main__":
    unittest.main()
