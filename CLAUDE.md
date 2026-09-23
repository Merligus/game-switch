# GameSwitch — context for future sessions

Moves Steam and Heroic games between an ext4 SSD and an ntfs3 drive. PySide6 +
rsync, no third-party Python packages. Read `README.md` first for the user-facing
behaviour; this file records the *why* and the traps that cost real debugging.

## Core design decisions

**Steam parks by deregistering.** Moving a Steam game to the HD moves its
`appmanifest_<appid>.acf` alongside the payload into
`/mnt/windows/GameSwitch/steam/<appid>/`. Steam then lists the game as *not
installed* and can never launch, update or validate it from NTFS. This was a
deliberate choice over registering the HD as a second Steam library: Valve does
not support NTFS libraries, update staging uses cross-filesystem renames that
fail, and the user's Windows Steam library on the same disk already has
colliding folder names (`Don't Starve Together` exists in both).

**Heroic does not deregister.** Heroic launches whatever is at `install_path`,
so a switch is a plain move plus an atomic rewrite of that field in its store
JSON. Heroic games stay playable on either disk. `executable` is stored relative
to `install_path`, so it needs no patching. Wine prefixes live in a separate
directory and are never moved.

**Wine prefixes never leave ext4.** `steamapps/compatdata/<appid>` holds save
games and needs POSIX permissions. It stays on the SSD whatever happens to the
payload. Same for `shadercache`, which regenerates anyway.

**Ordering is what makes it safe.** Copy, verify, *then* change the launcher's
metadata, *then* delete the source. An interruption therefore always leaves the
game fully on one disk. The launcher check is re-run immediately before the
delete, so opening Steam mid-transfer keeps the old copy instead of losing it.

## Traps — every one of these was a real bug

**`QtCore.Signal(int, ...)` is a 32-bit C++ `int`.** Byte counts above 2 GiB
overflow shiboken, which leaves a dangling Python exception inside a cross-thread
emit and **segfaults the process**. Any signal carrying a file size must use
`"qlonglong"`, on both the `Signal` and the `@Slot` decorator. Symptom: a
`RuntimeWarning: libshiboken: Overflow` in the journal, then SIGSEGV, on every
game over 2 GiB while small games work fine.

**Never connect a bare lambda to a worker-thread signal.** With no receiver
QObject, Qt picks a *direct* connection and the slot body runs on the worker
thread. If it touches widgets, Qt crashes. Connect to a bound method of a
GUI-thread QObject so the call is queued.

**Cancel must not go through a Qt connection.** The worker thread has no event
loop while `run()` is executing, so a queued `cancel` is never delivered and the
button silently does nothing. Call `worker.cancel()` directly from a GUI-thread
lambda — it only sets a flag and terminates the rsync child, both thread-safe.

**`pgrep -f` matches this process.** A pattern in our own argv makes the app
detect itself as a running launcher. Detect running games by walking `/proc` and
comparing each process's `exe`/`cwd` against the library roots, and use
`pgrep -x` (exact comm) for the clients. Exclude the whole ancestor chain.

**`~/.steam/steam.pid` is routinely stale.** Verify the pid is alive *and* that
its `comm` looks like Steam before believing it.

**`assert_deletable` must test `is_symlink()` before `resolve()`.** `resolve()`
follows the link, so the check can never fire and `rmtree` would eat the target.

**NTFS loses permissions.** rsync to ntfs3 must pass `--no-perms --no-owner
--no-group`, and `-rt` rather than `-a` because POSIX symlinks do not survive.
Permissions are recorded as *dominant mode + exceptions* (Terraria is 16 038
files, one exception) and stored twice: in the state dir and in
`.gameswitch.json` beside the parked game. Recovery must carry the tree through
the journal too, or an idempotent re-run overwrites the good sidecar with an
empty one.

**Heroic DLCs share the parent game's `install_path`.** They get their own entry
in `installed.json` with an empty `executable`. `scan()` collapses them into one
row and `rewrite_install_path` repoints every entry on that path, otherwise the
DLC ends up pointing at a directory that no longer exists.

**Steam's `SizeOnDisk` is not the real byte count.** Don't Starve Together
reports 4 297 870 823 in the manifest and 4 304 766 947 on disk. Verification
compares against a fresh scan, never the manifest.

**Dry runs must leave no trace.** `write_test` creates directories to probe
writability; it removes any it created.

## Testing

    ./bin/gameswitch --list                        # expect one row per real game
    ./bin/gameswitch --doctor                      # disks, launchers, pending recovery
    ./bin/gameswitch --switch <key> --dry-run      # full plan, writes nothing
    ./bin/gameswitch --switch <key> --deep-verify  # checksum every file

Use the smallest real game for round trips. Verify a round trip with
`sha256sum` over every file *and* `find -printf '%m %y %p\n'` for the permission
bits — a copy that is byte-identical but lost its executable bit is still broken.

The GUI can be driven head-less with `QT_QPA_PLATFORM=offscreen`, which exercises
the same widget and threading code paths. Install
`QtCore.qInstallMessageHandler` in such a test and fail on any message
mentioning a thread: those warnings precede the segfaults.

Steam's own `~/.local/share/Steam/logs/content_log.txt` is the authority on what
happened to an app — it records every install, uninstall and state change with
timestamps. Check it before concluding this app lost someone's data.

## Things deliberately not done

Windows Steam's own games under `/mnt/windows/SteamLibrary` are ignored: they
have no Linux appmanifests, so copying them to the SSD would not make Steam see
them. Steam runtimes and Proton are filtered out of the list. `shadercache` is
left alone.
