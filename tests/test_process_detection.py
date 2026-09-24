#!/usr/bin/env python3
"""Launcher-detection regression tests.  Run: python3 tests/test_process_detection.py

This detection has broken twice, in opposite directions:

  * it reported our own rsync as a running launcher (rsync chdirs into the
    directory it writes into, which is how a running game is recognised);
  * fixing that blinded it completely, because the candidate's parent chain was
    matched against our whole ancestor set and `systemd --user` is an ancestor
    of every process in the session -- so the app cheerfully said "Steam and
    Heroic are closed" with Steam wide open.

A one-sided test passes in the second case. Every assertion here therefore comes
in a pair: a real launcher MUST be found, and our own helper MUST NOT be.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="gsw-procdet-"))
SSD, HD = TMP / "ssd", TMP / "hd"
os.environ.update(
    GAMESWITCH_SSD_ROOT=str(SSD),
    GAMESWITCH_HD_ROOT=str(HD),
    GAMESWITCH_STEAM_ROOT=str(TMP / "steamroot"),
    GAMESWITCH_HEROIC_CONFIG=str(TMP / "heroiccfg"),
    GAMESWITCH_STATE_DIR=str(TMP / "state"),
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gameswitch import config, safety  # noqa: E402

STEAM_GAME = config.SSD_STEAM_LIB / "steamapps" / "common" / "FakeGame"
HEROIC_GAME = config.SSD_HEROIC / "FakeGame"
FAILURES: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        FAILURES.append(what)


def detached(argv: list[str], cwd: Path) -> subprocess.Popen:
    """Start a process that is NOT one of our descendants.

    `setsid --fork` makes it reparent away from us, which is what a real Steam
    or Heroic launched from the desktop looks like.
    """
    return subprocess.Popen(["setsid", "--fork", *argv], cwd=str(cwd),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for(predicate, timeout: float = 6.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.15)
    return False


def main() -> int:
    for d in (STEAM_GAME, HEROIC_GAME, config.STEAM_ROOT, config.HEROIC_CONFIG):
        d.mkdir(parents=True, exist_ok=True)

    print("\n1. a process named `steam` that we did not spawn must be detected")
    fake_steam = TMP / "steam"                      # comm becomes "steam"
    shutil.copy(shutil.which("sleep"), fake_steam)
    os.chmod(fake_steam, 0o755)
    detached([str(fake_steam), "25"], cwd=TMP)
    found = wait_for(lambda: any(c == "steam" for _, c in safety.steam_processes()))
    check(found, "pgrep path finds an unrelated `steam` process")
    check(bool([i for i in safety.launcher_issues("steam") if i.level == "block"]),
          "a Steam switch is blocked while it runs")

    print("\n2. an unrelated process sitting in a game folder must be detected")
    detached([shutil.which("sleep"), "25"], cwd=STEAM_GAME)
    check(wait_for(lambda: len(safety.steam_processes()) >= 2),
          "cwd path finds a process inside the Steam library")
    detached([shutil.which("sleep"), "25"], cwd=HEROIC_GAME)
    check(wait_for(lambda: bool(safety.heroic_processes())),
          "cwd path finds a process inside the Heroic folder")

    print("\n3. ordinary session processes must NOT be mistaken for ours")
    session = []
    for e in Path("/proc").iterdir():
        if not e.name.isdigit():
            continue
        try:
            if e.stat().st_uid != os.getuid():
                continue
            comm = Path(f"/proc/{e.name}/comm").read_text().strip()
        except OSError:
            continue
        if comm in ("plasmashell", "kwin_wayland", "pipewire", "kded6", "dolphin"):
            session.append((int(e.name), comm))
    # our own ancestors (`systemd --user` among them) are legitimately ours, so
    # only genuine siblings belong in this check
    ancestors = safety._own_pids()
    session = [(p, c) for p, c in session if p not in ancestors]
    check(bool(session), f"found sibling session processes to test against ({len(session)})")
    wrong = [f"{p}({c})" for p, c in session if safety._is_ours(p)]
    check(not wrong, f"_is_ours() is False for unrelated session processes{' -- ' + str(wrong) if wrong else ''}")
    check(safety._is_ours(os.getpid()), "_is_ours() is True for this process")
    check(safety._is_ours(os.getppid()), "_is_ours() is True for our parent")

    print("\n4. our own rsync writing into a game folder must NOT be detected")
    src = TMP / "payload"
    src.mkdir(exist_ok=True)
    (src / "blob.bin").write_bytes(os.urandom(192 * 1024 * 1024))
    dest = config.HD_HEROIC / "FakeGame"
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["systemd-inhibit", "--what=sleep:idle", "--who=GameSwitch", "--why=test",
         "--mode=block", "--", "rsync", "-rt", str(src) + "/", str(dest) + "/"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    safety.register_child(proc.pid)
    ours_flagged, samples = set(), 0
    while proc.poll() is None:
        for pid, comm in safety.heroic_processes():
            if comm in ("rsync", "systemd-inhibit"):
                ours_flagged.add((pid, comm))
        samples += 1
        time.sleep(0.05)
    safety.unregister_child(proc.pid)
    check(samples > 0, f"sampled the detector while rsync ran ({samples} samples)")
    check(not ours_flagged, f"our own rsync is not reported{' -- ' + str(sorted(ours_flagged)) if ours_flagged else ''}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all process-detection checks passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        subprocess.run(["pkill", "-f", str(TMP)], capture_output=True)
        shutil.rmtree(TMP, ignore_errors=True)
    raise SystemExit(code)
