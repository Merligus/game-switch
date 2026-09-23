"""Entry point.  Runs the GUI by default; the CLI flags exist for testing."""
from __future__ import annotations

import argparse
import sys

from . import config, heroiclib, safety, steamlib, transfer
from .model import human


def all_games():
    return steamlib.scan() + heroiclib.scan()


def find_game(key: str):
    for g in all_games():
        if g.key == key or g.key.endswith(":" + key) or g.title.lower() == key.lower():
            return g
    return None


def cmd_list() -> int:
    games = sorted(all_games(), key=lambda g: (-g.size, g.title))
    if not games:
        print("no games found")
        return 1
    print(f"{'WHERE':<5} {'SIZE':>11}  {'LAUNCHER':<16} {'KEY':<26} TITLE")
    for g in games:
        print(f"{g.location_label:<5} {human(g.size):>11}  {g.launcher_label:<16} {g.key:<26} {g.title}")
    for label, root in ((config.SSD_LABEL, config.SSD_ROOT), (config.HD_LABEL, config.HD_ROOT)):
        print(f"\n{label}: {human(safety.free_bytes(root))} free of {human(safety.total_bytes(root))}")
    return 0


def cmd_doctor() -> int:
    print("Steam processes :", safety.steam_processes() or "none")
    print("Heroic processes:", safety.heroic_processes() or "none")
    for label, root in ((config.SSD_LABEL, config.SSD_ROOT), (config.HD_LABEL, config.HD_ROOT)):
        ok, err = safety.is_mounted_rw(root)
        wok, werr = safety.write_test(root / ".gameswitch-probe") if ok else (False, "skipped")
        info = safety.mount_for(root)
        print(f"{label:<12} {str(root):<34} fs={info[0] if info else '?':<6} rw={ok} write={wok} "
              f"free={human(safety.free_bytes(root))}")
        if err:
            print("   !", err)
        if not wok and ok:
            print("   !", werr)
    try:
        (config.HD_ROOT / ".gameswitch-probe").rmdir()
        (config.SSD_ROOT / ".gameswitch-probe").rmdir()
    except OSError:
        pass
    j = transfer.read_journal()
    print("journal:", f"{j['title']} phase={j['phase']} -> {transfer.recovery_options(j)}" if j else "clean")
    return 0


def cmd_switch(key: str, dry_run: bool, deep: bool) -> int:
    game = find_game(key)
    if not game:
        print(f"no such game: {key}", file=sys.stderr)
        return 2
    plan = transfer.plan_for(game)
    print("\n".join(plan.describe()))
    print()
    t = transfer.Transfer(
        game, deep_verify=deep, dry_run=dry_run,
        on_phase=lambda p, txt: print(f"== {p}: {txt}"),
        on_log=lambda m: print("   " + m),
        on_progress=lambda done, total, speed, eta: print(
            f"\r   {human(done)} / {human(total)}  {speed}  eta {eta}s      ", end="", flush=True),
    )
    try:
        t.run()
    except transfer.Cancelled:
        print("\ncancelled")
        return 1
    except Exception as exc:
        sys.stdout.flush()
        print(f"\nERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    print()
    return 0


def cmd_recover(action: str) -> int:
    j = transfer.read_journal()
    if not j:
        print("no interrupted transfer")
        return 0
    opts = transfer.recovery_options(j)
    print(f"interrupted: {j['title']} ({j['direction']}) phase={j['phase']} options={opts}")
    if action == "status":
        return 0
    log = lambda m: print("   " + m)
    if action == "rollback":
        if "rollback" not in opts:
            print("too late to roll back safely; use --recover finish", file=sys.stderr)
            return 2
        transfer.rollback_journal(j, on_log=log)
    elif action == "finish":
        transfer.finish_journal(j, on_phase=lambda p, t: print(f"== {p}: {t}"), on_log=log)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gameswitch", description=__doc__)
    ap.add_argument("--list", action="store_true", help="list games and exit")
    ap.add_argument("--doctor", action="store_true", help="print disk/launcher health")
    ap.add_argument("--switch", metavar="KEY", help="switch a game to the other disk")
    ap.add_argument("--dry-run", action="store_true", help="with --switch: show what would happen")
    ap.add_argument("--deep-verify", action="store_true", help="checksum every file after copying")
    ap.add_argument("--recover", choices=("status", "finish", "rollback"),
                    help="deal with an interrupted transfer")
    args = ap.parse_args(argv)

    if args.list:
        return cmd_list()
    if args.doctor:
        return cmd_doctor()
    if args.recover:
        return cmd_recover(args.recover)
    if args.switch:
        lock = safety.InstanceLock()
        if not lock.acquire():
            print("another GameSwitch instance is running", file=sys.stderr)
            return 3
        try:
            return cmd_switch(args.switch, args.dry_run, args.deep_verify)
        finally:
            lock.release()

    from .ui import run_gui
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
