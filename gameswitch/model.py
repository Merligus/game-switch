"""Shared data types."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SSD = "ssd"
HD = "hd"


@dataclass
class Game:
    key: str                 # "steam:4496690" / "heroic:epic:<app_name>"
    launcher: str            # "steam" | "heroic"
    title: str
    size: int                # bytes, best known estimate
    location: str            # SSD | HD
    payload: Path            # directory that actually holds the game files
    meta: dict = field(default_factory=dict)

    @property
    def location_label(self) -> str:
        return "SSD" if self.location == SSD else "HD"

    @property
    def target(self) -> str:
        return HD if self.location == SSD else SSD

    @property
    def launcher_label(self) -> str:
        if self.launcher == "heroic":
            return f"Heroic ({self.meta.get('runner', '?')})"
        return "Steam"


@dataclass
class Issue:
    level: str               # "block" | "warn"
    code: str
    message: str
    fix: str | None = None   # "close_steam" | "close_heroic" | "clean_dest" | None


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"
