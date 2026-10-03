"""Setup checks that run before any live step. Every probe is read-only or creates nothing:
write scopes are tested with calls that fail validation (400) or are empty batches, and a 403 or a
missing-scope error means the key or token was created without that permission."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from .config import Settings
from .hubspot import BASE

OK, WARN, FAIL = "ok", "warn", "fail"
SLACK_BOT_SCOPES = ("chat:write", "users:read", "users:read.email")


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


def _hs(s: Settings, method: str, path: str, body: Optional[dict] = None) -> requests.Response:
    return requests.request(method, BASE + path, json=body, timeout=30,
                            headers={"Authorization": f"Bearer {s.hubspot_key}"})


def _probe(name: str, resp: requests.Response, ok_codes: tuple, scope_hint: str) -> Check:
    if resp.status_code in ok_codes:
        return Check(name, OK, f"HTTP {resp.status_code}")
    if resp.status_code in (401, 403):
        return Check(name, FAIL, f"HTTP {resp.status_code}: key lacks {scope_hint}")
    return Check(name, FAIL, f"HTTP {resp.status_code}: {resp.text[:140]}")


def hubspot_checks(s: Settings) -> list:
    if not s.hubspot_key:
        return [Check("HUBSPOT_SERVICE_KEY", FAIL, "not set in .env")]
    out: list = []
    try:
        r = _hs(s, "GET", "/crm/v3/owners")
        out.append(_probe("read owners", r, (200,), "crm.objects.owners.read"))
        if r.status_code == 200:
            ids = {o["id"] for o in r.json().get("results", [])}
            out.append(Check("HUBSPOT_OWNER_ID", OK if s.hubspot_owner_id in ids else WARN,
                             "matches an owner" if s.hubspot_owner_id in ids else
                             "unset or not an owner id; the first owner will be used"))
        out.append(_probe("read deal properties", _hs(s, "GET", "/crm/v3/properties/deals"), (200,),
                          "crm.schemas.deals.read"))
        out.append(_probe("read deals", _hs(s, "GET", "/crm/v3/objects/deals?limit=1"), (200,),
                          "crm.objects.deals.read"))
        out.append(_probe("read companies", _hs(s, "GET", "/crm/v3/objects/companies?limit=1"), (200,),
                          "crm.objects.companies.read"))
        # write probes: invalid or empty bodies, so nothing is created
        out.append(_probe("write property groups", _hs(s, "POST", "/crm/v3/properties/deals/groups", {}),
                          (400, 409, 422), "crm.schemas.deals.write"))
        out.append(_probe("write deal properties", _hs(s, "POST", "/crm/v3/properties/deals", {}),
                          (400, 409, 422), "crm.schemas.deals.write"))
        out.append(_probe("write companies", _hs(s, "POST", "/crm/v3/objects/companies/batch/create",
                                                  {"inputs": []}), (200, 201, 400), "crm.objects.companies.write"))
        out.append(_probe("write deals", _hs(s, "POST", "/crm/v3/objects/deals/batch/create", {"inputs": []}),
                          (200, 201, 400), "crm.objects.deals.write"))
    except requests.RequestException as e:
        out.append(Check("HubSpot reachable", FAIL, type(e).__name__))
    return out


def slack_checks(s: Settings) -> list:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    out: list = []
    for key in ("slack_bot_token", "slack_app_token", "slack_channel", "slack_reviewer_ids"):
        if not getattr(s, key):
            out.append(Check(key.upper(), FAIL, "not set in .env"))
    if s.slack_bot_token:
        try:
            client = WebClient(token=s.slack_bot_token)
            res = client.auth_test()
            have = set((res.headers.get("x-oauth-scopes") or "").replace(" ", "").split(","))
            miss = [x for x in SLACK_BOT_SCOPES if x not in have]
            out.append(Check("bot token", OK, f"authenticated as {res['user']}"))
            out.append(Check("bot scopes", FAIL if miss else OK, f"missing: {', '.join(miss)}" if miss else
                             "chat:write, users:read, users:read.email present"))
            if s.slack_channel.startswith("C"):
                try:
                    info = client.conversations_info(channel=s.slack_channel)
                    out.append(Check("review channel", OK if info["channel"].get("is_member") else FAIL,
                                     "bot is a member" if info["channel"].get("is_member")
                                     else "bot is not in the channel: /invite the app there"))
                except SlackApiError as e:
                    err = e.response.get("error", "")
                    out.append(Check("review channel", WARN, "cannot verify membership "
                                     f"({err}); a post will fail with not_in_channel if the bot was not invited"))
        except SlackApiError as e:
            out.append(Check("bot token", FAIL, e.response.get("error", "error")))
    if s.slack_app_token:
        try:
            WebClient().apps_connections_open(app_token=s.slack_app_token)
            out.append(Check("app token (Socket Mode)", OK, "connections:write works"))
        except SlackApiError as e:
            out.append(Check("app token (Socket Mode)", FAIL, e.response.get("error", "error")))
    return out


def openai_checks(s: Settings) -> list:
    out: list = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        out.append(Check("ANTHROPIC_API_KEY", FAIL, "is set: Claude Code would bill the API, not the subscription"))
    else:
        out.append(Check("ANTHROPIC_API_KEY", OK, "unset"))
    if not s.openai_api_key:
        return out + [Check("OPENAI_API_KEY", FAIL, "not set in .env")]
    from .llm import model_available
    for label, model in (("extractor model", s.extractor_model), ("learner model", s.learner_model)):
        if not model:
            out.append(Check(label, FAIL, "not set in .env"))
            continue
        try:
            model_available(s, model)
            out.append(Check(label, OK, model))
        except Exception as e:
            out.append(Check(label, FAIL, f"{model}: {type(e).__name__}"))
    priced = all(s.price(m) != (0.0, 0.0) for m in (s.extractor_model, s.learner_model))
    out.append(Check("prices", OK if priced else WARN, "set" if priced else "unset: spend will log as $0"))
    return out


GROUPS: dict = {"hubspot": hubspot_checks, "slack": slack_checks, "openai": openai_checks}


def run(s: Settings, groups: list) -> dict:
    return {g: GROUPS[g](s) for g in groups}


def passed(results: dict) -> bool:
    return all(c.status != FAIL for checks in results.values() for c in checks)


def render(results: dict) -> str:
    mark = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
    lines = []
    for g, checks in results.items():
        lines.append(f"[{g}]")
        lines += [f"  {mark[c.status]} {c.name}: {c.detail}" for c in checks]
    return "\n".join(lines)
