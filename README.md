# GameSwitch

Move Steam and Heroic games between a fast SSD and a big NTFS drive with one
button, so only the games you are actually playing take up SSD space.

    ./bin/gameswitch              # the app
    ./install.sh                  # desktop entry + icon into ~/.local/share

Requires Python 3, PySide6 and rsync. Nothing else — no pip install, no venv.

The two disks are set in `gameswitch/config.py` and default to
`/run/media/merligus/SSD Gamer` (ext4) and `/mnt/windows` (ntfs3). Every path
there can be overridden with an environment variable, so you can point the app
at your own drives (or at a fixture directory for testing) without editing code:

    GAMESWITCH_SSD_ROOT   GAMESWITCH_HD_ROOT      GAMESWITCH_STEAM_ROOT
    GAMESWITCH_HEROIC_CONFIG                      GAMESWITCH_STATE_DIR

## How a switch works

The two filesystems are not equivalent — ntfs3 cannot store Unix permission bits,
ownership or POSIX symlinks, and Valve does not support Steam libraries on NTFS.
GameSwitch sidesteps all of that by **never running a game from NTFS**.

    SSD (playing)                              HD (parked)
    ──────────────────────────────────────     ──────────────────────────────────
    <SSD>/SteamLibrary/steamapps/              /mnt/windows/GameSwitch/steam/<appid>/
      appmanifest_<appid>.acf          ───►      appmanifest_<appid>.acf
      common/<installdir>/             ───►      common/<installdir>/
      compatdata/<appid>/  stays on the SSD      .gameswitch.json

Moving a Steam game to the HD moves its `appmanifest_*.acf` too, so Steam lists it
as **not installed** while it is parked and can never launch, update or validate it
from NTFS. Switch it back before playing.

Wine prefixes (`compatdata/<appid>`) never leave ext4 — they need POSIX permissions
and hold your save games.

Heroic games are different: Heroic is happy to run from any path, so a switch is a
plain move plus a rewrite of `install_path` in Heroic's store JSON (backed up first
to `~/.local/state/gameswitch/backups/`). Wine prefixes are left where they are.

## Safety

Nothing is deleted until the copy has been made **and verified**. The launcher's
metadata changes last, so an interruption always leaves the game fully on one disk.

Before every switch:

* the owning launcher must be closed (Steam for Steam games, Heroic for Heroic
  games) — running games are detected by their `exe`/`cwd`, not by name matching;
* `/mnt/windows` must be mounted read-write, proven by an actual write test.
  A read-only NTFS mount almost always means Windows was shut down with Fast
  Startup on, or is hibernated — boot Windows and shut it down fully, or run
  `sudo ntfsfix -d /dev/sdc2`;
* the destination needs `size × 1.05 + 1 GiB` free;
* the destination must not already hold a leftover copy;
* every path is checked to be inside the two managed disks before any delete.
  Symlinks, mount points and anything near the filesystem root are refused.

The launcher check runs **again** immediately before the delete step. If you opened
Steam during the transfer, the old copy is kept and the app tells you so.

`systemd-inhibit` keeps the machine awake for the duration.

### Permissions across NTFS

NTFS cannot hold the executable bit, so before copying, the tree is recorded as
*dominant mode + exceptions* (for Terraria: one entry, not 16 038). The record is
stored twice — in `~/.local/state/gameswitch/modes/` and in `.gameswitch.json`
next to the parked game — and reapplied on the way back. If both are missing, the
app falls back to marking every ELF binary and `#!` script executable.

### If a transfer is interrupted

The app writes `~/.local/state/gameswitch/journal.json` as it goes and offers to
recover on next launch:

* interrupted **before** the copy was verified → *Roll back* deletes the partial
  copy; the game is untouched on its original disk;
* interrupted **after** → *Finish* completes the move. The remaining steps are
  idempotent, so running it twice is harmless.

From a terminal: `./bin/gameswitch --recover status|finish|rollback`.

### Logs

Launched from the application menu there is no terminal, so the GUI writes
warnings, Qt messages and any traceback to
`~/.local/state/gameswitch/logs/gameswitch.log`. Check it first if something
misbehaves.

### Manual recovery

Everything is plain files. To un-park a game by hand:

    mv "/mnt/windows/GameSwitch/steam/<appid>/common/<installdir>" \
       "/run/media/merligus/SSD Gamer/SteamLibrary/steamapps/common/"
    mv "/mnt/windows/GameSwitch/steam/<appid>/appmanifest_<appid>.acf" \
       "/run/media/merligus/SSD Gamer/SteamLibrary/steamapps/"
    chmod -R u+rwX,go+rX "/run/media/merligus/SSD Gamer/SteamLibrary/steamapps/common/<installdir>"

`.gameswitch.json` in the parking directory records the original permissions.

## CLI

    gameswitch                       # GUI
    gameswitch --list                # every game, its size and which disk it is on
    gameswitch --doctor              # disk/launcher health, pending recovery
    gameswitch --switch steam:4496690            # move to the other disk
    gameswitch --switch Upward --dry-run         # show the plan, touch nothing
    gameswitch --switch Upward --deep-verify     # checksum every file after copying
    gameswitch --recover status|finish|rollback

`--switch` accepts a key (`steam:4496690`, `heroic:epic:<id>`), a bare appid, or an
exact title.

## What is deliberately ignored

* `/mnt/windows/SteamLibrary/steamapps/common` — those 17 folders belong to the
  *Windows* Steam install and have no Linux appmanifests, so Linux Steam cannot see
  them and copying them to the SSD would not change that.
* Steam runtimes and tools (Proton, Steam Linux Runtime, Steamworks redistributables).
* Folders in `common/` with no `appmanifest_*.acf` (leftovers from old installs).
* `shadercache/` — it stays on the SSD and regenerates on its own.

## Tests

    python3 tests/test_process_detection.py

Checks that a real Steam or Heroic process is found *and* that the app's own
rsync is not — both directions, because a detector that finds nothing satisfies
either half on its own.

## Layout

    gameswitch/config.py      paths and policy, all overridable by env vars
    gameswitch/vdf.py         read-only parser for Valve's ACF/VDF format
    gameswitch/steamlib.py    Steam library and parking-area discovery
    gameswitch/heroiclib.py   Heroic store JSON read + atomic install_path rewrite
    gameswitch/safety.py      preflight checks, process detection, delete guards
    gameswitch/transfer.py    scan / copy / verify / register / delete + recovery
    gameswitch/ui.py          PySide6 window and progress dialog
    icons/gameswitch.svg      application icon
