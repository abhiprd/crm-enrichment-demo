"""Settings from the environment (.env is loaded if present). Nothing here ever prints a secret."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional for offline commands
    load_dotenv = None


def load_env(root: Path) -> None:
    if load_dotenv is not None and (root / ".env").exists():
        load_dotenv(root / ".env", override=False)


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = ""
    extractor_model: str = ""
    extractor_effort: str = ""  # none | low | medium; empty means the provider default
    learner_model: str = ""
    prices: tuple = ()  # ((model, usd_per_mtok_in, usd_per_mtok_out), ...) from the provider's price page
    hubspot_key: str = ""
    hubspot_owner_id: str = ""
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_channel: str = ""
    slack_reviewer_ids: tuple = ()
    slack_manager_ids: tuple = ()

    @classmethod
    def load(cls) -> "Settings":
        ids = tuple(x.strip() for x in _get("SLACK_REVIEWER_IDS").split(",") if x.strip())
        managers = tuple(x.strip() for x in _get("SLACK_MANAGER_IDS").split(",") if x.strip())
        ext, lrn = _get("OPENAI_EXTRACTOR_MODEL"), _get("OPENAI_LEARNER_MODEL")
        prices = tuple(
            (m, float(_get(f"{role}_PRICE_IN_PER_MTOK", "0") or 0), float(_get(f"{role}_PRICE_OUT_PER_MTOK", "0") or 0))
            for role, m in (("EXTRACTOR", ext), ("LEARNER", lrn)) if m)
        return cls(
            openai_api_key=_get("OPENAI_API_KEY"),
            extractor_model=ext, extractor_effort=_get("OPENAI_EXTRACTOR_EFFORT"), learner_model=lrn,
            prices=prices,
            hubspot_key=_get("HUBSPOT_SERVICE_KEY"),
            hubspot_owner_id=_get("HUBSPOT_OWNER_ID"),
            slack_bot_token=_get("SLACK_BOT_TOKEN"),
            slack_app_token=_get("SLACK_APP_TOKEN"),
            slack_channel=_get("SLACK_REVIEW_CHANNEL"),
            slack_reviewer_ids=ids, slack_manager_ids=managers,
        )

    def price(self, model: str) -> tuple:
        for m, p_in, p_out in self.prices:
            if m == model:
                return p_in, p_out
        return 0.0, 0.0

    def missing(self, *names: str) -> list:
        return [n for n in names if not getattr(self, n)]
