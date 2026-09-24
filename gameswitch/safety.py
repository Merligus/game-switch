"""Preflight checks.  Nothing destructive happens unless every check passes."""
from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path

from . import config
from .model import HD, SSD, Game, Issue, human

# --------------------------------------------------------------------------
# process detection
# --------------------------------------------------------------------------

def _pgrep_exact(name: str) -> list[int]:
    """Exact comm match only.  `pgrep -f` is deliberately avoided: it happily
    matches this very process when the pattern appears in our own argv."""
    try:
        out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in out.stdout.split() if x.isdigit()]


def _own_pids() -> set[int]:
    """This process and its whole ancestor chain, so we never detect ourselves."""
    pids = {os.getpid()}
    pid = os.getppid()
    for _ in range(24):
        if pid <= 1:
            break
        pids.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat.rsplit(") ", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pids


# PIDs of helpers this app spawned (rsync and its systemd-inhibit wrapper).
# Registered by the transfer so process detection can rule them out even after
# they are reparented to init.
_OUR_CHILDREN: set[int] = set()

# Binaries this app drives.  A launcher is never one of these, so they can be
# ruled out on name alone as a second net.
_TOOL_EXES = {"rsync", "systemd-inhibit", "cp", "mv", "ionice", "nice"}


def register_child(pid: int) -> None:
    _OUR_CHILDREN.add(pid)


def unregister_child(pid: int) -> None:
    _OUR_CHILDREN.discard(pid)


def _ppid(pid: int) -> int:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def _is_ours(pid: int) -> bool:
    """True for this process, its ancestors, and anything it spawned.

    rsync chdirs into the directory it is writing into, so during a transfer our
    own copier sits inside a game folder and would otherwise be reported as a
    running launcher.  Walking the parent chain catches it whatever it is called.
    """
    mine = _own_pids()
    cur, seen = pid, set()
    for _ in range(64):
        if cur <= 1 or cur in seen:
            break
        if cur in mine or cur in _OUR_CHILDREN:
            return True
        seen.add(cur)
        cur = _ppid(cur)
    return False


def _comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _link(pid: int, what: str) -> Path | None:
    try:
        return Path(os.readlink(f"/proc/{pid}/{what}"))
    except OSError:
        return None


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()[:120]


def _procs_under(roots: tuple[Path, ...]) -> list[int]:
    """Our own user's processes whose executable or cwd lives under `roots`.

    This is how a *running game* is detected, without any pattern matching.
    """
    resolved = []
    for r in roots:
        try:
            resolved.append(str(r.resolve()))
        except OSError:
            resolved.append(str(r))
    uid = os.getuid()
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if _is_ours(pid):
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        exe_name = (_link(pid, "exe") or Path("")).name
        if exe_name in _TOOL_EXES or _comm(pid) in _TOOL_EXES:
            continue
        for what in ("exe", "cwd"):
            target = _link(pid, what)
            if target is None:
                continue
            s = str(target)
            if any(s == r or s.startswith(r.rstrip("/") + "/") for r in resolved):
                found.append(pid)
                break
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def steam_processes() -> list[tuple[int, str]]:
    """Steam client, its helper, or a game launched through it.

    ~/.steam/steam.pid is NOT trusted on its own: it is routinely left behind
    stale after a crash or an unclean logout (it is stale on this machine).
    """
    hits: dict[int, str] = {}
    for name in ("steam", "steamwebhelper", "steamerrorrepor", "gameoverlayui"):
        for pid in _pgrep_exact(name):
            if not _is_ours(pid):
                hits[pid] = _comm(pid) or name
    for pid in _procs_under((config.STEAM_ROOT, config.SSD_STEAM_LIB, config.HD_PARK_STEAM)):
        hits[pid] = _comm(pid) or _cmdline(pid)
    pidfile = Path.home() / ".steam" / "steam.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    if pid and not _is_ours(pid) and _alive(pid) and "steam" in _comm(pid).lower():
        hits.setdefault(pid, _comm(pid))
    return sorted(hits.items())


def heroic_processes() -> list[tuple[int, str]]:
    hits: dict[int, str] = {}
    for name in ("heroic", "legendary", "gogdl", "nile"):
        for pid in _pgrep_exact(name):
            if not _is_ours(pid):
                hits[pid] = _comm(pid) or name
    roots = (Path("/opt/Heroic"), config.HEROIC_CONFIG, config.SSD_HEROIC, config.HD_HEROIC)
    for pid in _procs_under(roots):
        hits[pid] = _comm(pid) or _cmdline(pid)
    return sorted(hits.items())


def close_steam(timeout: float = 30.0) -> bool:
    try:
        subprocess.run(["steam", "-shutdown"], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return not steam_processes()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not steam_processes():
            return True
        time.sleep(1.0)
    return not steam_processes()


# --------------------------------------------------------------------------
# mounts and disks
# --------------------------------------------------------------------------

def _mounts() -> list[tuple[str, str, str, set[str]]]:
    out = []
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            src, mp, fstype, opts = parts[0], parts[1], parts[2], parts[3]
            mp = mp.replace("\\040", " ").replace("\\011", "\t")
            out.append((src, mp, fstype, set(opts.split(","))))
    except OSError:
        pass
    return out


def mount_for(path: Path) -> tuple[str, str, set[str]] | None:
    """Longest mountpoint that contains `path`."""
    try:
        target = str(path.resolve())
    except OSError:
        target = str(path)
    best = None
    for src, mp, fstype, opts in _mounts():
        if target == mp or target.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) > len(best[0]):
                best = (mp, fstype, opts, src)
    if best is None:
        return None
    return (best[1], best[0], best[2])  # fstype, mountpoint, opts


def is_mounted_rw(path: Path) -> tuple[bool, str]:
    info = mount_for(path)
    if info is None:
        return False, f"{path} is not on any mounted filesystem"
    fstype, mp, opts = info
    if "ro" in opts:
        return False, (
            f"{mp} is mounted READ-ONLY ({fstype}). On NTFS this usually means "
            "Windows was shut down with Fast Startup enabled or is hibernated. "
            "Boot into Windows and shut it down fully, or run: sudo ntfsfix -d <device>"
        )
    return True, ""


def write_test(directory: Path) -> tuple[bool, str]:
    """Actually create and delete a file: `rw` in /proc/mounts can still lie.

    Any directory this has to create is removed again, so a dry run and a
    failed preflight both leave the disk exactly as they found it.
    """
    created: list[Path] = []
    probe_dir = directory
    while not probe_dir.exists() and probe_dir != probe_dir.parent:
        created.append(probe_dir)
        probe_dir = probe_dir.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".gameswitch-write-test-{os.getpid()}"
        probe.write_bytes(b"gameswitch")
        data = probe.read_bytes()
        probe.unlink()
        if data != b"gameswitch":
            return False, f"write test in {directory} read back wrong data"
        return True, ""
    except OSError as exc:
        return False, f"cannot write to {directory}: {exc}"
    finally:
        for d in created:          # deepest first
            try:
                d.rmdir()
            except OSError:
                break


def free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    st = os.statvfs(probe)
    return st.f_bavail * st.f_frsize


def total_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    st = os.statvfs(probe)
    return st.f_blocks * st.f_frsize


def required_space(size: int) -> int:
    return int(size * config.SPACE_FACTOR) + config.SPACE_MARGIN


# --------------------------------------------------------------------------
# path containment -- the gate in front of every recursive delete
# --------------------------------------------------------------------------

def within(child: Path, *parents: Path) -> bool:
    try:
        c = child.resolve()
    except OSError:
        return False
    for p in parents:
        try:
            if c.is_relative_to(p.resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def assert_deletable(path: Path, *allowed_roots: Path) -> None:
    """Raise unless `path` is a real directory safely inside an allowed root."""
    # is_symlink() must be tested on the UNRESOLVED path: resolve() follows the
    # link, so a symlink would otherwise pass and rmtree would eat its target.
    if path.is_symlink():
        raise RuntimeError(f"refusing to delete {path}: it is a symlink")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise RuntimeError(f"refusing to delete {resolved}: not a directory")
    if not within(resolved, *allowed_roots):
        raise RuntimeError(
            f"refusing to delete {resolved}: outside {[str(r) for r in allowed_roots]}"
        )
    if len(resolved.parts) <= 3:
        raise RuntimeError(f"refusing to delete {resolved}: too close to the filesystem root")
    info = mount_for(resolved)
    if info and str(resolved) == info[1]:
        raise RuntimeError(f"refusing to delete {resolved}: it is a mount point")


# --------------------------------------------------------------------------
# single instance
# --------------------------------------------------------------------------

class InstanceLock:
    def __init__(self) -> None:
        self._fh = None

    def acquire(self) -> bool:
        config.LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(config.LOCKFILE, "w")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return True

    def release(self) -> None:
        if self._fh:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


# --------------------------------------------------------------------------
# the actual preflight
# --------------------------------------------------------------------------

def launcher_issues(launcher: str) -> list[Issue]:
    """Only the launcher that owns the game is a hard blocker.

    Steam never touches Heroic's files and vice versa, so blocking a Steam move
    because Heroic happens to be open would just be annoying.  A *busy* other
    launcher is still worth a warning: it competes for free space and I/O.
    """
    issues: list[Issue] = []
    steam = steam_processes()
    heroic = heroic_processes()

    if launcher == "steam" and steam:
        names = ", ".join(f"{c}({p})" for p, c in steam[:4])
        issues.append(Issue(
            "block", "steam_running",
            f"Steam is running ({names}). Close Steam before switching a game — it "
            "rewrites appmanifest files on exit and would undo the move.",
            fix="close_steam"))
    if launcher == "heroic" and heroic:
        names = ", ".join(f"{c}({p})" for p, c in heroic[:4])
        issues.append(Issue(
            "block", "heroic_running",
            f"Heroic is running ({names}). Close Heroic before switching a game — it "
            "rewrites its library JSON on exit and would undo the move.",
            fix="close_heroic"))

    if launcher == "steam" and heroic:
        issues.append(Issue("warn", "heroic_busy",
                            f"Heroic is running ({len(heroic)} processes). If it is downloading, "
                            "free space on the destination can shrink during the transfer."))
    if launcher == "heroic" and steam:
        issues.append(Issue("warn", "steam_busy",
                            "Steam is running. It will not touch Heroic's files, but it may be "
                            "downloading and competing for disk space."))
    return issues


def preflight(game: Game, dest_payload: Path, dest_container: Path) -> list[Issue]:
    issues = launcher_issues(game.launcher)

    for label, path in ((config.SSD_ROOT, config.SSD_ROOT), (config.HD_ROOT, config.HD_ROOT)):
        ok, err = is_mounted_rw(path)
        if not ok:
            issues.append(Issue("block", "mount_ro", err))

    if not any(i.code == "mount_ro" for i in issues):
        ok, err = write_test(dest_container)
        if not ok:
            issues.append(Issue("block", "dest_unwritable", err))
        ok, err = write_test(game.payload.parent)
        if not ok:
            issues.append(Issue("block", "src_unwritable",
                                f"{err} — the source must be writable so the old copy can be removed."))

    need = required_space(game.size)
    have = free_bytes(dest_container)
    if have < need:
        issues.append(Issue("block", "no_space",
                            f"Not enough room on the destination: need {human(need)} "
                            f"(game {human(game.size)} + safety margin), {human(have)} free."))

    if dest_payload.exists():
        try:
            leftover = any(dest_payload.iterdir())
        except OSError:
            leftover = True
        if leftover:
            issues.append(Issue("block", "dest_exists",
                                f"{dest_payload} already exists and is not empty — most likely a "
                                "previous transfer was interrupted.", fix="clean_dest"))

    allowed = (config.SSD_ROOT, config.HD_ROOT)
    if not within(game.payload, *allowed):
        issues.append(Issue("block", "src_escape",
                            f"Source {game.payload} is outside the managed disks — refusing to touch it."))
    if not within(dest_payload.parent if not dest_payload.exists() else dest_payload, *allowed):
        issues.append(Issue("block", "dest_escape",
                            f"Destination {dest_payload} is outside the managed disks — refusing."))

    if game.launcher == "steam":
        manifest = game.meta.get("manifest")
        if not manifest or not Path(manifest).is_file():
            issues.append(Issue("block", "no_manifest",
                                "The Steam appmanifest for this game is missing; nothing to move."))
        if game.meta.get("pending_update"):
            issues.append(Issue("warn", "pending_update",
                                "Steam has a pending update for this game. The move is safe, but "
                                "Steam will want to download the update after you switch it back."))
        compat = game.meta.get("compatdata")
        if compat and game.location == HD and not Path(compat).exists():
            issues.append(Issue("warn", "no_compatdata",
                                f"The wine prefix {compat} is gone. Saves stored inside the prefix "
                                "would be lost; Steam will create a fresh one."))
    return issues


def scan_ntfs_hazards(root: Path) -> list[str]:
    """Names that are legal on ext4 but risky on NTFS."""
    problems: list[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            lowered: dict[str, str] = {}
            for name in list(dirnames) + list(filenames):
                if set(name) & config.NTFS_BAD_CHARS:
                    problems.append(f"illegal NTFS character in {Path(dirpath, name)}")
                if name != name.rstrip(". "):
                    problems.append(f"trailing dot/space in {Path(dirpath, name)}")
                prev = lowered.get(name.lower())
                if prev is not None and prev != name:
                    problems.append(f"case-only collision: {Path(dirpath, prev)} vs {name}")
                lowered[name.lower()] = name
            if len(problems) > 20:
                problems.append("... (more suppressed)")
                return problems
    except OSError as exc:
        problems.append(f"could not scan {root}: {exc}")
    return problems
