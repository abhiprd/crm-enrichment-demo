"""Thin HubSpot REST client (service key, bearer auth) and the deal-field mapping.

Dry-run by default: reads need a key, but writes are only recorded in `planned` unless live=True.
Endpoints and property names follow HubSpot's CRM v3 docs and are unverified until the M0 live run.
"""

from __future__ import annotations

from typing import Any, Optional

import requests

BASE = "https://api.hubapi.com"
GROUP = {"name": "ai_extracted", "label": "AI-extracted"}
DEAL_TO_COMPANY = 5  # HUBSPOT_DEFINED association type id, deal -> company

# deal field -> HubSpot deal property. amount and hs_next_step are native; the rest are custom text.
PROPERTY = {
    "budget": "amount",
    "decision_timeline": "ai_decision_timeline",
    "competitors": "ai_competitors",
    "economic_buyer": "ai_economic_buyer",
    "champion": "ai_champion",
    "pain_points": "ai_pain_points",
    "use_case": "ai_use_case",
    "next_step": "hs_next_step",
}
CUSTOM_PROPERTIES = [
    {"name": n, "label": l, "type": "string", "fieldType": "text", "groupName": GROUP["name"]}
    for n, l in [
        ("ai_decision_timeline", "Decision timeline"), ("ai_competitors", "Competitors"),
        ("ai_economic_buyer", "Economic buyer"), ("ai_champion", "Champion"),
        ("ai_pain_points", "Pain points"), ("ai_use_case", "Use case"),
    ]
]


def fmt_value(field: str, value: Any) -> str:
    """Render a field value as the text stored in HubSpot. None and empty render as ''."""
    if value in (None, [], ""):
        return ""
    if field == "budget":
        return str(value["max"]) if isinstance(value, dict) else str(value)
    if field == "competitors":
        return "; ".join(f"{c['name']} ({c['stance']})" for c in value)
    if field in ("pain_points", "use_case"):
        return "; ".join(value)
    if field == "next_step":
        when = value.get("date") or "no date"
        return f"{value['action']} | owner: {value['owner']} | by {when}"
    return str(value)


def props_from_fields(before: dict) -> dict:
    """crm_before values (field -> value) to HubSpot deal properties."""
    return {PROPERTY[f]: fmt_value(f, v) for f, v in before.items() if f in PROPERTY}


def deal_url(portal_id, deal_id: str, ui_domain: str = "app.hubspot.com") -> str:
    """Link to the deal record in the HubSpot UI (0-3 is the deal object type)."""
    return f"https://{ui_domain}/contacts/{portal_id}/record/0-3/{deal_id}"


class HubSpotError(RuntimeError):
    pass


class HubSpot:
    def __init__(self, token: str, live: bool = False, session: Optional[requests.Session] = None):
        self.token, self.live = token, live
        self.session = session or requests.Session()
        self.planned: list = []

    def call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        if method != "GET" and not self.live:
            self.planned.append((method, path, body))
            return {"dry_run": True}
        if not self.token:
            raise HubSpotError("HUBSPOT_SERVICE_KEY is not set")
        resp = self.session.request(method, BASE + path, json=body, timeout=30,
                                    headers={"Authorization": f"Bearer {self.token}"})
        if resp.status_code >= 300:
            raise HubSpotError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}

    # --- schema ---------------------------------------------------------------------------
    def _existing(self, path: str) -> set:
        if not self.token:  # dry-run without a key: assume nothing exists, plan everything
            return set()
        return {x["name"] for x in self.call("GET", path).get("results", [])}

    def ensure_properties(self) -> None:
        if GROUP["name"] not in self._existing("/crm/v3/properties/deals/groups"):
            self.call("POST", "/crm/v3/properties/deals/groups", {**GROUP, "displayOrder": -1})
        have = self._existing("/crm/v3/properties/deals")
        for prop in CUSTOM_PROPERTIES:
            if prop["name"] not in have:
                self.call("POST", "/crm/v3/properties/deals", prop)

    def account(self) -> dict:
        """portalId and uiDomain, needed to build record links. Empty if the key cannot read account info."""
        try:
            return self.call("GET", "/account-info/v3/details")
        except HubSpotError:
            return {}

    def default_owner_id(self) -> str:
        owners = self.call("GET", "/crm/v3/owners").get("results", [])
        return owners[0]["id"] if owners else ""

    # --- records --------------------------------------------------------------------------
    def get_deal(self, deal_id: str, props: list) -> dict:
        qs = ",".join(props)
        return self.call("GET", f"/crm/v3/objects/deals/{deal_id}?properties={qs}").get("properties", {})

    def patch_deal(self, deal_id: str, props: dict) -> dict:
        return self.call("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": props})

    def create_companies(self, companies: list) -> dict:
        """companies: [{name, domain}] -> {domain: company_id}."""
        inputs = [{"properties": {"name": c["name"], "domain": c["domain"]}} for c in companies]
        res = self.call("POST", "/crm/v3/objects/companies/batch/create", {"inputs": inputs})
        return {r["properties"]["domain"]: r["id"] for r in res.get("results", [])}

    def create_deals(self, deals: list) -> dict:
        """deals: [{name, props, company_id, owner_id}] -> {dealname: deal_id}."""
        inputs = []
        for d in deals:
            props = {"dealname": d["name"], "pipeline": "default", "dealstage": "appointmentscheduled", **d["props"]}
            if d.get("owner_id"):
                props["hubspot_owner_id"] = d["owner_id"]
            inputs.append({"properties": props, "associations": [{
                "to": {"id": d["company_id"]},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": DEAL_TO_COMPANY}]}]})
        res = self.call("POST", "/crm/v3/objects/deals/batch/create", {"inputs": inputs})
        return {r["properties"]["dealname"]: r["id"] for r in res.get("results", [])}
