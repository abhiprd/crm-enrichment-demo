"""Repository paths. Everything resolves from one root so tests can point at a temp dir."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    root: Path

    @classmethod
    def from_env(cls, root: str | os.PathLike | None = None) -> "Paths":
        base = root or os.environ.get("CRM_ROOT") or Path.cwd()
        return cls(Path(base).resolve())

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def deals(self) -> Path:
        return self.data / "deals.json"

    @property
    def transcripts(self) -> Path:
        return self.data / "transcripts"

    @property
    def keys(self) -> Path:
        return self.data / "keys"

    @property
    def audit(self) -> Path:
        return self.data / "audit"

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def processed(self) -> Path:
        return self.inbox / "processed"

    @property
    def rejected(self) -> Path:
        return self.inbox / "rejected"

    @property
    def requests(self) -> Path:
        return self.root / "prompts" / "requests"

    @property
    def request_template(self) -> Path:
        return self.root / "prompts" / "transcript_request.md"

    def ensure(self) -> None:
        for d in (self.transcripts, self.keys, self.audit, self.inbox,
                  self.processed, self.rejected, self.requests):
            d.mkdir(parents=True, exist_ok=True)
