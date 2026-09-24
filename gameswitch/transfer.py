"""The move engine: scan -> copy -> verify -> register -> delete.

Design rule: the source is never touched until the destination has been copied
AND verified, and the launcher's metadata is the last thing to change, so an
interruption at any point leaves a state that is either "fully on the old disk"
or "fully on the new disk" -- never half of each.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import config, heroiclib, safety
from .model import HD, SSD, Game, Issue, human

PHASES = ["preflight", "scan", "copy", "verify", "register", "cleanup", "done"]

_PROGRESS_RE = re.compile(r"^\s*([\d,]+)\s+(\d+)%\s+(\S+)\s+(\S+)")


class Cancelled(Exception):
    pass


class TransferError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# tree scanning + permission sidecar
# ---------------------------------------------------------------------------

@dataclass
class Tree:
    files: int = 0
    bytes: int = 0
    file_mode: int = 0o644
    dir_mode: int = 0o755
    file_exc: dict[str, int] = field(default_factory=dict)
    dir_exc: dict[str, int] = field(default_factory=dict)
    symlinks: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "files": self.files,
            "bytes": self.bytes,
            "file_mode": oct(self.file_mode),
            "dir_mode": oct(self.dir_mode),
            "file_exc": {k: oct(v) for k, v in self.file_exc.items()},
            "dir_exc": {k: oct(v) for k, v in self.dir_exc.items()},
            "symlinks": self.symlinks,
        }

    @staticmethod
    def from_json(d: dict) -> "Tree":
        return Tree(
            files=int(d.get("files", 0)),
            bytes=int(d.get("bytes", 0)),
            file_mode=int(d.get("file_mode", "0o644"), 8),
            dir_mode=int(d.get("dir_mode", "0o755"), 8),
            file_exc={k: int(v, 8) for k, v in (d.get("file_exc") or {}).items()},
            dir_exc={k: int(v, 8) for k, v in (d.get("dir_exc") or {}).items()},
            symlinks=dict(d.get("symlinks") or {}),
        )


def scan_tree(root: Path) -> Tree:
    """Walk `root` recording size, file count and permission bits.

    Modes are stored as "dominant mode + exceptions" so the sidecar stays tiny
    even for a game like Terraria with 16k files.
    """
    file_modes: Counter[int] = Counter()
    dir_modes: Counter[int] = Counter()
    per_file: dict[str, int] = {}
    per_dir: dict[str, int] = {}
    symlinks: dict[str, str] = {}
    files = 0
    total = 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in list(dirnames):
            p = here / name
            if p.is_symlink():
                dirnames.remove(name)
                symlinks[str(p.relative_to(root))] = os.readlink(p)
                continue
            try:
                m = stat.S_IMODE(p.lstat().st_mode)
            except OSError:
                continue
            rel = str(p.relative_to(root))
            dir_modes[m] += 1
            per_dir[rel] = m
        for name in filenames:
            p = here / name
            try:
                st = p.lstat()
            except OSError:
                continue
            rel = str(p.relative_to(root))
            if stat.S_ISLNK(st.st_mode):
                symlinks[rel] = os.readlink(p)
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            files += 1
            total += st.st_size
            m = stat.S_IMODE(st.st_mode)
            file_modes[m] += 1
            per_file[rel] = m

    t = Tree(files=files, bytes=total)
    if file_modes:
        t.file_mode = file_modes.most_common(1)[0][0]
    if dir_modes:
        t.dir_mode = dir_modes.most_common(1)[0][0]
    t.file_exc = {k: v for k, v in per_file.items() if v != t.file_mode}
    t.dir_exc = {k: v for k, v in per_dir.items() if v != t.dir_mode}
    t.symlinks = symlinks
    return t


def infer_modes(root: Path) -> int:
    """Fallback when no sidecar exists: +x for ELF binaries and shebang scripts."""
    fixed = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath, name)
            try:
                with open(p, "rb") as fh:
                    head = fh.read(4)
            except OSError:
                continue
            if head[:4] == b"\x7fELF" or head[:2] == b"#!":
                try:
                    os.chmod(p, 0o755)
                    fixed += 1
                except OSError:
                    pass
    return fixed


def apply_modes(root: Path, tree: Tree, log=None) -> None:
    """Restore permissions lost by the round trip through NTFS."""
    dirs: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirs.append(here)
        for name in filenames:
            p = here / name
            rel = str(p.relative_to(root))
            mode = tree.file_exc.get(rel, tree.file_mode)
            try:
                os.chmod(p, mode)
            except OSError as exc:
                if log:
                    log(f"chmod {p}: {exc}")
    # deepest first so a restrictive mode never blocks traversal
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        rel = str(d.relative_to(root)) if d != root else "."
        mode = tree.dir_exc.get(rel, tree.dir_mode)
        try:
            os.chmod(d, mode)
        except OSError as exc:
            if log:
                log(f"chmod {d}: {exc}")
    for rel, target in tree.symlinks.items():
        link = root / rel
        try:
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                link.unlink()
            os.symlink(target, link)
        except OSError as exc:
            if log:
                log(f"symlink {link} -> {target}: {exc}")


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------

def write_journal(data: dict) -> None:
    config.JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.JOURNAL.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, config.JOURNAL)


def read_journal() -> dict | None:
    try:
        return json.loads(config.JOURNAL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_journal() -> None:
    try:
        config.JOURNAL.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    game: Game
    direction: str                 # "to_hd" | "to_ssd"
    src_payload: Path
    dest_payload: Path
    dest_container: Path
    src_manifest: Path | None = None
    dest_manifest: Path | None = None
    park_dir: Path | None = None
    dest_is_ntfs: bool = False

    def describe(self) -> list[str]:
        arrow = "SSD -> HD" if self.direction == "to_hd" else "HD -> SSD"
        out = [f"{self.game.title}  [{arrow}]",
               f"  copy    {self.src_payload}",
               f"       -> {self.dest_payload}"]
        if self.src_manifest and self.dest_manifest:
            out.append(f"  manifest {self.src_manifest.name}: {self.src_manifest.parent} -> {self.dest_manifest.parent}")
        if self.game.launcher == "heroic":
            out.append(f"  rewrite install_path in {self.game.meta['store']}")
        out.append(f"  delete  {self.src_payload}")
        if self.direction == "to_ssd" and self.park_dir:
            out.append(f"  delete  {self.park_dir}")
        return out


def plan_for(game: Game) -> Plan:
    to_hd = game.location == SSD
    direction = "to_hd" if to_hd else "to_ssd"

    if game.launcher == "steam":
        appid = game.meta["appid"]
        installdir = game.meta["installdir"]
        lib_steamapps = config.SSD_STEAM_LIB / "steamapps"
        park_dir = config.HD_PARK_STEAM / appid
        manifest_name = f"appmanifest_{appid}.acf"
        if to_hd:
            return Plan(
                game=game, direction=direction,
                src_payload=game.payload,
                dest_payload=park_dir / "common" / installdir,
                dest_container=park_dir,
                src_manifest=Path(game.meta["manifest"]),
                dest_manifest=park_dir / manifest_name,
                park_dir=park_dir,
                dest_is_ntfs=True,
            )
        return Plan(
            game=game, direction=direction,
            src_payload=game.payload,
            dest_payload=lib_steamapps / "common" / installdir,
            dest_container=lib_steamapps,
            src_manifest=Path(game.meta["manifest"]) if game.meta.get("manifest") else None,
            dest_manifest=lib_steamapps / manifest_name,
            park_dir=game.meta.get("park_dir"),
            dest_is_ntfs=False,
        )

    dest = heroiclib.destination_for(game)
    return Plan(
        game=game, direction=direction,
        src_payload=game.payload,
        dest_payload=dest,
        dest_container=dest.parent,
        dest_is_ntfs=to_hd,
    )


# ---------------------------------------------------------------------------
# the transfer
# ---------------------------------------------------------------------------

class Transfer:
    def __init__(self, game: Game, *, deep_verify: bool = False, dry_run: bool = False,
                 on_phase=None, on_progress=None, on_log=None) -> None:
        self.game = game
        self.plan = plan_for(game)
        self.deep_verify = deep_verify
        self.dry_run = dry_run
        self._on_phase = on_phase or (lambda *a: None)
        self._on_progress = on_progress or (lambda *a: None)
        self._on_log = on_log or (lambda *a: None)
        self._cancel = False
        self._proc: subprocess.Popen | None = None
        self.tree: Tree | None = None
        self.warnings: list[Issue] = []

    # -- helpers ------------------------------------------------------------
    def cancel(self) -> None:
        self._cancel = True
        p = self._proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def _check_cancel(self) -> None:
        if self._cancel:
            raise Cancelled()

    def log(self, msg: str) -> None:
        self._on_log(msg)

    def phase(self, name: str, text: str = "") -> None:
        self._on_phase(name, text)
        self.log(f"[{name}] {text}" if text else f"[{name}]")

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        return (config.SSD_ROOT, config.HD_ROOT)

    # -- steps --------------------------------------------------------------
    def run(self) -> None:
        try:
            self._preflight()
            self._scan()
            self._copy()
            self._verify()
            self._register()
            self._cleanup()
            self.phase("done", "Transfer complete.")
            clear_journal()
        except Cancelled:
            self.phase("cancelled", "Cancelled — rolling back the partial copy.")
            self._rollback_partial()
            clear_journal()
            raise
        except Exception:
            self.log("FAILED — the source was left untouched unless stated above.")
            raise

    def _preflight(self) -> None:
        self.phase("preflight", "Checking disks and launchers")
        issues = safety.preflight(self.game, self.plan.dest_payload, self.plan.dest_container)
        blockers = [i for i in issues if i.level == "block"]
        self.warnings = [i for i in issues if i.level == "warn"]
        for w in self.warnings:
            self.log(f"WARNING: {w.message}")
        if blockers:
            raise TransferError("\n".join(b.message for b in blockers))
        self._check_cancel()

    def _scan(self) -> None:
        self.phase("scan", f"Reading {self.plan.src_payload}")
        self.tree = scan_tree(self.plan.src_payload)
        self.log(f"{self.tree.files} files, {human(self.tree.bytes)}, "
                 f"{len(self.tree.file_exc)} permission exceptions, "
                 f"{len(self.tree.symlinks)} symlinks")
        if self.plan.dest_is_ntfs:
            hazards = safety.scan_ntfs_hazards(self.plan.src_payload)
            for h in hazards:
                self.log(f"WARNING (NTFS): {h}")
            if hazards:
                self.warnings.append(Issue("warn", "ntfs_names",
                                           f"{len(hazards)} file name(s) may not survive NTFS; see log."))
        if not self.dry_run:
            modes_file = config.MODES_DIR / f"{self.game.key.replace(':', '-')}.json"
            modes_file.write_text(json.dumps(self.tree.to_json(), indent=1), encoding="utf-8")
            self.log(f"permission sidecar -> {modes_file}")
        self._journal("scan")
        self._check_cancel()

    def _journal(self, phase: str) -> None:
        if self.dry_run:
            return
        p = self.plan
        write_journal({
            "version": 1,
            "key": self.game.key,
            "launcher": self.game.launcher,
            "title": self.game.title,
            "direction": p.direction,
            "phase": phase,
            "started": int(time.time()),
            "src_payload": str(p.src_payload),
            "dest_payload": str(p.dest_payload),
            "dest_container": str(p.dest_container),
            "src_manifest": str(p.src_manifest) if p.src_manifest else None,
            "dest_manifest": str(p.dest_manifest) if p.dest_manifest else None,
            "park_dir": str(p.park_dir) if p.park_dir else None,
            "dest_is_ntfs": p.dest_is_ntfs,
            "files": self.tree.files if self.tree else 0,
            "bytes": self.tree.bytes if self.tree else 0,
            "tree": self.tree.to_json() if self.tree else None,
            "meta": {k: str(v) for k, v in self.game.meta.items()},
        })

    def _rsync_cmd(self, extra: list[str]) -> list[str]:
        cmd = ["rsync", "-rt", "--partial", "--no-inc-recursive"]
        if self.plan.dest_is_ntfs:
            # ntfs3 cannot hold Unix ownership or permission bits
            cmd += ["--no-perms", "--no-owner", "--no-group"]
        cmd += extra
        cmd += [str(self.plan.src_payload) + "/", str(self.plan.dest_payload) + "/"]
        if shutil.which("systemd-inhibit"):
            cmd = ["systemd-inhibit", "--what=sleep:idle", "--who=GameSwitch",
                   f"--why=Moving {self.game.title}", "--mode=block", "--"] + cmd
        return cmd

    def _copy(self) -> None:
        total = self.tree.bytes if self.tree else 0
        self.phase("copy", f"Copying {human(total)} to {self.plan.dest_payload}")
        self._journal("copy")
        if not self.dry_run:
            self.plan.dest_payload.mkdir(parents=True, exist_ok=True)
        cmd = self._rsync_cmd(["--info=progress2"] + (["--dry-run"] if self.dry_run else []))
        self.log("$ " + " ".join(cmd))

        env = dict(os.environ, LC_ALL="C")
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        safety.register_child(self._proc.pid)
        started = time.time()
        pending = b""
        fd = self._proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            pending += chunk
            parts = re.split(rb"[\r\n]", pending)
            pending = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                m = _PROGRESS_RE.match(line)
                if m:
                    done = int(m.group(1).replace(",", ""))
                    speed = m.group(3)
                    elapsed = max(time.time() - started, 0.001)
                    rate = done / elapsed
                    eta = int((total - done) / rate) if rate > 0 and total > done else 0
                    self._on_progress(done, total, speed, eta)
                else:
                    self.log(line)
            if self._cancel:
                break
        rc = self._proc.wait()
        safety.unregister_child(self._proc.pid)
        self._proc = None
        self._check_cancel()
        if rc != 0:
            raise TransferError(f"rsync failed with exit code {rc} — nothing was deleted.")
        if not self.dry_run:
            self._fsync_dir(self.plan.dest_payload)
        self._on_progress(total, total, "", 0)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _verify(self) -> None:
        self.phase("verify", "Verifying the copy")
        self._journal("verify")
        if self.dry_run:
            self.log("dry run: skipping verification")
            return
        got = scan_tree(self.plan.dest_payload)
        want = self.tree
        self.log(f"source {want.files} files / {human(want.bytes)}  ->  "
                 f"destination {got.files} files / {human(got.bytes)}")
        if got.files != want.files or got.bytes != want.bytes:
            raise TransferError(
                f"Verification FAILED: destination has {got.files} files / {got.bytes} bytes, "
                f"source has {want.files} files / {want.bytes} bytes. "
                "Nothing was deleted; the game is still intact on the original disk."
            )
        if self.deep_verify:
            self.phase("verify", "Deep verify (checksumming every file)")
            cmd = self._rsync_cmd(["-c", "--dry-run", "--itemize-changes"])
            env = dict(os.environ, LC_ALL="C")
            out = subprocess.run(cmd, capture_output=True, text=True, env=env)
            diffs = [l for l in out.stdout.splitlines() if l[:1] in "<>ch*"]
            for d in diffs[:20]:
                self.log("DIFF " + d)
            if diffs:
                raise TransferError(
                    f"Deep verify FAILED: {len(diffs)} file(s) differ. Nothing was deleted."
                )
            self.log("deep verify clean")
        self._check_cancel()

    def _register(self) -> None:
        """Point the launcher at the new location.  Idempotent."""
        self.phase("register", "Updating launcher metadata")
        self._journal("register")
        if self.dry_run:
            for line in self.plan.describe():
                self.log("would: " + line)
            return

        if not self.plan.dest_is_ntfs:
            # coming back to ext4: restore the permissions NTFS could not hold
            tree = self.tree
            modes_file = config.MODES_DIR / f"{self.game.key.replace(':', '-')}.json"
            side = (self.plan.park_dir / ".gameswitch.json") if self.plan.park_dir else None
            if side and side.is_file():
                try:
                    embedded = json.loads(side.read_text(encoding="utf-8")).get("tree")
                    if embedded:
                        tree = Tree.from_json(embedded)
                        self.log("permissions restored from the sidecar stored next to the game")
                except (OSError, ValueError):
                    pass
            elif modes_file.is_file():
                try:
                    tree = Tree.from_json(json.loads(modes_file.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    tree = None
            if tree and (tree.file_exc or tree.file_mode != 0o644 or tree.symlinks):
                apply_modes(self.plan.dest_payload, tree, log=self.log)
                self.log(f"permissions applied (default {oct(tree.file_mode)}, "
                         f"{len(tree.file_exc)} exceptions, {len(tree.symlinks)} symlinks)")
            else:
                n = infer_modes(self.plan.dest_payload)
                self.log(f"no sidecar found — inferred +x for {n} ELF/script files")

        if self.game.launcher == "steam":
            self._register_steam()
        else:
            heroiclib.rewrite_install_path(self.game, self.plan.dest_payload)
            self.log(f"Heroic install_path -> {self.plan.dest_payload}")
        self._check_cancel()

    def _register_steam(self) -> None:
        p = self.plan
        if p.direction == "to_hd":
            p.park_dir.mkdir(parents=True, exist_ok=True)
            side = p.park_dir / ".gameswitch.json"
            tree_json = self.tree.to_json() if self.tree else None
            if side.is_file():
                # an idempotent re-run (crash recovery) must not replace a
                # complete permission record with a thinner one
                try:
                    old = json.loads(side.read_text(encoding="utf-8")).get("tree")
                    if old and len(old.get("file_exc") or {}) >= len((tree_json or {}).get("file_exc") or {}):
                        tree_json = old
                        self.log("kept the existing permission sidecar")
                except (OSError, ValueError):
                    pass
            side.write_text(json.dumps({
                "version": 1,
                "appid": self.game.meta["appid"],
                "title": self.game.title,
                "installdir": self.game.meta["installdir"],
                "size": self.tree.bytes if self.tree else self.game.size,
                "files": self.tree.files if self.tree else 0,
                "parked_at": int(time.time()),
                "source_library": str(config.SSD_STEAM_LIB),
                "pending_update": bool(self.game.meta.get("pending_update")),
                "compatdata": str(self.game.meta.get("compatdata", "")),
                "tree": tree_json,
            }, indent=2), encoding="utf-8")
            if p.src_manifest and p.src_manifest.is_file():
                shutil.copy2(p.src_manifest, p.dest_manifest)
                self._fsync_file(p.dest_manifest)
                self.log(f"manifest copied to {p.dest_manifest}")
                p.src_manifest.unlink()
                self.log(f"manifest removed from the Steam library — Steam now shows "
                         f"{self.game.title} as not installed")
            elif p.dest_manifest.is_file():
                self.log("manifest already parked (idempotent re-run)")
            else:
                raise TransferError("the appmanifest vanished mid-transfer")
        else:
            src = p.src_manifest
            if src and Path(src).is_file():
                shutil.copy2(src, p.dest_manifest)
                self._fsync_file(p.dest_manifest)
                self.log(f"manifest restored to {p.dest_manifest}")
            elif p.dest_manifest.is_file():
                self.log("manifest already restored (idempotent re-run)")
            else:
                raise TransferError(
                    "no appmanifest found in the parking directory — Steam would not see the game. "
                    "The game files are on the SSD; re-add them with Steam's 'Install' + 'Verify'."
                )

    @staticmethod
    def _fsync_file(path: Path) -> None:
        try:
            with open(path, "rb") as fh:
                os.fsync(fh.fileno())
        except OSError:
            pass

    def _cleanup(self) -> None:
        self.phase("cleanup", "Removing the old copy")
        self._journal("cleanup")
        if self.dry_run:
            self.log(f"would delete {self.plan.src_payload}")
            return

        # last gate: the launcher must still be closed before anything is deleted
        late = safety.launcher_issues(self.game.launcher)
        blockers = [i for i in late if i.level == "block"]
        if blockers:
            raise TransferError(
                "The launcher was opened during the transfer, so the old copy was NOT deleted. "
                "The game is now present on both disks; close it and run the switch again.\n"
                + "\n".join(b.message for b in blockers))

        if self.plan.src_payload.is_dir():
            safety.assert_deletable(self.plan.src_payload, *self.allowed_roots)
            shutil.rmtree(self.plan.src_payload)
            self.log(f"deleted {self.plan.src_payload}")
        if self.plan.direction == "to_ssd" and self.plan.park_dir and Path(self.plan.park_dir).is_dir():
            safety.assert_deletable(Path(self.plan.park_dir), config.HD_PARK)
            shutil.rmtree(self.plan.park_dir)
            self.log(f"deleted parking directory {self.plan.park_dir}")

    def _rollback_partial(self) -> None:
        dest = self.plan.dest_payload
        if self.dry_run or not dest.exists():
            return
        try:
            safety.assert_deletable(dest, *self.allowed_roots)
            shutil.rmtree(dest)
            self.log(f"removed partial copy {dest}")
            if self.plan.direction == "to_hd" and self.plan.park_dir:
                pk = Path(self.plan.park_dir)
                if pk.is_dir() and not any(pk.rglob("appmanifest_*.acf")):
                    shutil.rmtree(pk, ignore_errors=True)
        except Exception as exc:
            self.log(f"could not remove the partial copy: {exc}")


# ---------------------------------------------------------------------------
# crash recovery
# ---------------------------------------------------------------------------

def recovery_options(j: dict) -> list[str]:
    """Before `register` the copy is disposable; from `register` on it is the
    good copy and the only safe move is to finish."""
    return ["finish"] if j.get("phase") in ("register", "cleanup") else ["resume", "rollback"]


def _game_from_journal(j: dict) -> Game:
    meta = dict(j.get("meta") or {})
    for k in ("manifest", "park_dir", "library", "compatdata", "store"):
        if meta.get(k) and meta[k] != "None":
            meta[k] = Path(meta[k])
        elif k in meta:
            meta[k] = None
    meta["pending_update"] = str(meta.get("pending_update", "False")) == "True"
    return Game(
        key=j["key"],
        launcher=j.get("launcher", "steam"),
        title=j.get("title", j["key"]),
        size=int(j.get("bytes") or 0),
        location=SSD if j.get("direction") == "to_hd" else HD,
        payload=Path(j["src_payload"]),
        meta=meta,
    )


def transfer_from_journal(j: dict, *, on_phase=None, on_log=None) -> Transfer:
    game = _game_from_journal(j)
    t = Transfer(game, on_phase=on_phase, on_log=on_log)
    t.plan = Plan(
        game=game,
        direction=j["direction"],
        src_payload=Path(j["src_payload"]),
        dest_payload=Path(j["dest_payload"]),
        dest_container=Path(j["dest_container"]),
        src_manifest=Path(j["src_manifest"]) if j.get("src_manifest") else None,
        dest_manifest=Path(j["dest_manifest"]) if j.get("dest_manifest") else None,
        park_dir=Path(j["park_dir"]) if j.get("park_dir") else None,
        dest_is_ntfs=bool(j.get("dest_is_ntfs")),
    )
    if j.get("tree"):
        t.tree = Tree.from_json(j["tree"])
    else:
        t.tree = Tree(files=int(j.get("files") or 0), bytes=int(j.get("bytes") or 0))
    return t


def finish_journal(j: dict, *, on_phase=None, on_log=None) -> None:
    t = transfer_from_journal(j, on_phase=on_phase, on_log=on_log)
    t._register()
    t._cleanup()
    t.phase("done", "Interrupted transfer finished.")
    clear_journal()


def rollback_journal(j: dict, *, on_log=None) -> None:
    log = on_log or (lambda m: None)
    dest = Path(j["dest_payload"])
    if dest.exists():
        try:
            safety.assert_deletable(dest, config.SSD_ROOT, config.HD_ROOT)
            shutil.rmtree(dest)
            log(f"removed partial copy {dest}")
        except Exception as exc:
            log(f"could not remove {dest}: {exc}")
    park = Path(j["park_dir"]) if j.get("park_dir") else None
    if j.get("direction") == "to_hd" and park and park.is_dir():
        if not any(park.rglob("appmanifest_*.acf")):
            shutil.rmtree(park, ignore_errors=True)
            log(f"removed empty parking directory {park}")
    log("rollback complete — the game is untouched on its original disk")
    clear_journal()
