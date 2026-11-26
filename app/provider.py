"""Adapter for the documented Keitaro Admin API v1.

The caller must configure a dedicated test tracker. Campaigns are created
disabled. Flow ownership is recorded in its comments and survives restarts.
"""

import asyncio
import json
import math
import re
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from app.editor import StaleFlowError, offer_revision


class ProviderError(RuntimeError):
    """A message safe to return to the caller."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_PREFIX = "/admin_api/v1"
_FLOW_MARKER = "adrobot:managed-offers:v1"
_COUNTRY_REDIRECT_URL = "https://www.google.com/"
_PAGE_SIZE = 100
_MAX_BODY = 8 * 1024 * 1024


class KeitaroProvider:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 10.0, editor_prefix: str = ""):
        if not isinstance(api_key, str) or not re.fullmatch(r"[!-~]+", api_key):
            raise ProviderError("A valid tracker API key is required.")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ProviderError("The tracker timeout must be a positive number.")
        self._base_url = _admin_url(base_url)
        self._api_key = api_key
        self._timeout = timeout
        self._editor_prefix = editor_prefix

    async def catalog(self) -> dict:
        campaigns, offers, domains, groups, sources = await asyncio.gather(
            self._campaigns(),
            self._get_list("/offers"),
            self._get_list("/domains"),
            self._get_list("/groups?type=campaigns"),
            self._get_list("/traffic_sources"),
        )
        return {
            "campaigns": [_identity(item) for item in campaigns],
            "offers": [_identity(item) for item in offers if _is_active(item)],
            "domains": [_identity(item) for item in domains],
            "groups": [_identity(item) for item in groups],
            "sources": [_identity(item) for item in sources],
        }

    async def get_campaign(self, campaign_id: int) -> dict:
        campaign_id = _input_id(campaign_id, "Campaign ID")
        campaign, flows = await self._campaign_and_flows(campaign_id)
        flow = _managed_flow(campaign_id, flows)
        if flow is None:
            offer_flows = [item for item in flows if _is_active(item) and _offers(item)]
            if len(offer_flows) > 1:
                raise ProviderError("This campaign has multiple unmanaged offer flows.")
            flow = next(iter(offer_flows), None)
        shares = _offers(flow) if flow is not None else []
        names = await self._offer_names() if shares else {}
        return _campaign_view(campaign, shares, names)

    async def get_editor_campaign(self, campaign_id: int) -> dict:
        campaign, flows = await self._editor_campaign(campaign_id)
        names = await self._offer_names() if any(_accepts_offers(flow) for flow in flows) else {}
        return {
            **_identity(campaign),
            "flows": [_editor_flow(campaign_id, flow, names) for flow in flows if flow.get("state") != "deleted"],
        }

    async def set_editor_offers(
        self, campaign_id: int, flow_id: int, offers: list[dict], revision: str
    ) -> dict:
        shares = _input_allocation(offers)
        _, flows = await self._editor_campaign(campaign_id)
        flow = _flow_by_id(campaign_id, flow_id, flows)
        current = _offers(flow)
        if revision != offer_revision(current):
            raise StaleFlowError()
        names = await self._offer_names(active_only=True)
        if any(offer_id not in names for offer_id, _ in shares):
            raise ProviderError("One or more selected offers are unavailable.")
        if sorted(current) == sorted(shares):
            return _editor_flow(campaign_id, flow, names)

        result = await self._request("PUT", f"/streams/{flow_id}", {"offers": _offer_request(shares)})
        if isinstance(result, dict) and result.get("id") is not None:
            try:
                confirmed = _flow_by_id(campaign_id, flow_id, [result])
                if sorted(_offers(confirmed)) == sorted(shares):
                    return _editor_flow(campaign_id, confirmed, names)
            except (KeyError, ProviderError):
                pass
        _, flows = await self._editor_campaign(campaign_id)
        confirmed = _flow_by_id(campaign_id, flow_id, flows)
        if sorted(_offers(confirmed)) != sorted(shares):
            raise ProviderError("Keitaro did not confirm the offer allocation. Refresh the streams before retrying.")
        return _editor_flow(campaign_id, confirmed, names)

    async def _editor_campaign(self, campaign_id: int) -> tuple[dict, list[dict]]:
        campaign_id = _input_id(campaign_id, "Campaign ID")
        campaign, flows = await self._campaign_and_flows(campaign_id)
        if self._editor_prefix and not _identity(campaign)["name"].startswith(self._editor_prefix):
            raise KeyError(campaign_id)
        return campaign, flows

    async def create_campaign(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ProviderError("Campaign details are required.")
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProviderError("A campaign name is required.")
        country = payload.get("country")
        if not isinstance(country, str) or not re.fullmatch(r"[A-Za-z]{2}", country.strip()):
            raise ProviderError("Country must be a two-letter country code.")
        country = country.strip().upper()
        offer_id = _input_id(payload.get("offer_id"), "Offer ID")
        domain_id = _input_id(payload.get("domain_id"), "Domain ID")
        group_id = _input_id(payload.get("group_id"), "Group ID")
        source_id = _input_id(payload.get("source_id"), "Traffic source ID")
        names = await self._offer_names(active_only=True)
        if offer_id not in names:
            raise ProviderError("The selected offer is unavailable.")

        created = await self._request(
            "POST",
            "/campaigns",
            {
                "name": name.strip(),
                "alias": f"adrobot-{uuid4().hex}",
                "state": "disabled",
                "type": "position",
                "domain_id": domain_id,
                "group_id": str(group_id),
                "traffic_source_id": source_id,
            },
        )
        try:
            campaign = _object(created)
            campaign_id = _identity(campaign)["id"]
        except ProviderError:
            raise ProviderError("Keitaro returned no usable campaign ID. Verify creation before retrying.") from None
        if campaign.get("state") != "disabled":
            raise ProviderError(f"Verify that Keitaro campaign {campaign_id} is disabled before retrying.")

        shares = [(offer_id, 100)]
        try:
            redirect_flow = _object(
                await self._request(
                    "POST",
                    "/streams",
                    {
                        "campaign_id": campaign_id,
                        "name": "AdRobot country redirect",
                        "schema": "redirect",
                        "type": "regular",
                        "position": 1,
                        "state": "active",
                        "action_type": "http",
                        "action_payload": _COUNTRY_REDIRECT_URL,
                        "filters": [{"name": "country", "mode": "accept", "payload": [country]}],
                    },
                )
            )
            _confirm_country_redirect(campaign_id, country, redirect_flow)
            flow = _object(
                await self._request(
                    "POST",
                    "/streams",
                    {
                        "campaign_id": campaign_id,
                        "name": "AdRobot offers",
                        "comments": _marker(campaign_id),
                        "schema": "landings",
                        "type": "default",
                        "position": 2,
                        "state": "active",
                        "action_type": "http",
                        "landings": [],
                        "filters": [],
                        "offers": _offer_request(shares),
                    },
                )
            )
            if flow.get("type") != "default" or flow.get("state") != "active" or flow.get("filters"):
                raise ProviderError("Keitaro did not confirm an active, unfiltered fallback.")
            _confirm_allocation(campaign_id, [flow], shares)
        except ProviderError:
            raise ProviderError(
                f"Keitaro campaign {campaign_id} was created disabled. "
                "Verify its country redirect and fallback in the tracker before retrying."
            ) from None
        return _campaign_view(campaign, shares, names)

    async def set_offers(self, campaign_id: int, offers: list[dict]) -> dict:
        campaign_id = _input_id(campaign_id, "Campaign ID")
        shares = _input_allocation(offers)
        (campaign, flows), names = await asyncio.gather(
            self._campaign_and_flows(campaign_id),
            self._offer_names(active_only=True),
        )
        if any(offer_id not in names for offer_id, _ in shares):
            raise ProviderError("One or more selected offers are unavailable.")
        flow = _managed_flow(campaign_id, flows)
        if flow is None:
            raise ProviderError("This campaign has no AdRobot-managed offer flow.")
        flow_id = _response_id(flow.get("id"))
        if sorted(_offers(flow)) != sorted(shares):
            updated = _object(
                await self._request("PUT", f"/streams/{flow_id}", {"offers": _offer_request(shares)})
            )
            _confirm_allocation(campaign_id, [updated], shares, flow_id)
        return _campaign_view(campaign, shares, names)

    async def _campaigns(self) -> list[dict]:
        campaigns = []
        campaign_ids = set()
        while True:
            page = await self._get_list(f"/campaigns?offset={len(campaigns)}&limit={_PAGE_SIZE}")
            for campaign in page:
                campaign_id = _response_id(campaign.get("id"))
                if campaign_id in campaign_ids:
                    raise ProviderError("Keitaro returned duplicate campaign IDs while paging.")
                campaign_ids.add(campaign_id)
                campaigns.append(campaign)
            if len(page) < _PAGE_SIZE:
                return campaigns

    async def _campaign_and_flows(self, campaign_id: int) -> tuple[dict, list[dict]]:
        raw_campaign, flows = await asyncio.gather(
            self._request("GET", f"/campaigns/{campaign_id}"),
            self._get_list(f"/campaigns/{campaign_id}/streams"),
        )
        campaign = _object(raw_campaign)
        if _identity(campaign)["id"] != campaign_id:
            raise ProviderError("Keitaro returned a different campaign.")
        return campaign, flows

    async def _offer_names(self, *, active_only: bool = False) -> dict[int, str]:
        names = {}
        for item in await self._get_list("/offers"):
            if not active_only or _is_active(item):
                offer = _identity(item)
                names[offer["id"]] = offer["name"]
        return names

    async def _get_list(self, path: str) -> list[dict]:
        response = await self._request("GET", path)
        if not isinstance(response, list):
            raise ProviderError("Keitaro returned an unexpected list response.")
        return [_object(item) for item in response]

    async def _request(self, method: str, path: str, body: dict | None = None):
        return await asyncio.to_thread(self._send, method, path, body)

    def _send(self, method: str, path: str, body: dict | None):
        headers = {
            "Api-Key": self._api_key,
            "Accept": "application/json",
            "User-Agent": "Adrobot/0.1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            self._base_url + path,
            data=json.dumps(body).encode("ascii") if body is not None else None,
            method=method,
            headers=headers,
        )
        opener = build_opener(_NoRedirect(), ProxyHandler({}))
        try:
            with opener.open(request, timeout=self._timeout) as response:
                raw = response.read(_MAX_BODY + 1)
        except HTTPError as error:
            error.close()
            message = _http_error(error.code)
            if method != "GET" and error.code >= 500:
                message += " Verify tracker state before retrying."
            raise ProviderError(message) from None
        except (TimeoutError, URLError, OSError) as error:
            timeout = isinstance(error, TimeoutError) or (
                isinstance(error, URLError) and isinstance(error.reason, TimeoutError)
            )
            message = "Keitaro timed out." if timeout else "Keitaro could not be reached."
            if method != "GET":
                message += " Verify tracker state before retrying."
            raise ProviderError(message) from None
        except HTTPException:
            message = "Keitaro returned an incomplete HTTP response."
            if method != "GET":
                message += " Verify tracker state before retrying."
            raise ProviderError(message) from None
        if len(raw) > _MAX_BODY:
            raise ProviderError("Keitaro returned an oversized response.")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            raise ProviderError("Keitaro returned invalid JSON.") from None


def _admin_url(value: str) -> str:
    if not isinstance(value, str) or any(character.isspace() for character in value):
        raise ProviderError("A valid tracker base URL is required.")
    if "://" not in value:
        value = "https://" + value
    try:
        url = urlsplit(value)
        invalid = (
            not url.hostname
            or not re.fullmatch(r"[A-Za-z0-9.:-]+", url.hostname)
            or url.username is not None
            or url.password is not None
            or url.port == 0
            or bool(url.query)
            or bool(url.fragment)
        )
    except ValueError:
        raise ProviderError("A valid tracker base URL is required.") from None
    if invalid:
        raise ProviderError("A valid tracker base URL is required.")
    local_http = url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    if url.scheme != "https" and not local_http:
        raise ProviderError("The tracker base URL must use HTTPS.")
    path = url.path.rstrip("/")
    if not path.endswith(_PREFIX):
        path += _PREFIX
    return urlunsplit((url.scheme, url.netloc, path, "", ""))


def _http_error(code: int) -> str:
    if code in (401, 403):
        return "Keitaro denied access. Check the API key and its permissions."
    if code == 402:
        return "This Keitaro edition does not provide Admin API access."
    if code == 404:
        return "Keitaro could not find the requested resource."
    if code == 429:
        return "Keitaro rate limit reached."
    if 300 <= code < 400:
        return "Keitaro returned a redirect. Check the configured API URL."
    return f"Keitaro rejected the request (HTTP {code})."


def _object(value) -> dict:
    if not isinstance(value, dict):
        raise ProviderError("Keitaro returned an unexpected object response.")
    return value


def _identity(item: dict) -> dict:
    name = item.get("name")
    if not isinstance(name, str):
        raise ProviderError("Keitaro returned an entity without a usable name.")
    return {"id": _response_id(item.get("id")), "name": name}


def _input_id(value, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ProviderError(f"{label} must be a positive integer.")
    return value


def _response_id(value) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,18}", value):
        value = int(value)
    if type(value) is not int or value <= 0:
        raise ProviderError("Keitaro returned an invalid resource ID.")
    return value


def _marker(campaign_id: int) -> str:
    return f"{_FLOW_MARKER}:{campaign_id}"


def _is_active(item: dict) -> bool:
    return item.get("state", "active") == "active"


def _managed_flow(campaign_id: int, flows: list[dict]) -> dict | None:
    matches = [flow for flow in flows if flow.get("comments") == _marker(campaign_id)]
    if len(matches) > 1:
        raise ProviderError("Multiple Keitaro flows have this campaign's AdRobot marker.")
    if not matches:
        return None
    flow = matches[0]
    if (
        _response_id(flow.get("campaign_id")) != campaign_id
        or flow.get("schema") != "landings"
        or flow.get("type") not in {"regular", "default"}
        or flow.get("state") == "deleted"
    ):
        raise ProviderError("The AdRobot-marked Keitaro flow is incompatible.")
    return flow


def _offers(flow: dict) -> list[tuple[int, int]]:
    entries = flow.get("offers", [])
    if not isinstance(entries, list):
        raise ProviderError("Keitaro returned invalid offers for a campaign flow.")
    shares = []
    for entry in entries:
        entry = _object(entry)
        if _is_active(entry):
            offer_id = _response_id(entry.get("offer_id"))
            weight = entry.get("share")
            if type(weight) is not int or not 0 <= weight <= 100:
                raise ProviderError("Keitaro returned an invalid offer weight.")
            shares.append((offer_id, weight))
    return shares


def _accepts_offers(flow: dict) -> bool:
    return flow.get("state") != "deleted" and flow.get("schema") == "landings"


def _flow_by_id(campaign_id: int, flow_id: int, flows: list[dict]) -> dict:
    for flow in flows:
        if _response_id(flow.get("id")) == flow_id and _accepts_offers(flow):
            if _response_id(flow.get("campaign_id")) == campaign_id:
                return flow
            break
    raise KeyError(flow_id)


def _editor_flow(campaign_id: int, flow: dict, names: dict[int, str]) -> dict:
    flow_id = _response_id(flow.get("id"))
    accepts_offers = _accepts_offers(flow)
    view = {
        "id": flow_id,
        "name": flow.get("name") or f"Поток #{flow_id}",
        "accepts_offers": accepts_offers,
        "managed": flow.get("comments") == _marker(campaign_id),
    }
    if accepts_offers:
        shares = _offers(flow)
        view["revision"] = offer_revision(shares)
        view["offers"] = _offer_rows(shares, names)
    return view


def _input_allocation(offers: list[dict]) -> list[tuple[int, int]]:
    if not isinstance(offers, list) or not offers:
        raise ProviderError("At least one offer is required.")
    shares = []
    offer_ids = set()
    for offer in offers:
        if not isinstance(offer, dict):
            raise ProviderError("Offer details must include an ID and weight.")
        offer_id = _input_id(offer.get("id"), "Offer ID")
        weight = offer.get("weight")
        if type(weight) is not int or not 1 <= weight <= 100:
            raise ProviderError("Offer weights must be integer percentages between 1 and 100.")
        if offer_id in offer_ids:
            raise ProviderError("Each offer can appear only once.")
        offer_ids.add(offer_id)
        shares.append((offer_id, weight))
    if sum(weight for _, weight in shares) != 100:
        raise ProviderError("Offer weights must total 100 percent.")
    return shares


def _offer_request(shares: list[tuple[int, int]]) -> list[dict]:
    return [{"offer_id": offer_id, "share": weight, "state": "active"} for offer_id, weight in shares]


def _confirm_country_redirect(campaign_id: int, country: str, flow: dict) -> None:
    _response_id(flow.get("id"))
    filters = flow.get("filters")
    if (
        _response_id(flow.get("campaign_id")) != campaign_id
        or flow.get("type") != "regular"
        or flow.get("state") != "active"
        or flow.get("schema") != "redirect"
        or flow.get("action_type") != "http"
        or flow.get("action_payload") != _COUNTRY_REDIRECT_URL
        or not isinstance(filters, list)
        or len(filters) != 1
    ):
        raise ProviderError("Keitaro did not confirm the country redirect.")
    rule = _object(filters[0])
    payload = rule.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = [payload]
    if rule.get("name") != "country" or rule.get("mode") != "accept" or payload != [country]:
        raise ProviderError("Keitaro did not confirm the country redirect filter.")


def _confirm_allocation(
    campaign_id: int, flows: list[dict], expected: list[tuple[int, int]], flow_id: int | None = None
) -> None:
    flow = _managed_flow(campaign_id, flows)
    if flow is not None and sorted(_offers(flow)) == sorted(expected):
        returned_id = _response_id(flow.get("id"))
        if flow_id is None or returned_id == flow_id:
            return
    raise ProviderError("Keitaro did not confirm the offer allocation. Verify tracker state before retrying.")


def _campaign_view(campaign: dict, shares: list[tuple[int, int]], names: dict[int, str]) -> dict:
    view = _identity(campaign)
    view["offers"] = _offer_rows(shares, names)
    return view


def _offer_rows(shares: list[tuple[int, int]], names: dict[int, str]) -> list[dict]:
    return [
        {"id": offer_id, "name": names.get(offer_id, f"Offer #{offer_id}"), "weight": weight}
        for offer_id, weight in shares
    ]
