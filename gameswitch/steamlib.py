"""Discovery of Steam games, on the SSD library and in the HD parking area."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config, vdf
from .model import HD, SSD, Game

_TOOL_RE = re.compile(config.TOOL_NAME_PATTERN, re.IGNORECASE)


def is_tool(appid: str, name: str) -> bool:
    return appid in config.TOOL_APPIDS or bool(_TOOL_RE.match(name or ""))


def steam_libraries() -> list[Path]:
    """Library roots registered with Steam (the dir that contains steamapps/)."""
    libs: list[Path] = []
    try:
        data = vdf.load(config.STEAM_LIBRARYFOLDERS)
    except OSError:
        data = {}
    folders = vdf.get_ci(data, "libraryfolders", {}) or {}
    for entry in folders.values():
        if isinstance(entry, dict) and entry.get("path"):
            libs.append(Path(entry["path"]))
        elif isinstance(entry, str):
            libs.append(Path(entry))
    if config.STEAM_ROOT not in libs:
        libs.append(config.STEAM_ROOT)
    out, seen = [], set()
    for p in libs:
        if str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    return out


def read_manifest(acf: Path) -> dict | None:
    try:
        state = vdf.get_ci(vdf.load(acf), "AppState")
    except OSError:
        return None
    if not isinstance(state, dict):
        return None
    appid = vdf.get_ci(state, "appid", "")
    if not appid:
        return None
    return {
        "appid": str(appid),
        "name": vdf.get_ci(state, "name", f"App {appid}"),
        "installdir": vdf.get_ci(state, "installdir", ""),
        "size_on_disk": int(vdf.get_ci(state, "SizeOnDisk", "0") or 0),
        "state_flags": int(vdf.get_ci(state, "StateFlags", "0") or 0),
        "bytes_to_download": int(vdf.get_ci(state, "BytesToDownload", "0") or 0),
        "bytes_downloaded": int(vdf.get_ci(state, "BytesDownloaded", "0") or 0),
        "acf": acf,
    }


def scan_installed() -> list[Game]:
    """Games with a live appmanifest inside the SSD Steam library."""
    games: list[Game] = []
    for lib in steam_libraries():
        steamapps = lib / "steamapps"
        # Only the SSD library takes part in switching.  ~/.local/share/Steam
        # holds runtimes only and lives on the root filesystem.
        try:
            same = steamapps.resolve().is_relative_to(config.SSD_STEAM_LIB.resolve())
        except (OSError, ValueError):
            same = False
        if not same:
            continue
        for acf in sorted(steamapps.glob("appmanifest_*.acf")):
            m = read_manifest(acf)
            if not m or is_tool(m["appid"], m["name"]):
                continue
            payload = steamapps / "common" / m["installdir"]
            if not m["installdir"] or not payload.is_dir():
                continue
            games.append(
                Game(
                    key=f"steam:{m['appid']}",
                    launcher="steam",
                    title=m["name"],
                    size=m["size_on_disk"] or dir_size(payload),
                    location=SSD,
                    payload=payload,
                    meta={
                        "appid": m["appid"],
                        "installdir": m["installdir"],
                        "library": lib,
                        "manifest": acf,
                        "state_flags": m["state_flags"],
                        "pending_update": bool(m["state_flags"] & 2)
                        or m["bytes_downloaded"] != m["bytes_to_download"],
                        "compatdata": steamapps / "compatdata" / m["appid"],
                    },
                )
            )
    return games


def scan_parked() -> list[Game]:
    """Games sitting in /mnt/windows/GameSwitch/steam/<appid>/."""
    games: list[Game] = []
    root = config.HD_PARK_STEAM
    if not root.is_dir():
        return games
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        side = d / ".gameswitch.json"
        info: dict = {}
        if side.is_file():
            try:
                info = json.loads(side.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                info = {}
        acfs = list(d.glob("appmanifest_*.acf"))
        m = read_manifest(acfs[0]) if acfs else None
        appid = str(info.get("appid") or (m or {}).get("appid") or d.name)
        installdir = info.get("installdir") or (m or {}).get("installdir") or ""
        title = info.get("title") or (m or {}).get("name") or f"App {appid}"
        payload = d / "common" / installdir if installdir else None
        if payload is None or not payload.is_dir():
            cand = [p for p in (d / "common").glob("*") if p.is_dir()] if (d / "common").is_dir() else []
            if not cand:
                continue
            payload = cand[0]
            installdir = payload.name
        games.append(
            Game(
                key=f"steam:{appid}",
                launcher="steam",
                title=title,
                size=int(info.get("size") or (m or {}).get("size_on_disk") or 0) or dir_size(payload),
                location=HD,
                payload=payload,
                meta={
                    "appid": appid,
                    "installdir": installdir,
                    "park_dir": d,
                    "manifest": acfs[0] if acfs else None,
                    "library": config.SSD_STEAM_LIB,
                    "pending_update": bool(info.get("pending_update")),
                    "compatdata": config.SSD_STEAM_LIB / "steamapps" / "compatdata" / appid,
                },
            )
        )
    return games


def dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def scan() -> list[Game]:
    return scan_installed() + scan_parked()
