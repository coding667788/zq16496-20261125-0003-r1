from __future__ import annotations

from typing import Any


class VaultDrillError(Exception):
    """A user-correctable input/schema error."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            data["details"] = self.details
        return data
