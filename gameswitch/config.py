"""Paths and tunables for GameSwitch.

Every root can be overridden with an environment variable so the whole app can
be pointed at a fixture directory during testing without touching real games.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "GameSwitch"


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


# --- disks -----------------------------------------------------------------
SSD_ROOT = _env_path("GAMESWITCH_SSD_ROOT", "/run/media/merligus/SSD Gamer")
HD_ROOT = _env_path("GAMESWITCH_HD_ROOT", "/mnt/windows")

SSD_LABEL = "SSD Gamer"
HD_LABEL = "Windows HD"

# --- steam -----------------------------------------------------------------
STEAM_ROOT = _env_path("GAMESWITCH_STEAM_ROOT", str(Path.home() / ".local/share/Steam"))
STEAM_LIBRARYFOLDERS = STEAM_ROOT / "steamapps" / "libraryfolders.vdf"

# The library that lives on the fast disk.  Games are only ever switched
# between this library and the parking area on the HD.
SSD_STEAM_LIB = SSD_ROOT / "SteamLibrary"

# Parking area on the HD.  Deliberately NOT /mnt/windows/SteamLibrary: that is
# the *Windows* Steam library and it already contains colliding folder names
# (e.g. "Don't Starve Together" exists in both).
HD_PARK = HD_ROOT / "GameSwitch"
HD_PARK_STEAM = HD_PARK / "steam"

# --- heroic ----------------------------------------------------------------
HEROIC_CONFIG = _env_path("GAMESWITCH_HEROIC_CONFIG", str(Path.home() / ".config/heroic"))
SSD_HEROIC = SSD_ROOT / "Heroic"
HD_HEROIC = HD_ROOT / "Games" / "Heroic"

# --- state -----------------------------------------------------------------
STATE_DIR = _env_path(
    "GAMESWITCH_STATE_DIR",
    str(Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "gameswitch"),
)
JOURNAL = STATE_DIR / "journal.json"
LOCKFILE = STATE_DIR / "lock"
MODES_DIR = STATE_DIR / "modes"
BACKUP_DIR = STATE_DIR / "backups"
LOG_DIR = STATE_DIR / "logs"

# --- policy ----------------------------------------------------------------
# Destination must have size * SPACE_FACTOR + SPACE_MARGIN free.
SPACE_FACTOR = 1.05
SPACE_MARGIN = 1 * 1024 ** 3

# Steam apps that are runtimes / tools, not games.
TOOL_APPIDS = {
    "228980",    # Steamworks Common Redistributables
    "1070560",   # Steam Linux Runtime 1.0 (scout)
    "1391110",   # Steam Linux Runtime 2.0 (soldier)
    "1493710",   # Proton Experimental
    "1628350",   # Steam Linux Runtime 3.0 (sniper)
    "1826330",   # Proton EasyAntiCheat Runtime
    "1887720",   # Proton 7.0 and friends share this prefix in practice
    "2180100",   # Proton Hotfix
    "2348590",   # Proton 8.0
    "2805730",   # Proton 9.0
    "3658110",   # Proton 10.0
    "4183110",   # Steam Linux Runtime 4.0
    "858280",    # Steam VR / misc tooling
}
TOOL_NAME_PATTERN = r"^(Steam Linux Runtime|Proton|Steamworks Common|SteamVR|Steam Controller)"

# Characters that are legal on ext4 but problematic on NTFS.
NTFS_BAD_CHARS = set('\\:*?"<>|')

for _d in (STATE_DIR, MODES_DIR, BACKUP_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)
