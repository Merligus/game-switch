"""PySide6 interface: a game list with one switch button per row."""
from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from . import config, heroiclib, safety, steamlib, transfer
from .model import HD, SSD, Game, human

BANNER = "Do not open Steam or Heroic until this finishes."
DESTRUCTIVE_PHASES = {"register", "cleanup"}


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------

class TransferWorker(QtCore.QObject):
    phase = QtCore.Signal(str, str)
    # byte counts MUST be qlonglong: a plain Python `int` in a Signal maps to a
    # C++ 32-bit int, so any game over 2 GiB overflows shiboken, leaves a
    # dangling exception inside a cross-thread emit, and segfaults the process.
    progress = QtCore.Signal("qlonglong", "qlonglong", str, int)
    logged = QtCore.Signal(str)
    finished = QtCore.Signal(bool, str)

    def __init__(self, game: Game, deep_verify: bool) -> None:
        super().__init__()
        self.transfer = transfer.Transfer(
            game,
            deep_verify=deep_verify,
            on_phase=lambda p, t: self.phase.emit(p, t),
            on_progress=lambda d, t, s, e: self.progress.emit(d, t, s, e),
            on_log=lambda m: self.logged.emit(m),
        )

    @QtCore.Slot()
    def run(self) -> None:
        try:
            self.transfer.run()
            self.finished.emit(True, "")
        except transfer.Cancelled:
            self.finished.emit(False, "Cancelled — nothing was deleted.")
        except Exception as exc:
            self.finished.emit(False, str(exc))

    def cancel(self) -> None:
        self.transfer.cancel()


class RecoveryWorker(QtCore.QObject):
    phase = QtCore.Signal(str, str)
    logged = QtCore.Signal(str)
    finished = QtCore.Signal(bool, str)

    def __init__(self, journal: dict, action: str) -> None:
        super().__init__()
        self.journal = journal
        self.action = action

    @QtCore.Slot()
    def run(self) -> None:
        try:
            if self.action == "finish":
                transfer.finish_journal(
                    self.journal,
                    on_phase=lambda p, t: self.phase.emit(p, t),
                    on_log=lambda m: self.logged.emit(m))
            else:
                transfer.rollback_journal(self.journal, on_log=lambda m: self.logged.emit(m))
            self.finished.emit(True, "")
        except Exception as exc:
            self.finished.emit(False, str(exc))


# ---------------------------------------------------------------------------
# progress dialog
# ---------------------------------------------------------------------------

class ProgressDialog(QtWidgets.QDialog):
    def __init__(self, parent, title: str, subtitle: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(620)
        self._cancellable = True
        self._done = False

        lay = QtWidgets.QVBoxLayout(self)

        head = QtWidgets.QLabel(f"<b>{title}</b>")
        head.setTextFormat(QtCore.Qt.RichText)
        lay.addWidget(head)

        self.sub = QtWidgets.QLabel(subtitle)
        self.sub.setWordWrap(True)
        self.sub.setStyleSheet("color: palette(mid);")
        lay.addWidget(self.sub)

        warn = QtWidgets.QLabel(BANNER)
        warn.setStyleSheet(
            "background:#7f1d1d; color:#fee2e2; padding:8px; border-radius:6px; font-weight:600;")
        warn.setWordWrap(True)
        lay.addWidget(warn)

        self.phase_label = QtWidgets.QLabel("Starting…")
        lay.addWidget(self.phase_label)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFormat("%p%")
        lay.addWidget(self.bar)

        self.stats = QtWidgets.QLabel(" ")
        self.stats.setStyleSheet("color: palette(mid); font-family: monospace;")
        lay.addWidget(self.stats)

        self.log_toggle = QtWidgets.QPushButton("Show details")
        self.log_toggle.setCheckable(True)
        self.log_toggle.toggled.connect(self._toggle_log)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setVisible(False)
        self.log.setMinimumHeight(220)
        self.log.setStyleSheet("font-family: monospace; font-size: 11px;")
        lay.addWidget(self.log_toggle)
        lay.addWidget(self.log)

        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.close_btn = QtWidgets.QPushButton("Close")
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        btns.addWidget(self.cancel_btn)
        btns.addWidget(self.close_btn)
        lay.addLayout(btns)

    def _toggle_log(self, on: bool) -> None:
        self.log.setVisible(on)
        self.log_toggle.setText("Hide details" if on else "Show details")
        self.adjustSize()

    # -- slots --------------------------------------------------------------
    @QtCore.Slot(str, str)
    def on_phase(self, name: str, text: str) -> None:
        pretty = {"preflight": "Checking", "scan": "Reading the game folder",
                  "copy": "Copying", "verify": "Verifying", "register": "Updating launcher",
                  "cleanup": "Removing the old copy", "done": "Done",
                  "cancelled": "Cancelled"}.get(name, name)
        self.phase_label.setText(f"{pretty} — {text}" if text else pretty)
        if name in DESTRUCTIVE_PHASES:
            self._cancellable = False
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setToolTip("Cannot cancel while the launcher metadata is being updated")
        if name == "copy":
            self.bar.setRange(0, 1000)
        elif name == "verify":
            self.bar.setRange(0, 0)   # indeterminate: verifying gives no progress

    @QtCore.Slot("qlonglong", "qlonglong", str, int)
    def on_progress(self, done: int, total: int, speed: str, eta: int) -> None:
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(min(1000, int(done / total * 1000)))
        mins, secs = divmod(max(eta, 0), 60)
        self.stats.setText(f"{human(done)} / {human(total)}   {speed}   eta {mins:d}m{secs:02d}s")

    @QtCore.Slot(str)
    def on_log(self, msg: str) -> None:
        self.log.appendPlainText(msg)
        if msg.startswith("WARNING") and not self.log_toggle.isChecked():
            self.log_toggle.setChecked(True)

    @QtCore.Slot(bool, str)
    def mark_done(self, ok: bool, message: str) -> None:
        logging.info("transfer finished: ok=%s %s", ok, message)
        self._done = True
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        self.close_btn.setDefault(True)
        self.close_btn.setFocus()
        self.bar.setRange(0, 1000)
        if ok:
            self.close_btn.setText("Back to list")
            self.bar.setValue(1000)
            self.phase_label.setText("Done.")
            self.sub.setText("The game is now on the other disk.")
        elif message.startswith("Cancelled"):
            # a cancel is a clean outcome, not an error
            self.close_btn.setText("Back to list")
            self.bar.setValue(0)
            self.phase_label.setText("Cancelled.")
            self.sub.setText(message)
            self.sub.setStyleSheet("color:#fbbf24;")
        else:
            self.phase_label.setText("Failed.")
            self.sub.setText(message)
            self.sub.setStyleSheet("color:#f87171;")
            self.log_toggle.setChecked(True)
            self.log.appendPlainText("\n" + message)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if not self._done:
            event.ignore()
        else:
            super().closeEvent(event)


# ---------------------------------------------------------------------------
# main window
# ---------------------------------------------------------------------------

class MainWindow(QtWidgets.QMainWindow):
    COLUMNS = ["Game", "Launcher", "Size", "Location", ""]

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("GameSwitch")
        self.setWindowIcon(app_icon())
        self.resize(940, 620)
        self.games: list[Game] = []
        self.sort_col = 2
        self.sort_desc = True
        self._thread: QtCore.QThread | None = None
        self._worker = None

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 10)
        root.setSpacing(10)

        # --- disk bars ---
        disks = QtWidgets.QHBoxLayout()
        self.ssd_bar, ssd_box = self._disk_widget(config.SSD_LABEL)
        self.hd_bar, hd_box = self._disk_widget(config.HD_LABEL)
        disks.addWidget(ssd_box, 1)
        disks.addWidget(hd_box, 1)
        root.addLayout(disks)

        # --- toolbar ---
        bar = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Filter games…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.render)
        bar.addWidget(self.search, 1)
        self.deep = QtWidgets.QCheckBox("Deep verify")
        self.deep.setToolTip("Checksum every file after copying. Slower, but proves the copy is byte-identical.")
        bar.addWidget(self.deep)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self.reload)
        bar.addWidget(refresh)
        root.addLayout(bar)

        # --- table ---
        self.table = QtWidgets.QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        for c in (1, 2, 3, 4):
            hh.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(self._sort_by)
        root.addWidget(self.table, 1)

        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("padding:6px; border-radius:6px;")
        root.addWidget(self.status)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(3000)

        self.reload()
        QtCore.QTimer.singleShot(300, self.check_recovery)

    # -- widgets ------------------------------------------------------------
    def _disk_widget(self, label: str) -> tuple[QtWidgets.QProgressBar, QtWidgets.QWidget]:
        box = QtWidgets.QGroupBox(label)
        v = QtWidgets.QVBoxLayout(box)
        v.setContentsMargins(10, 6, 10, 8)
        bar = QtWidgets.QProgressBar()
        bar.setRange(0, 1000)
        bar.setTextVisible(True)
        v.addWidget(bar)
        return bar, box

    # -- data ---------------------------------------------------------------
    def reload(self) -> None:
        try:
            self.games = steamlib.scan() + heroiclib.scan()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Scan failed", str(exc))
            self.games = []
        self.render()
        self.refresh_status()

    def _sort_by(self, col: int) -> None:
        if col == len(self.COLUMNS) - 1:
            return
        if col == self.sort_col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = col, col == 2
        self.render()

    def render(self) -> None:
        needle = self.search.text().strip().lower()
        rows = [g for g in self.games if needle in g.title.lower() or needle in g.key.lower()]
        keys = {0: lambda g: g.title.lower(), 1: lambda g: g.launcher_label,
                2: lambda g: g.size, 3: lambda g: g.location}
        rows.sort(key=keys.get(self.sort_col, keys[0]), reverse=self.sort_desc)

        self.table.setRowCount(len(rows))
        for r, g in enumerate(rows):
            title = QtWidgets.QTableWidgetItem(g.title)
            title.setToolTip(str(g.payload))
            self.table.setItem(r, 0, title)
            self.table.setItem(r, 1, QtWidgets.QTableWidgetItem(g.launcher_label))
            size = QtWidgets.QTableWidgetItem(human(g.size))
            size.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.table.setItem(r, 2, size)
            loc = QtWidgets.QTableWidgetItem("SSD" if g.location == SSD else "HD")
            loc.setForeground(QtGui.QColor("#4ade80") if g.location == SSD else QtGui.QColor("#fbbf24"))
            loc.setToolTip(str(g.payload))
            self.table.setItem(r, 3, loc)

            btn = QtWidgets.QPushButton("→ HD" if g.location == SSD else "→ SSD")
            btn.setToolTip(
                f"Move {g.title} to the {'Windows HD (frees SSD space)' if g.location == SSD else 'SSD (fast)'}")
            btn.clicked.connect(lambda _=False, game=g: self.switch(game))
            self.table.setCellWidget(r, 4, btn)
        self.table.resizeRowsToContents()

    def refresh_status(self) -> None:
        for bar, root, label in ((self.ssd_bar, config.SSD_ROOT, config.SSD_LABEL),
                                 (self.hd_bar, config.HD_ROOT, config.HD_LABEL)):
            try:
                free = safety.free_bytes(root)
                total = safety.total_bytes(root)
            except OSError:
                bar.setFormat(f"{label}: not mounted")
                bar.setValue(0)
                continue
            used = total - free
            bar.setValue(int(used / total * 1000) if total else 0)
            bar.setFormat(f"{human(free)} free of {human(total)}")

        problems: list[str] = []
        steam = safety.steam_processes()
        heroic = safety.heroic_processes()
        if steam:
            problems.append(f"Steam is running ({len(steam)} processes) — Steam games cannot be switched.")
        if heroic:
            problems.append(f"Heroic is running ({len(heroic)} processes) — Heroic games cannot be switched.")
        for root, label in ((config.SSD_ROOT, config.SSD_LABEL), (config.HD_ROOT, config.HD_LABEL)):
            ok, err = safety.is_mounted_rw(root)
            if not ok:
                problems.append(err)

        if problems:
            self.status.setText("  ⚠  " + "\n  ⚠  ".join(problems))
            self.status.setStyleSheet(
                "padding:6px; border-radius:6px; background:#78350f; color:#fef3c7;")
        else:
            self.status.setText("  ✓  Steam and Heroic are closed, both disks are writable — ready to switch.")
            self.status.setStyleSheet(
                "padding:6px; border-radius:6px; background:#14532d; color:#dcfce7;")

        busy = self._thread is not None
        for r in range(self.table.rowCount()):
            w = self.table.cellWidget(r, 4)
            if not w:
                continue
            title_item = self.table.item(r, 0)
            game = next((g for g in self.games if g.title == (title_item.text() if title_item else "")), None)
            blocked = busy or (game is not None and bool(
                steam if game.launcher == "steam" else heroic))
            w.setEnabled(not blocked)

    # -- actions ------------------------------------------------------------
    def switch(self, game: Game) -> None:
        if self._thread is not None:
            return
        issues = safety.launcher_issues(game.launcher)
        blockers = [i for i in issues if i.level == "block"]
        if blockers and not self._offer_fix(blockers):
            return

        plan = transfer.plan_for(game)
        where = config.HD_LABEL if game.target == HD else config.SSD_LABEL
        need = safety.required_space(game.size)
        have = safety.free_bytes(plan.dest_container)
        detail = (f"<b>{game.title}</b><br>{human(game.size)}<br><br>"
                  f"<tt>{plan.src_payload}</tt><br>→ <tt>{plan.dest_payload}</tt><br><br>"
                  f"{where} has {human(have)} free (needs {human(need)}).")
        if game.launcher == "steam" and game.target == HD:
            detail += ("<br><br>While it is on the HD, Steam will list this game as "
                       "<i>not installed</i>. Its wine prefix and saves stay on the SSD.")
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(f"Move to {where}?")
        box.setTextFormat(QtCore.Qt.RichText)
        box.setText(detail)
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(QtWidgets.QMessageBox.Yes)
        if box.exec() != QtWidgets.QMessageBox.Yes:
            return

        dlg = ProgressDialog(self, f"Moving {game.title} to {where}",
                             f"{plan.src_payload}  →  {plan.dest_payload}")
        worker = TransferWorker(game, self.deep.isChecked())
        self._start(worker, dlg)

    def _offer_fix(self, blockers) -> bool:
        msg = "\n\n".join(b.message for b in blockers)
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Cannot switch yet")
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setText(msg)
        close_steam = any(b.fix == "close_steam" for b in blockers)
        if close_steam:
            btn = box.addButton("Close Steam", QtWidgets.QMessageBox.AcceptRole)
        box.addButton(QtWidgets.QMessageBox.Cancel)
        box.exec()
        if close_steam and box.clickedButton() is btn:
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            ok = safety.close_steam()
            QtWidgets.QApplication.restoreOverrideCursor()
            if not ok:
                QtWidgets.QMessageBox.warning(self, "Steam is still running",
                                              "Steam did not shut down. Close it manually and try again.")
            self.refresh_status()
            return ok
        return False

    def _start(self, worker, dlg: ProgressDialog) -> None:
        """Run `worker` on its own thread and show `dlg` until it is finished.

        Every receiver here is a QObject owned by the GUI thread, so Qt delivers
        the worker's signals as *queued* calls.  Connecting a bare lambda instead
        would give a direct connection and let the worker thread repaint widgets,
        which segfaults Qt.
        """
        thread = QtCore.QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.phase.connect(dlg.on_phase)
        if hasattr(worker, "progress"):
            worker.progress.connect(dlg.on_progress)
        worker.logged.connect(dlg.on_log)
        worker.finished.connect(dlg.mark_done)
        worker.finished.connect(thread.quit)

        if hasattr(worker, "cancel"):
            # called straight from the GUI thread: it only sets a flag and
            # terminates the rsync child, both safe across threads.  A Qt
            # connection would be queued into a thread that is not running an
            # event loop, so the click would be swallowed.
            dlg.cancel_btn.clicked.connect(lambda: worker.cancel())
        else:
            dlg.cancel_btn.setEnabled(False)

        self._thread, self._worker = thread, worker
        self.refresh_status()
        thread.start()
        try:
            dlg.exec()
        finally:
            # the dialog can only be dismissed after the worker reported back,
            # so this returns immediately -- it just guarantees the thread is
            # really gone before the last Python reference to it is dropped.
            thread.quit()
            if not thread.wait(15000):
                thread.terminate()
                thread.wait(2000)
            self._thread = None
            self._worker = None
            worker.deleteLater()
            thread.deleteLater()
        self.reload()

    def check_recovery(self) -> None:
        j = transfer.read_journal()
        if not j:
            return
        opts = transfer.recovery_options(j)
        arrow = "SSD → HD" if j["direction"] == "to_hd" else "HD → SSD"
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Interrupted transfer")
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setTextFormat(QtCore.Qt.RichText)
        if "finish" in opts:
            box.setText(
                f"<b>{j['title']}</b> ({arrow}) was interrupted at the <i>{j['phase']}</i> step.<br><br>"
                "The copy was already verified, so the only safe action is to finish the move.")
            go = box.addButton("Finish the move", QtWidgets.QMessageBox.AcceptRole)
            action = "finish"
        else:
            box.setText(
                f"<b>{j['title']}</b> ({arrow}) was interrupted at the <i>{j['phase']}</i> step.<br><br>"
                "The game is still intact on its original disk. Rolling back deletes the "
                "incomplete copy on the other disk.")
            go = box.addButton("Roll back", QtWidgets.QMessageBox.AcceptRole)
            action = "rollback"
        box.addButton("Ignore for now", QtWidgets.QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not go:
            return
        dlg = ProgressDialog(self, f"{action.title()}: {j['title']}", arrow)
        dlg.bar.setRange(0, 0)
        self._start(RecoveryWorker(j, action), dlg)


LOGFILE = config.LOG_DIR / "gameswitch.log"
ICON_FILE = Path(__file__).resolve().parent.parent / "icons" / "gameswitch.svg"


def app_icon() -> QtGui.QIcon:
    """Installed theme icon first, then the copy shipped next to the source."""
    icon = QtGui.QIcon.fromTheme("gameswitch")
    if icon.isNull() and ICON_FILE.is_file():
        icon = QtGui.QIcon(str(ICON_FILE))
    if icon.isNull():
        icon = QtGui.QIcon.fromTheme("exchange-positions")
    return icon


def _install_logging() -> None:
    """Record warnings, Qt messages and tracebacks to a file.

    Launched from the application menu there is no terminal, so without this
    every diagnostic (a RuntimeWarning, a Qt thread complaint) goes nowhere.
    """
    logging.basicConfig(
        filename=LOGFILE, filemode="a", level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s")
    logging.info("--- GameSwitch %s starting ---", __import__("gameswitch").__version__)
    logging.captureWarnings(True)

    def showwarning(message, category, filename, lineno, file=None, line=None):
        logging.warning("%s:%s: %s: %s", filename, lineno, category.__name__, message)

    warnings.showwarning = showwarning

    def qt_message(mode, context, message):
        level = {QtCore.QtMsgType.QtWarningMsg: logging.WARNING,
                 QtCore.QtMsgType.QtCriticalMsg: logging.ERROR,
                 QtCore.QtMsgType.QtFatalMsg: logging.CRITICAL}.get(mode, logging.INFO)
        logging.log(level, "Qt: %s", message)

    QtCore.qInstallMessageHandler(qt_message)


def _install_excepthook() -> None:
    """Show unexpected errors instead of letting them kill the window."""
    previous = sys.excepthook

    def hook(kind, value, tb):
        previous(kind, value, tb)
        logging.error("unhandled exception", exc_info=(kind, value, tb))
        try:
            QtWidgets.QMessageBox.critical(
                None, "GameSwitch — unexpected error",
                f"{kind.__name__}: {value}\n\nThe window stays open; your games were not touched "
                f"unless a transfer log said otherwise.\n\nDetails were written to {LOGFILE}")
        except Exception:
            pass

    sys.excepthook = hook


def run_gui() -> int:
    lock = safety.InstanceLock()
    app = QtWidgets.QApplication(sys.argv)
    _install_logging()
    _install_excepthook()
    app.setApplicationName("GameSwitch")
    app.setWindowIcon(app_icon())
    app.setDesktopFileName("gameswitch")
    if not lock.acquire():
        QtWidgets.QMessageBox.warning(None, "GameSwitch",
                                      "Another GameSwitch window is already open.")
        return 3
    try:
        win = MainWindow()
        win.show()
        return app.exec()
    finally:
        lock.release()
