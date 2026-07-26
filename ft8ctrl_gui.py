#!/usr/bin/env python3
#
# BSD 3-Clause License
#
# Copyright (c) 2023, Fred W6BSD
# All rights reserved.
#
"""Qt GUI front-end for ft8ctrl - a small WSJT-X companion utility, not a full
dashboard. Tabbed rather than split-paned, so it stays compact on screen.

Requires PyQt6 (installed via `apt install python3-pyqt6` on Debian/Ubuntu rather
than pip, since it isn't in requirements.txt - see README for details).
"""

import logging
import sqlite3
import sys
from argparse import ArgumentParser
from collections import Counter
from datetime import datetime
from pathlib import Path
from threading import Thread

from PyQt6.QtCore import QSettings, Qt, QTimer
from PyQt6.QtGui import (QAction, QColor, QFontDatabase, QFontMetrics, QIcon,
                         QPainter, QPalette, QPen, QPixmap)
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QHeaderView, QLabel,
                             QMainWindow, QMenu, QMessageBox, QPushButton,
                             QSpinBox, QSplitter, QSystemTrayIcon,
                             QTableWidget, QTableWidgetItem, QTabWidget,
                             QVBoxLayout, QWidget)

from config import Config
from dashboard import DashboardLogHandler
from dbutils import get_worked
from ft8ctrl import Sequencer, setup
from plugins.base import MAX_SNR, MIN_SNR
from status import Status, mentions_call

REFRESH_MS = 500
GREEN = QColor("#2e7d32")
RED = QColor("#c62828")
# WSJT-X's own default highlight colors (Settings > Colours): green for "CQ in
# message", yellow for "Transmitted message".
CQ_BG = QColor("#90ee90")
CQ_FG = QColor("#000000")
# Matches the active-QSO banner's red, for decode lines that mention my own
# callsign - a report, RR73, or 73 addressed to me stands out the same way.
MYCALL_BG = RED
MYCALL_FG = QColor("#ffffff")
# Matches the idle/"Listening" banner's blue, for a reply from the other station
# addressed to me specifically - distinct from MYCALL (used for my own Tx).
REPLY_BG = QColor("#1976d2")
REPLY_FG = QColor("#ffffff")

ORG_NAME = "FT8Commander"
APP_NAME = "ft8ctrl_gui"
DEFAULT_SIZE = (480, 420)

# Tab indices, in the order they're added in MainWindow.__init__ - used to drive the
# live count/highlight badges on the tabs that track a running total. Activity
# shares the Attempts tab (see MainWindow.__init__) rather than getting its own
# index - the combined tab's badge tracks Attempts, the more meaningful of the two
# since it's persistent rather than cleared every selection cycle.
TAB_ATTEMPTS, TAB_WORKED = 1, 2
TAB_BASE_LABELS = {TAB_ATTEMPTS: "Attempts", TAB_WORKED: "Worked"}

ATTEMPT_COLOR = {"worked": GREEN, "broken": RED, "in progress": QColor("#1976d2")}

# Monitor greys out while the radio is keyed - nothing can be received then, so
# the :checked green is scoped to the enabled state and a disabled look wins.
MONITOR_STYLE = (
  "QPushButton:checked:enabled { background-color: #90ee90; font-weight: bold; }"
  "QPushButton:disabled { background-color: #dcdcdc; color: #9a9a9a; }"
)
# Enable Tx distinguishes "automation armed" from "actually on the air".
TX_ARMED_STYLE = "QPushButton:checked { background-color: #ff6b6b; font-weight: bold; }"
TX_ONAIR_STYLE = ("QPushButton:checked { background-color: #c62828; color: white; "
                  "font-weight: bold; }")


def build_icon():
  """A small radio-waves/antenna mark, drawn at runtime so the app has a real
  window/taskbar/tray icon without needing to ship a separate image asset."""
  pixmap = QPixmap(64, 64)
  pixmap.fill(Qt.GlobalColor.transparent)
  painter = QPainter(pixmap)
  painter.setRenderHint(QPainter.RenderHint.Antialiasing)

  painter.setPen(QPen(QColor("#2aa198"), 2))
  painter.setBrush(QColor("#073642"))
  painter.drawEllipse(2, 2, 60, 60)

  painter.setPen(QPen(QColor("#eee8d5"), 3))
  painter.setBrush(Qt.BrushStyle.NoBrush)
  for radius in (10, 18, 26):
    painter.drawArc(32 - radius, 40 - radius, radius * 2, radius * 2, 30 * 16, 120 * 16)

  painter.setPen(Qt.PenStyle.NoPen)
  painter.setBrush(QColor("#eee8d5"))
  painter.drawEllipse(29, 37, 6, 6)
  painter.end()
  return QIcon(pixmap)


COMPASS_POINTS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass_point(azimuth):
  """16-point compass abbreviation for a bearing in degrees, e.g. 45 -> 'NE'."""
  return COMPASS_POINTS[round(azimuth / 22.5) % 16]


class MainWindow(QMainWindow):

  def __init__(self, mycall, call_select=None, sequencer=None, db_name=None):
    super().__init__()
    self.mycall = mycall
    # Source for the QSO history pane. Kept optional so the window can still be
    # constructed (e.g. in tests) without a database behind it.
    self.db_name = Path(db_name).expanduser() if db_name else None
    self._history_session_count = None
    # Tracks the last applied Enable Tx styling so it is only swapped on a
    # transition rather than on every refresh tick.
    self._tx_on_air = None
    # Min SNR is editable from the GUI, but only for the first active selector -
    # `call_selector` in the config can list several, and editing "the" min SNR
    # only makes unambiguous sense for one of them at a time.
    selectors = call_select.call_select if call_select else None
    self.snr_selector = selectors[0] if selectors else None
    self.snr_selector_name = self.snr_selector.__class__.__name__ if self.snr_selector else None
    # tx_retries lives on the Sequencer itself (it's a top-level ft8ctrl: config
    # value, not per-selector like min_snr).
    self.sequencer = sequencer
    self.settings = QSettings(ORG_NAME, APP_NAME)
    self.setWindowTitle(f"FT8Commander-NG - {mycall}")
    self.setWindowIcon(build_icon())
    self.resize(*DEFAULT_SIZE)
    self._closing = False

    self._build_menu()

    central = QWidget()
    self.setCentralWidget(central)
    layout = QVBoxLayout(central)
    layout.setContentsMargins(4, 4, 4, 4)
    layout.setSpacing(3)

    self.header_label = QLabel()
    self.header_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    self.header_label.setWordWrap(True)
    self.header_label.setStyleSheet(
      "background-color: #073642; color: #eee8d5; padding: 4px; font-weight: bold;")
    layout.addWidget(self.header_label)

    qso_row = QHBoxLayout()
    self.qso_with_label = QLabel()
    self.qso_with_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    self.qso_with_label.setWordWrap(True)
    font = self.qso_with_label.font()
    font.setPointSize(font.pointSize() + 2)
    font.setBold(True)
    self.qso_with_label.setFont(font)
    qso_row.addWidget(self.qso_with_label, stretch=1)

    self.bearing_label = QLabel("")
    self.bearing_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    bearing_font = self.bearing_label.font()
    bearing_font.setPointSize(bearing_font.pointSize() + 1)
    bearing_font.setBold(True)
    self.bearing_label.setFont(bearing_font)
    self.bearing_label.setStyleSheet("padding: 3px;")
    qso_row.addWidget(self.bearing_label)

    # Fix both labels to the height of a 2-line block up front - the bearing label
    # switches between one line ("-") and two ("045° NE" / "1,234 km"), and without
    # a fixed height that resize shifts the whole row (and everything below it)
    # every time a QSO starts or ends. Sized off the taller of the two fonts (the
    # qso_with_label one, being larger) - it was previously sized off bearing_font
    # alone, which clipped descenders ('g', 'y'...) on the qso_with_label text.
    line_height = max(QFontMetrics(font).height(), QFontMetrics(bearing_font).height())
    row_height = line_height * 2 + 10
    self.qso_with_label.setFixedHeight(row_height)
    self.bearing_label.setFixedHeight(row_height)
    layout.addLayout(qso_row)

    layout.addLayout(self._build_controls())

    self.tabs = QTabWidget()
    layout.addWidget(self.tabs, stretch=1)
    self._build_tabs()

    # Tracks the last-seen count per tab, so a running total that increases while
    # you're looking at a *different* tab can flag that tab as having new activity.
    # Connected only now, after all tabs exist and this state is ready - connecting
    # earlier fires immediately when the first tab is added, before it's ready.
    self._default_tab_color = self.tabs.tabBar().tabTextColor(0)
    self._tab_counts = dict.fromkeys(TAB_BASE_LABELS, 0)
    self.tabs.currentChanged.connect(self._on_tab_changed)

    self._build_status_bar()
    self._build_tray()
    self._restore_geometry()

    self.timer = QTimer(self)
    self.timer.timeout.connect(self.refresh)
    self.timer.start(REFRESH_MS)
    self.refresh()

  # -- One-time construction ------------------------------------------------

  def _build_tabs(self):
    """Populate the tab widget. Each tab pairs a live view with the persistent
    record behind it, split vertically."""
    mono_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)

    self.decode_window_table = self._make_table(
      ["Time", "SNR", "Freq(Hz)", "Mode", "Message"], mono_font)
    # Bottom pane, similar to WSJT-X's own Rx Frequency window: everything
    # mentioning my call plus every decode (including the original CQ) from a
    # station whose CQ I've answered this session - not just the current QSO.
    self.mycall_table = self._make_table(
      ["Time", "SNR", "Freq(Hz)", "Mode", "Message"], mono_font)
    self.tabs.addTab(
      self._tab_page(self._split(self.decode_window_table, self.mycall_table, 3, 1)),
      "Decode")

    # Activity (top) is the cycle-scoped "what was considered and why";
    # Attempts (bottom) is the persistent "what was called and how it went".
    self.activity_table = self._make_table(
      ["Time", "SNR", "Freq(Hz)", "Mode", "Message", "Result"])
    self.activity_stats_label = self._stats_label()
    self.attempts_table = self._make_table(
      ["Time", "Call", "Grid", "Dist(km)", "Country", "SNR", "Band", "Selector", "Result"],
      mono_font)
    self.attempts_stats_label = self._stats_label()
    self.tabs.addTab(self._tab_page(self._split(
      self._tab_page(self.activity_table, self.activity_stats_label),
      self._tab_page(self.attempts_table, self.attempts_stats_label), 1, 2)), "Attempts")

    # This session's QSOs on top, every QSO ever logged underneath.
    worked_columns = ["Date/Time", "Call", "Grid", "Dist(km)", "Country", "Band",
                      "Freq(MHz)", "RSTs", "RSTr"]
    self.worked_table = self._make_table(worked_columns)
    self.worked_stats_label = self._stats_label()
    self.history_table = self._make_table(worked_columns)
    self.history_stats_label = self._stats_label()
    self.tabs.addTab(self._tab_page(self._split(
      self._tab_page(self.worked_table, self.worked_stats_label),
      self._tab_page(self.history_table, self.history_stats_label), 1, 2)), "Worked")

  @staticmethod
  def _split(top, bottom, top_stretch, bottom_stretch):
    splitter = QSplitter(Qt.Orientation.Vertical)
    splitter.addWidget(top)
    splitter.addWidget(bottom)
    splitter.setStretchFactor(0, top_stretch)
    splitter.setStretchFactor(1, bottom_stretch)
    return splitter

  def _build_menu(self):
    file_menu = self.menuBar().addMenu("&File")
    exit_action = QAction("E&xit", self)
    exit_action.triggered.connect(self._quit)
    file_menu.addAction(exit_action)

    view_menu = self.menuBar().addMenu("&View")
    self.dark_mode_action = QAction("&Dark Mode", self, checkable=True)
    self.dark_mode_action.toggled.connect(self._toggle_dark_mode)
    view_menu.addAction(self.dark_mode_action)

    session_action = QAction("&Session Info...", self)
    session_action.triggered.connect(self._show_session_info)
    view_menu.addAction(session_action)

    help_menu = self.menuBar().addMenu("&Help")
    about_action = QAction("&About", self)
    about_action.triggered.connect(self._show_about)
    help_menu.addAction(about_action)

  def _build_controls(self):
    row = QHBoxLayout()
    row.setSpacing(4)

    # "Monitor" is a real control on OUR side of the link: toggling it off stops
    # the sequencer from polling/sending to WSJT-X, so a closed WSJT-X can't
    # generate connection errors. (It does not command WSJT-X's own Monitor
    # button - WSJT-X has no documented UDP command for that.) "Decode" stays a
    # read-only indicator mirroring WSJT-X's reported decoding state. "Enable Tx"
    # drives our automation pause.
    self.monitor_button = QPushButton("Monitor")
    self.monitor_button.setCheckable(True)
    self.monitor_button.setToolTip(
      "When on, poll WSJT-X for decodes and drive contacts. Turn off to stop all "
      "communication with WSJT-X (e.g. when WSJT-X is closed).")
    self.monitor_button.setStyleSheet(MONITOR_STYLE)
    self.monitor_button.clicked.connect(self._toggle_monitoring)
    row.addWidget(self.monitor_button)

    self.decode_button = QPushButton("Decode")
    self.decode_button.setCheckable(True)
    self.decode_button.setEnabled(False)
    self.decode_button.setStyleSheet(
      "QPushButton:checked { background-color: #ffd54f; font-weight: bold; }")
    row.addWidget(self.decode_button)

    self.enable_tx_button = QPushButton("Enable Tx")
    self.enable_tx_button.setCheckable(True)
    self.enable_tx_button.setStyleSheet(TX_ARMED_STYLE)
    self.enable_tx_button.clicked.connect(self._toggle_paused)
    row.addWidget(self.enable_tx_button)

    row.addStretch(1)

    if self.snr_selector is not None:
      row.addWidget(QLabel(f"Min SNR ({self.snr_selector_name}):"))
      self.min_snr_spin = QSpinBox()
      self.min_snr_spin.setRange(MIN_SNR, MAX_SNR)
      self.min_snr_spin.setValue(self.snr_selector.min_snr)
      self.min_snr_spin.setToolTip(
        "Candidates below this SNR are rejected. Applies immediately and is saved "
        f"back to ft8ctrl.yaml under {self.snr_selector_name}.")
      self.min_snr_spin.editingFinished.connect(self._update_min_snr)
      row.addWidget(self.min_snr_spin)

    if self.sequencer is not None:
      row.addWidget(QLabel("Tx Retries:"))
      self.tx_retries_spin = QSpinBox()
      self.tx_retries_spin.setRange(1, 20)
      self.tx_retries_spin.setValue(self.sequencer.tx_retries)
      self.tx_retries_spin.setToolTip(
        "How many times to repeat the same message before giving up. Applies "
        "immediately and is saved back to ft8ctrl.yaml.")
      self.tx_retries_spin.editingFinished.connect(self._update_tx_retries)
      row.addWidget(self.tx_retries_spin)

    return row

  def _build_status_bar(self):
    self.statusBar().setStyleSheet("QStatusBar { font-size: 9pt; }")
    # addWidget (not addPermanentWidget) puts this on the left side of the status
    # bar, giving connection context (which UDP address/port WSJT-X is expected
    # on) next to the "last seen"/packet-count widgets on the right.
    self.status_endpoint_label = QLabel()
    self.statusBar().addWidget(self.status_endpoint_label)
    self.status_endpoint_label.setContentsMargins(4, 0, 8, 0)

    self.status_wsjtx_label = QLabel()
    self.status_packets_label = QLabel()
    for label in (self.status_wsjtx_label, self.status_packets_label):
      self.statusBar().addPermanentWidget(label)
      label.setContentsMargins(0, 0, 8, 0)

  def _build_tray(self):
    if not QSystemTrayIcon.isSystemTrayAvailable():
      self.tray = None
      return
    self.tray = QSystemTrayIcon(build_icon(), self)
    self.tray.setToolTip(f"FT8Commander-NG - {self.mycall}")

    # Qt docs: QSystemTrayIcon::setContextMenu() does not take ownership of the
    # menu - without a parent here, PyQt garbage-collects the underlying menu
    # (and its actions) once this method returns, since nothing else on the
    # Python side keeps it alive, silently breaking every item including Exit.
    menu = QMenu(self)
    show_action = QAction("Show/Hide", self)
    show_action.triggered.connect(self._toggle_visibility)
    menu.addAction(show_action)
    tx_action = QAction("Enable Tx", self)
    tx_action.triggered.connect(self._toggle_paused)
    menu.addAction(tx_action)
    menu.addSeparator()
    quit_action = QAction("Exit", self)
    quit_action.triggered.connect(self._quit)
    menu.addAction(quit_action)
    self.tray.setContextMenu(menu)

    self.tray.activated.connect(self._on_tray_activated)
    self.tray.show()

  @staticmethod
  def _make_table(headers, font=None):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    # Keep every column sized to its widest current content automatically; the
    # last column still stretches to fill any leftover space rather than leaving
    # a gap. Columns that don't fit the tab's width scroll horizontally instead
    # of forcing the window wider.
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    table.verticalHeader().setVisible(False)

    if font is None:
      font = table.font()
    font.setPointSize(max(8, font.pointSize() - 1))
    table.setFont(font)
    table.horizontalHeader().setFont(font)
    table.verticalHeader().setDefaultSectionSize(18)
    table.setStyleSheet("QTableWidget::item { padding: 0px; }")
    return table

  @staticmethod
  def _tab_page(table, stats_label=None):
    page = QWidget()
    vbox = QVBoxLayout(page)
    vbox.setContentsMargins(2, 2, 2, 2)
    vbox.addWidget(table)
    if stats_label:
      vbox.addWidget(stats_label)
    return page

  @staticmethod
  def _stats_label():
    label = QLabel()
    label.setWordWrap(True)
    return label

  @staticmethod
  def _fill(table, rows, row_bg=None):
    table.setRowCount(len(rows))
    for row, cells in enumerate(rows):
      for col, (text, color) in enumerate(cells):
        item = QTableWidgetItem(text)
        if color:
          item.setForeground(color)
        if row_bg and row_bg[row]:
          item.setBackground(row_bg[row])
        table.setItem(row, col, item)
    # Rows are ordered oldest-first, newest-last (see each _refresh_* caller), so
    # keep the view pinned to the newest entry instead of leaving it wherever the
    # scrollbar happened to be (or resetting to the top) after every refresh.
    table.scrollToBottom()

  def _update_tab_badge(self, index, count):
    self.tabs.setTabText(index, f"{TAB_BASE_LABELS[index]} ({count})")
    if count > self._tab_counts[index] and self.tabs.currentIndex() != index:
      self.tabs.tabBar().setTabTextColor(index, RED)
    self._tab_counts[index] = count

  # -- Window/settings persistence -------------------------------------------

  def _restore_geometry(self):
    geometry = self.settings.value("geometry")
    if geometry:
      self.restoreGeometry(geometry)
    tab_index = self.settings.value("current_tab", 0, type=int)
    if 0 <= tab_index < self.tabs.count():
      self.tabs.setCurrentIndex(tab_index)
    dark = self.settings.value("dark_mode", False, type=bool)
    self.dark_mode_action.setChecked(dark)

  # Qt calls this by name, so the camelCase spelling is mandatory.
  def closeEvent(self, event):  # noqa: N802  # pylint: disable=invalid-name
    # With a tray icon available, the window's close button just hides it rather
    # than quitting - matches how a background utility is expected to behave.
    # File > Exit / the tray menu's Exit call _quit() first, which is what
    # actually lets this fall through to a real close.
    if self.tray is not None and not self._closing:
      event.ignore()
      self.hide()
      return
    self.settings.setValue("geometry", self.saveGeometry())
    self.settings.setValue("current_tab", self.tabs.currentIndex())
    self.settings.setValue("dark_mode", self.dark_mode_action.isChecked())
    super().closeEvent(event)

  # -- Menu/control actions --------------------------------------------------

  def _quit(self):
    self._closing = True
    # self.close() alone isn't enough here: with a tray icon, the window is
    # normally already hidden (that's the whole point of minimizing to tray), and
    # Qt's quitOnLastWindowClosed only ends the app on a visible->closed
    # transition. Closing an already-hidden window doesn't trigger that, so
    # Exit would silently do nothing - quit the application explicitly instead.
    self.close()
    QApplication.instance().quit()

  def _toggle_visibility(self):
    if self.isVisible():
      self.hide()
    else:
      self.show()
      self.raise_()
      self.activateWindow()

  def _on_tray_activated(self, reason):
    if reason == QSystemTrayIcon.ActivationReason.Trigger:
      self._toggle_visibility()

  def _on_tab_changed(self, index):
    self.tabs.tabBar().setTabTextColor(index, self._default_tab_color)

  @staticmethod
  def _toggle_dark_mode(checked):
    app = QApplication.instance()
    if checked:
      palette = QPalette()
      palette.setColor(QPalette.ColorRole.Window, QColor(45, 45, 45))
      palette.setColor(QPalette.ColorRole.WindowText, QColor(220, 220, 220))
      palette.setColor(QPalette.ColorRole.Base, QColor(30, 30, 30))
      palette.setColor(QPalette.ColorRole.AlternateBase, QColor(45, 45, 45))
      palette.setColor(QPalette.ColorRole.Text, QColor(220, 220, 220))
      palette.setColor(QPalette.ColorRole.Button, QColor(45, 45, 45))
      palette.setColor(QPalette.ColorRole.ButtonText, QColor(220, 220, 220))
      palette.setColor(QPalette.ColorRole.Highlight, QColor(38, 79, 120))
      palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
      app.setStyle("Fusion")
      app.setPalette(palette)
    else:
      app.setStyle("Fusion")
      app.setPalette(app.style().standardPalette())

  def _toggle_paused(self):
    Status().set_paused(not Status().is_paused())

  def _toggle_monitoring(self):
    Status().set_monitoring(not Status().is_monitoring())

  def _update_min_snr(self):
    value = self.min_snr_spin.value()
    if value == self.snr_selector.min_snr:
      return
    # Live effect: the running selector filters with this attribute directly on
    # every selection cycle, so this takes effect immediately, no restart needed.
    self.snr_selector.min_snr = value
    Config().save_value(self.snr_selector_name, 'min_snr', value)

  def _update_tx_retries(self):
    value = self.tx_retries_spin.value()
    if value == self.sequencer.tx_retries:
      return
    # Live effect: the running Sequencer reads this attribute directly on its
    # next retry check, so this takes effect immediately, no restart needed.
    self.sequencer.tx_retries = value
    Config().save_value('ft8ctrl', 'tx_retries', value)

  def _show_about(self):
    QMessageBox.about(
      self, "About FT8Commander-NG",
      "FT8Commander-NG\n\n"
      "WSJT-X FT8/FT4 automation.\n"
      "Original project, including DXEntity.py, by Fred Cirera (W6BSD).\n"
      "Maintained by Paul Miskovsky (VE3EXR).\n"
      "https://github.com/0x9900/FT8Commander"
    )

  def _show_session_info(self):
    data = Status().snapshot()
    selectors = ', '.join(data['selectors']) if data['selectors'] else '-'
    QMessageBox.information(
      self, "Session Info",
      f"My call: {self.mycall}\n"
      f"Grid: {data['grid'] or '-'}\n"
      f"Max retries: {data['max_retries'] or '-'}\n"
      f"Selectors: {selectors}\n"
      f"Blacklist: {data['blacklist_size']} calls\n"
      f"WSJT-X endpoint: {data['wsjt_endpoint'] or '-'}\n"
      f"Logger relay: {data['relay_endpoint'] or 'not configured'}"
    )

  # -- Live refresh -----------------------------------------------------------

  def refresh(self):
    data = Status().snapshot()
    self._refresh_header(data)
    self._refresh_qso_with(data)
    self._refresh_controls(data)
    self._refresh_decode_window(data)
    self._refresh_attempts(data)
    self._refresh_activity(data)
    self._refresh_worked(data)
    self._refresh_status_bar(data)

  def _refresh_header(self, data):
    band = f"{data['band']}m" if data['band'] else '-'

    # `paused` is our own automation's state (the Enable Tx button). WSJT-X's own
    # self-reported `tx_enabled` turns out not to be a useful idle/ready signal:
    # it's False the entire time we're idle and only flips True the moment we
    # actually start calling someone, so it's redundant with `transmitting` and
    # was previously showing a permanent, meaningless "Tx disabled" caveat.
    if not data['monitoring']:
      activity = "Monitoring stopped"
    elif data['paused']:
      activity = "Automation paused"
    elif data['current_call']:
      # Kept brief here - retry progress and exchange stage are already shown in
      # the colored QSO banner below, so this line just needs the at-a-glance state.
      activity = f"Working {data['current_call']}"
    else:
      activity = "Idle"

    self.header_label.setText(
      f"{self.mycall}  {band}  {data['mode'] or '-'}  |  {activity}")

  def _refresh_qso_with(self, data):
    if not data['monitoring']:
      # Not talking to WSJT-X at all - muted, distinct from both an active QSO and
      # the "listening" idle state (we're not even listening).
      self.qso_with_label.setText("Monitoring stopped")
      self.qso_with_label.setStyleSheet("padding: 3px; color: gray;")
      self.bearing_label.setVisible(False)
    elif data['current_call']:
      color = "#2e7d32" if data['worked_before'] else "#c62828"
      verb = "Calling" if data['transmitting'] else "Waiting on"
      stage = f"\n{data['current_stage']}" if data['current_stage'] else ""
      self.qso_with_label.setText(
        f"{verb}: {data['current_call']} "
        f"({data['current_retry']}/{data['max_retries']}){stage}")
      self.qso_with_label.setStyleSheet(
        f"background-color: {color}; color: white; padding: 3px;")
      azimuth, distance = data['current_azimuth'], data['current_distance']
      if azimuth is not None:
        bearing = f"{azimuth:03.0f}° {compass_point(azimuth)}"
        if distance is not None:
          bearing += f"\n{distance:,.0f} km"
        self.bearing_label.setText(bearing)
        self.bearing_label.setVisible(True)
      else:
        self.bearing_label.setVisible(False)
    elif data['paused']:
      # Deliberately muted/plain - distinct from genuinely idle below, so pausing
      # automation doesn't look the same as it actively listening and waiting.
      self.qso_with_label.setText("Automation paused")
      self.qso_with_label.setStyleSheet("padding: 3px; color: gray;")
      self.bearing_label.setVisible(False)
    else:
      # Idle isn't "nothing happening" - still actively listening for CQs, so
      # give it a color (matching the same visual weight as the active-QSO
      # banner) instead of looking inert.
      last_heard = "Listening"
      if data['decodes']:
        ts, call = data['decodes'][0][0], data['decodes'][0][1]
        age = (datetime.utcnow() - ts).total_seconds()
        last_heard = f"Listening (last heard {call}, {age:.0f}s ago)"
      self.qso_with_label.setText(last_heard)
      self.qso_with_label.setStyleSheet(
        "background-color: #1976d2; color: white; padding: 3px;")
      self.bearing_label.setVisible(False)

  def _refresh_controls(self, data):
    # Only treat Tx as live while WSJT-X is still talking to us. If it dies
    # mid-transmission its last reported state stays Transmitting=True forever,
    # and keying off that alone would leave Monitor permanently disabled - the
    # one button you need in exactly that situation.
    alive = bool(data['wsjtx_seen']) and \
        (datetime.utcnow() - data['wsjtx_seen']).total_seconds() < 10
    on_air = data['transmitting'] and alive

    self.monitor_button.setChecked(data['monitoring'])
    # Nothing can be received while the radio is keyed, so Monitor greys out for
    # the duration of the transmission, as WSJT-X's own does.
    self.monitor_button.setEnabled(not on_air)
    self.decode_button.setChecked(data['decoding'])
    self.enable_tx_button.setChecked(not data['paused'])
    # Restyle only on transitions - this runs twice a second.
    if on_air is not self._tx_on_air:
      self._tx_on_air = on_air
      self.enable_tx_button.setStyleSheet(TX_ONAIR_STYLE if on_air else TX_ARMED_STYLE)

  def _decode_row(self, ts, snr, delta_freq, mode, message, is_cq, call, distinguish_reply=False):
    mentions_me = mentions_call(message, self.mycall)
    # In My Traffic, split "mentions me" into two cases: a reply from the other
    # station addressed to me (blue, matching the idle/"Listening" banner) versus
    # my own outgoing Tx (red, matching the active-QSO banner). The plain Decode
    # tab doesn't make this distinction (it never even shows my own Tx).
    if distinguish_reply and mentions_me and call != self.mycall:
      fg, bg = REPLY_FG, REPLY_BG
    elif mentions_me:
      fg, bg = MYCALL_FG, MYCALL_BG
    elif is_cq:
      fg, bg = CQ_FG, CQ_BG
    else:
      fg, bg = None, None
    row = [
      (ts.strftime('%H:%M:%S'), fg),
      (str(snr) if snr is not None else 'Tx', fg),
      (str(delta_freq), fg),
      (mode, fg),
      (message, fg),
    ]
    return row, bg

  def _refresh_decode_window(self, data):
    rows, row_bg = [], []
    # WSJT-X's own Band Activity window lists decodes oldest-first, newest at the
    # bottom - our deque stores newest-first, so reverse just for display here.
    for ts, snr, delta_freq, mode, message, is_cq, call in reversed(data['decode_window']):
      row, bg = self._decode_row(ts, snr, delta_freq, mode, message, is_cq, call)
      rows.append(row)
      row_bg.append(bg)
    self._fill(self.decode_window_table, rows, row_bg)

    # My Traffic is persistent (not cycle-cleared, unlike decode_window above) and
    # already pre-filtered by Status - everything mentioning my call, everything
    # from a station whose CQ I've answered this session, and my own transmissions.
    my_rows, my_row_bg = [], []
    for ts, snr, delta_freq, mode, message, is_cq, call in reversed(data['my_traffic']):
      row, bg = self._decode_row(ts, snr, delta_freq, mode, message, is_cq, call,
                                 distinguish_reply=True)
      my_rows.append(row)
      my_row_bg.append(bg)
    self._fill(self.mycall_table, my_rows, my_row_bg)

  def _refresh_attempts(self, data):
    rows = []
    outcomes = Counter()
    # Oldest-first, newest-last - matches the Decode tab's convention, and pairs
    # with _fill()'s scroll-to-bottom so the latest attempt is always in view.
    for ts, call, grid, distance, country, band, snr, selector, outcome in reversed(
        data['attempts']):
      color = ATTEMPT_COLOR.get(outcome)
      outcomes[outcome] += 1
      rows.append([
        (ts.strftime('%H:%M:%S'), None), (call, None), (grid or "-", None),
        (f"{distance:.0f}" if distance is not None else "-", None),
        (country or "-", None), (str(snr) if snr is not None else "-", None),
        (f"{band}m" if band else "-", None), (selector or "-", None),
        (outcome, color),
      ])
    self._fill(self.attempts_table, rows)
    self.attempts_stats_label.setText(
      f"In progress: {outcomes['in progress']}   Worked: {outcomes['worked']}   "
      f"Broken: {outcomes['broken']}"
    )
    self._update_tab_badge(TAB_ATTEMPTS, len(data['attempts']))

  def _refresh_activity(self, data):
    rows = []
    row_bg = []
    # Oldest-first, newest-last - matches the Decode tab's convention, and pairs
    # with _fill()'s scroll-to-bottom so the latest entry is always in view.
    for ts, snr, delta_freq, mode, message, result in reversed(data['activity']):
      is_selected = "selected" in result
      # Highlight the selected entry the same way the Decode window highlights CQs,
      # so the winning candidate stands out from the surrounding rejections at a glance.
      color = CQ_FG if is_selected else RED
      rows.append([
        (ts.strftime('%H:%M:%S'), color if is_selected else None),
        (str(snr) if snr is not None else '', color if is_selected else None),
        (str(delta_freq) if delta_freq is not None else '', color if is_selected else None),
        (mode or '', color if is_selected else None),
        (message or '', color if is_selected else None),
        (result, color),
      ])
      row_bg.append(CQ_BG if is_selected else None)
    self._fill(self.activity_table, rows, row_bg)

    counts = data['counts']
    rejected = sum(v for k, v in counts.items() if k.startswith('rejected:'))
    reasons = ', '.join(f"{k.split(': ', 1)[1]}: {v}"
                        for k, v in sorted(counts.items()) if k.startswith('rejected:'))
    text = f"Selected: {counts.get('selected', 0)}   Rejected: {rejected}"
    if reasons:
      text += f" ({reasons})"
    self.activity_stats_label.setText(text)

  @staticmethod
  def _mhz(frequency):
    """WSJT-X reports the dial frequency in Hz; hams read it in MHz."""
    return f"{frequency / 1e6:.3f}" if frequency else "-"

  def _refresh_worked(self, data):
    rows = []
    # Oldest-first, newest-last - matches the Decode tab's convention, and pairs
    # with _fill()'s scroll-to-bottom so the latest QSO is always in view.
    for (ts, call, grid, distance, country, band,
         frequency, rst_sent, rst_rcvd) in reversed(data['worked_log']):
      rows.append([
        # Full date here, matching the history pane below - a session can run
        # past midnight, and these get cross-referenced against a logbook.
        (ts.strftime('%Y-%m-%d %H:%M:%S'), None), (call, GREEN), (grid or "-", None),
        (f"{distance:.0f}" if distance is not None else "-", None),
        (country or "-", None), (f"{band}m" if band else "-", None),
        (self._mhz(frequency), None),
        (rst_sent or "-", None), (rst_rcvd or "-", None),
      ])
    self._fill(self.worked_table, rows)
    self._refresh_history(len(data['worked_log']))

    counts = data['counts']
    rep_diff = f"{data['rep_diff']:+.1f}dB" if data['rep_diff'] is not None else "-"
    self.worked_stats_label.setText(
      f"Worked: {counts.get('worked', 0)}   Broken: {counts.get('broken', 0)}   "
      f"Rep Diff: {rep_diff}"
    )
    self._update_tab_badge(TAB_WORKED, len(data['worked_log']))

  def _refresh_history(self, session_count):
    """Reload the full QSO history from the database. Only re-queries when this
    session has logged another contact (or on the first pass) - the refresh
    timer fires twice a second and the history only changes on a new QSO."""
    if self.db_name is None or session_count == self._history_session_count:
      return
    self._history_session_count = session_count
    try:
      history = get_worked(self.db_name)
    except sqlite3.Error as err:
      logging.getLogger('ft8ctrl.gui').warning("Could not read QSO history: %s", err)
      return

    rows = []
    for qso in reversed(history):          # oldest first, newest at the bottom
      when = qso['time']
      rows.append([
        (when.strftime('%Y-%m-%d %H:%M') if hasattr(when, 'strftime') else str(when), None),
        (qso['call'], GREEN), (qso['grid'] or "-", None),
        (f"{qso['distance']:.0f}" if qso['distance'] is not None else "-", None),
        (qso['country'] or "-", None), (f"{qso['band']}m" if qso['band'] else "-", None),
        (self._mhz(qso['frequency']), None),
        (qso['rst_sent'] or "-", None), (qso['rst_rcvd'] or "-", None),
      ])
    self._fill(self.history_table, rows)
    countries = len({q['country'] for q in history if q['country']})
    grids = len({q['grid'][:4] for q in history if q['grid']})
    self.history_stats_label.setText(
      f"History: {len(history)} QSOs   Countries: {countries}   Grids: {grids}")

  def _refresh_status_bar(self, data):
    self.status_endpoint_label.setText(f"WSJT-X endpoint: {data['wsjt_endpoint'] or '-'}")
    age = 'never'
    if data['wsjtx_seen']:
      age = f"{(datetime.utcnow() - data['wsjtx_seen']).total_seconds():.0f}s"
    self.status_wsjtx_label.setText(f"WSJT-X: {age} ago")
    self.status_packets_label.setText(
      f"In:{data['packets_in']} Out:{data['packets_out']}")


def main():
  parser = ArgumentParser(description="FT8Commander-NG - WSJT-X automation - Qt GUI")
  parser.add_argument("-c", "--config", help="Name of the configuration file")
  opts = parser.parse_args()

  console_handler = DashboardLogHandler()
  console_handler.setLevel(logging.WARNING)
  config, queue, call_select = setup(opts.config, console_handler)

  main_loop = Sequencer(config, queue, call_select)
  seq_thread = Thread(target=main_loop.run, daemon=True)
  seq_thread.start()

  app = QApplication(sys.argv)
  window = MainWindow(config.my_call, call_select, main_loop, config.db_name)
  window.show()
  sys.exit(app.exec())


if __name__ == '__main__':
  main()
