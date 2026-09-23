"""Discovery and relocation of Heroic games (Epic / GOG / Amazon / sideloaded).

Unlike Steam, Heroic is happy to run a game from any path, so a switch here is
a plain move plus an atomic rewrite of `install_path` in Heroic's store JSON.
Wine prefixes are never touched.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from . import config
from .model import HD, SSD, Game

# runner -> (json file, layout)
STORES: dict[str, tuple[Path, str]] = {
    "epic": (config.HEROIC_CONFIG / "legendaryConfig/legendary/installed.json", "map"),
    "gog": (config.HEROIC_CONFIG / "gog_store/installed.json", "installed_list"),
    "amazon": (config.HEROIC_CONFIG / "nile_config/nile/installed.json", "list"),
    "sideload": (config.HEROIC_CONFIG / "sideload_apps/library.json", "games_list"),
}

_PATH_KEYS = ("install_path", "path", "install_dir")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _entries(runner: str) -> list[tuple[dict, dict]]:
    """Return (container, entry) pairs so a caller can mutate entries in place."""
    path, layout = STORES[runner]
    data = _read_json(path)
    if data is None:
        return []
    out: list[tuple[dict, dict]] = []
    if layout == "map" and isinstance(data, dict):
        out = [(data, v) for v in data.values() if isinstance(v, dict)]
    elif layout == "installed_list" and isinstance(data, dict):
        out = [(data, v) for v in (data.get("installed") or []) if isinstance(v, dict)]
    elif layout == "list" and isinstance(data, list):
        out = [({}, v) for v in data if isinstance(v, dict)]
    elif layout == "games_list" and isinstance(data, dict):
        out = [({}, v) for v in (data.get("games") or []) if isinstance(v, dict)]
    return out


def _entry_path(entry: dict) -> str | None:
    for k in _PATH_KEYS:
        if entry.get(k):
            return entry[k]
    inst = entry.get("install")
    if isinstance(inst, dict):
        for k in _PATH_KEYS:
            if inst.get(k):
                return inst[k]
    return None


def _entry_id(runner: str, entry: dict) -> str:
    for k in ("app_name", "appName", "id", "app_id"):
        if entry.get(k):
            return str(entry[k])
    return str(entry.get("title", "unknown"))


def _entry_title(entry: dict) -> str:
    return entry.get("title") or entry.get("name") or _entry_id("", entry)


def _entry_size(entry: dict) -> int:
    for k in ("install_size", "installedSize", "size"):
        v = entry.get(k) or (entry.get("install") or {}).get(k) if isinstance(entry.get("install"), dict) else entry.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return 0


def scan() -> list[Game]:
    """One row per real game.

    A DLC shares its parent game's install_path and has no executable of its
    own; listing it separately would offer a "switch" that really moves the
    parent game, and leave a duplicate row behind.
    """
    games = _scan_raw()
    by_path: dict[str, list[Game]] = {}
    for g in games:
        by_path.setdefault(str(g.payload).rstrip("/"), []).append(g)
    out: list[Game] = []
    for group in by_path.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        # the parent is the one with an executable, else the biggest
        parent = next((g for g in group if g.meta.get("executable")), None) or \
            max(group, key=lambda g: g.size)
        extras = [g.title for g in group if g is not parent]
        parent.meta["shares_folder_with"] = extras
        out.append(parent)
    return out


def _scan_raw() -> list[Game]:
    games: list[Game] = []
    for runner in STORES:
        store_path, _ = STORES[runner]
        for _container, entry in _entries(runner):
            raw = _entry_path(entry)
            if not raw:
                continue
            payload = Path(raw)
            if not payload.is_dir():
                continue
            try:
                on_hd = payload.resolve().is_relative_to(config.HD_ROOT.resolve())
            except (OSError, ValueError):
                on_hd = False
            try:
                on_ssd = payload.resolve().is_relative_to(config.SSD_ROOT.resolve())
            except (OSError, ValueError):
                on_ssd = False
            if not (on_hd or on_ssd):
                continue  # lives somewhere we do not manage
            app_id = _entry_id(runner, entry)
            size = _entry_size(entry)
            games.append(
                Game(
                    key=f"heroic:{runner}:{app_id}",
                    launcher="heroic",
                    title=_entry_title(entry),
                    size=size or _dir_size(payload),
                    location=HD if on_hd else SSD,
                    payload=payload,
                    meta={
                        "runner": runner,
                        "app_id": app_id,
                        "store": store_path,
                        "folder": payload.name,
                        "executable": entry.get("executable")
                        or (entry.get("install") or {}).get("executable") or "",
                    },
                )
            )
    return games


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def destination_for(game: Game) -> Path:
    root = config.HD_HEROIC if game.target == HD else config.SSD_HEROIC
    return root / game.meta["folder"]


def rewrite_install_path(game: Game, new_path: Path) -> None:
    """Atomically point Heroic's store JSON at `new_path`.  Backs up first."""
    runner = game.meta["runner"]
    store_path, layout = STORES[runner]
    data = _read_json(store_path)
    if data is None:
        raise RuntimeError(f"cannot read Heroic store {store_path}")

    backup = config.BACKUP_DIR / f"{runner}-{int(time.time())}-{store_path.name}"
    shutil.copy2(store_path, backup)

    target_id = game.meta["app_id"]
    old = str(game.payload)
    new = str(new_path)
    changed = 0

    def patch(entry: dict) -> bool:
        hit = False
        for holder in (entry, entry.get("install") if isinstance(entry.get("install"), dict) else None):
            if not isinstance(holder, dict):
                continue
            for k in _PATH_KEYS:
                if holder.get(k) and str(holder[k]).rstrip("/") == old.rstrip("/"):
                    holder[k] = new
                    hit = True
                # executables and manifests embed the old prefix too
            for k in ("executable", "install_path_executable", "folder_name"):
                v = holder.get(k)
                if isinstance(v, str) and v.startswith(old):
                    holder[k] = new + v[len(old):]
                    hit = True
        return hit

    def wanted(entry: dict, key: str | None = None) -> bool:
        """The target game, plus any DLC installed into the same folder.

        Epic DLCs get their own entry in installed.json but share the parent
        game's install_path, so rewriting only the target would leave them
        pointing at a directory that no longer exists.
        """
        if key == target_id or _entry_id(runner, entry) == target_id:
            return True
        other = _entry_path(entry)
        return bool(other) and str(other).rstrip("/") == old.rstrip("/")

    if layout == "map" and isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict) and wanted(v, k):
                changed += patch(v)
    elif layout == "installed_list":
        for v in data.get("installed") or []:
            if isinstance(v, dict) and wanted(v):
                changed += patch(v)
    elif layout == "list":
        for v in data:
            if isinstance(v, dict) and wanted(v):
                changed += patch(v)
    elif layout == "games_list":
        for v in data.get("games") or []:
            if isinstance(v, dict) and wanted(v):
                changed += patch(v)

    if not changed:
        raise RuntimeError(f"{target_id} not found in {store_path}; nothing rewritten (backup: {backup})")

    tmp = store_path.with_suffix(store_path.suffix + ".gameswitch.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, store_path)
