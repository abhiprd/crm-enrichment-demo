"""Slack Socket Mode wiring for the review card. Needs --live and Slack tokens; logic lives in review.py.

Each handler opens its own SQLite connection, so the watcher thread and Slack threads never share one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import threading

from . import db, learning
from .config import Settings
from .paths import Paths
from .review import (card_blocks, edit_modal, get_proposal, handle_decision, modal_values, proposals_for,
                     reject_modal, reviewers_for)


def refresh_card(client, conn, channel: str, ts: str, interaction_id: str) -> None:
    client.chat_update(channel=channel, ts=ts, blocks=card_blocks(proposals_for(conn, interaction_id)),
                       text="Proposed CRM updates")


def build_app(settings: Settings, db_path: Path, hs, paths: Optional[Paths] = None) -> App:
    app = App(token=settings.slack_bot_token)

    def connect():
        return db.connect(db_path)

    def tell(client, channel: str, user: str, text: str) -> None:
        client.chat_postEphemeral(channel=channel, user=user, text=text)

    def locate(body: dict) -> tuple:
        return body["channel"]["id"], body["message"]["ts"]

    def can_act(row, user: str) -> bool:
        allowed = reviewers_for(row)
        return not allowed or user in allowed

    def on_button(decision: str):
        def _handler(ack, body, client):
            ack()
            conn = connect()
            pid, user = int(body["actions"][0]["value"]), body["user"]["id"]
            channel, ts = locate(body)
            row = get_proposal(conn, pid)
            if not can_act(row, user):
                return tell(client, channel, user, "only the owner can review this")
            out = handle_decision(conn, hs, pid, decision, user)
            if out.status in ("unauthorized", "not_pending", "error"):
                return tell(client, channel, user, out.message)
            if out.status in ("stale", "resolved"):
                tell(client, channel, user, out.message)
            refresh_card(client, conn, channel, ts, row["interaction_id"])
        return _handler

    def open_modal(builder: Callable):
        def _handler(ack, body, client):
            ack()
            conn = connect()
            pid, user = int(body["actions"][0]["value"]), body["user"]["id"]
            channel, ts = locate(body)
            row = get_proposal(conn, pid)
            if not can_act(row, user):
                return tell(client, channel, user, "only the owner can review this")
            if row["review_status"] != "pending":
                return tell(client, channel, user, f"already {row['review_status']}")
            client.views_open(trigger_id=body["trigger_id"], view=builder(row, channel, ts))
        return _handler

    def on_submit(decision: str):
        def _handler(ack, body, client, view):
            ack()
            conn = connect()
            v, user = modal_values(view), body["user"]["id"]
            row = get_proposal(conn, int(v["pid"]))
            out = handle_decision(conn, hs, int(v["pid"]), decision, user, reason=v["reason"], note=v["note"],
                                  final_value=v["text"])
            if out.status in ("unauthorized", "not_pending", "error", "stale", "resolved"):
                tell(client, v["channel"], user, out.message)
            if out.status not in ("unauthorized", "error"):
                refresh_card(client, conn, v["channel"], v["ts"], row["interaction_id"])
            if decision == "reject" and out.status == "rejected" and paths is not None:
                def say(text: str) -> None:
                    client.chat_postMessage(channel=v["channel"], thread_ts=v["ts"], text=text)
                threading.Thread(target=learning.learn_and_apply, args=(settings, paths, int(v["pid"]), say),
                                 daemon=True).start()
        return _handler

    app.action("approve")(on_button("approve"))
    app.action("edit")(open_modal(edit_modal))
    app.action("reject")(open_modal(reject_modal))
    app.view("edit_submit")(on_submit("edit"))
    app.view("reject_submit")(on_submit("reject"))
    return app


def post_card(settings: Settings, conn, interaction_id: str, client=None) -> str:
    """Post one card for every proposal of an interaction; store the message ts on each. Returns the ts."""
    from slack_sdk import WebClient
    client = client or WebClient(token=settings.slack_bot_token)
    rows = proposals_for(conn, interaction_id)
    res = client.chat_postMessage(channel=settings.slack_channel, blocks=card_blocks(rows),
                                  text="Proposed CRM updates")
    conn.execute("UPDATE proposals SET slack_ts=?, slack_channel=? WHERE interaction_id=?",
                 (res["ts"], res["channel"], interaction_id))
    conn.commit()
    return res["ts"]


def serve(settings: Settings, db_path: Path, hs, paths: Optional[Paths] = None) -> None:
    """Block and handle clicks (used by `crm slice --live`)."""
    SocketModeHandler(build_app(settings, db_path, hs, paths), settings.slack_app_token).start()


def start_background(settings: Settings, db_path: Path, hs, paths: Optional[Paths] = None) -> SocketModeHandler:
    """Connect Socket Mode in a background thread so the inbox watcher can run in the same process."""
    handler = SocketModeHandler(build_app(settings, db_path, hs, paths), settings.slack_app_token)
    handler.connect()
    return handler
