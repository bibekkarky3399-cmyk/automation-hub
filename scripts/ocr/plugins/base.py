from __future__ import annotations

from typing import Protocol


class AirlinePlugin(Protocol):
    name: str

    def parse_rows(self, items: list[dict]) -> list[dict[str, str]]:
        """Parse OCR tokens into flight rows (without QC columns)."""
        ...
