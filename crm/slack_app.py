"""Slack Socket Mode wiring for the review card. Needs --live and Slack tokens; logic lives in review.py."""

from __future__ import annotations

import sqlite3

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .config import Settings
from .review import Outcome, card_blocks, get_proposal, handle_decision


def build_app(settings: Settings, conn: sqlite3.Connection, hs) -> App:
    app = App(token=settings.slack_bot_token)

    def refresh(body: dict, client, pid: int) -> None:
        row = get_proposal(conn, pid)
        msg = body.get("message", {})
        client.chat_update(channel=body["channel"]["id"], ts=msg["ts"], blocks=card_blocks(row),
                           text="Proposed CRM update")

    def decide(decision: str):
        def _handler(ack, body, client, respond):
            ack()
            pid = int(body["actions"][0]["value"])
            out: Outcome = handle_decision(conn, hs, pid, decision, body["user"]["id"],
                                           settings.slack_reviewer_ids,
                                           reason="wrong_value")
            if out.status in ("unauthorized", "not_pending", "error"):
                respond(text=out.message, response_type="ephemeral", replace_original=False)
                return
            if out.status == "stale":
                respond(text=out.message, response_type="ephemeral", replace_original=False)
            refresh(body, client, pid)
        return _handler

    app.action("approve")(decide("approve"))
    app.action("reject")(decide("reject"))
    return app


def post_card(settings: Settings, conn: sqlite3.Connection, pid: int) -> None:
    from slack_sdk import WebClient
    client = WebClient(token=settings.slack_bot_token)
    row = get_proposal(conn, pid)
    res = client.chat_postMessage(channel=settings.slack_channel, blocks=card_blocks(row), text="Proposed CRM update")
    conn.execute("UPDATE proposals SET slack_ts=?, slack_channel=? WHERE id=?", (res["ts"], res["channel"], pid))
    conn.commit()


def serve(settings: Settings, conn: sqlite3.Connection, hs) -> None:
    SocketModeHandler(build_app(settings, conn, hs), settings.slack_app_token).start()
