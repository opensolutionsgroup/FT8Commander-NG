#!/usr/bin/env python3
#
# BSD 3-Clause License
#
# Copyright (c) 2023, Fred W6BSD
# All rights reserved.
#
"""Qt GUI front-end for ft8ctrl, mirroring the terminal dashboard's panels.

Requires PyQt6 (installed via `apt install python3-pyqt6` on Debian/Ubuntu rather
than pip, since it isn't in requirements.txt - see README for details).
"""

import logging
import sys
from argparse import ArgumentParser
from datetime import datetime
from threading import Thread

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QApplication, QGroupBox, QHBoxLayout, QLabel,
                             QMainWindow, QSplitter, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from dashboard import DashboardLogHandler
from ft8ctrl import Sequencer, setup
from status import Status

REFRESH_MS = 500
GREEN = QColor("#2e7d32")
RED = QColor("#c62828")
GOLD = QColor("#b8860b")


class MainWindow(QMainWindow):

  def __init__(self, mycall):
    super().__init__()
    self.mycall = mycall
    self.setWindowTitle(f"FT8Commander - {mycall}")
    self.resize(1500, 950)

    central = QWidget()
    self.setCentralWidget(central)
    layout = QVBoxLayout(central)

    self.header_label = QLabel()
    self.header_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    self.header_label.setStyleSheet(
      "background-color: #073642; color: #eee8d5; padding: 8px; font-weight: bold;")
    layout.addWidget(self.header_label)

    body = QSplitter(Qt.Orientation.Horizontal)
    layout.addWidget(body, stretch=1)

    self.decode_window_table = self._make_table(
      ["Time", "SNR", "Freq(Hz)", "Mode", "Message"])
    self.cq_table = self._make_table(
      ["Time", "Call", "Grid", "Dist(km)", "Country", "SNR", "Band", "Type"])
    left = QSplitter(Qt.Orientation.Vertical)
    left.addWidget(self._boxed("Decode window (current cycle)", self.decode_window_table))
    left.addWidget(self._boxed("Recent CQ traffic", self.cq_table))

    self.activity_table = self._make_table(["Time", "Call", "Result"])
    self.worked_table = self._make_table(["Time", "Call", "Grid", "Country", "Band"])
    self.session_label = QLabel()
    self.session_label.setTextFormat(Qt.TextFormat.RichText)
    right = QSplitter(Qt.Orientation.Vertical)
    right.addWidget(self._boxed("Selection activity", self.activity_table))
    right.addWidget(self._boxed("Worked", self.worked_table))
    right.addWidget(self._boxed("Session", self.session_label))

    body.addWidget(left)
    body.addWidget(right)

    self.footer_label = QLabel()
    self.footer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(self.footer_label)

    self.timer = QTimer(self)
    self.timer.timeout.connect(self.refresh)
    self.timer.start(REFRESH_MS)
    self.refresh()

  @staticmethod
  def _make_table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.horizontalHeader().setStretchLastSection(True)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    table.verticalHeader().setVisible(False)

    font = table.font()
    font.setPointSize(max(8, font.pointSize() - 1))
    table.setFont(font)
    table.horizontalHeader().setFont(font)
    table.verticalHeader().setDefaultSectionSize(20)
    table.setStyleSheet("QTableWidget::item { padding: 0px; }")
    return table

  @staticmethod
  def _boxed(title, widget):
    box = QGroupBox(title)
    vbox = QVBoxLayout(box)
    vbox.addWidget(widget)
    return box

  @staticmethod
  def _fill(table, rows):
    table.setRowCount(len(rows))
    for row, cells in enumerate(rows):
      for col, (text, color) in enumerate(cells):
        item = QTableWidgetItem(text)
        if color:
          item.setForeground(color)
        table.setItem(row, col, item)

  def refresh(self):
    data = Status().snapshot()
    self._refresh_header(data)
    self._refresh_decode_window(data)
    self._refresh_cq_table(data)
    self._refresh_activity(data)
    self._refresh_worked(data)
    self._refresh_session(data)
    self._refresh_footer(data)

  def _refresh_header(self, data):
    age = 'never'
    if data['wsjtx_seen']:
      age = f"{(datetime.utcnow() - data['wsjtx_seen']).total_seconds():.0f}s ago"
    band = f"{data['band']}m" if data['band'] else '-'

    if data['transmitting']:
      activity = f"Calling {data['current_call']} (attempt {data['current_retry']}/" \
                 f"{data['max_retries']})"
    elif not data['tx_enabled']:
      activity = "Tx disabled - listening only"
    else:
      activity = "Idle - waiting for a candidate"

    self.header_label.setText(
      f"{self.mycall}   Band: {band}   Mode: {data['mode'] or '-'}   "
      f"WSJT-X last seen: {age}   |   {activity}")

  def _refresh_decode_window(self, data):
    rows = [
      [(ts.strftime('%H:%M:%S'), GOLD if is_cq else None), (str(snr), GOLD if is_cq else None),
       (str(delta_freq), GOLD if is_cq else None), (mode, GOLD if is_cq else None),
       (message, GOLD if is_cq else None)]
      for ts, snr, delta_freq, mode, message, is_cq in data['decode_window']
    ]
    self._fill(self.decode_window_table, rows)

  def _refresh_cq_table(self, data):
    rows = []
    for ts, call, extra, snr, band, grid, distance, country in data['decodes']:
      rows.append([
        (ts.strftime('%H:%M:%S'), None), (call, None), (grid or "-", None),
        (f"{distance:.0f}" if distance is not None else "-", None),
        (country or "-", None), (str(snr), None),
        (f"{band}m" if band else "-", None), (extra or "", None),
      ])
    self._fill(self.cq_table, rows)

  def _refresh_activity(self, data):
    rows = []
    for ts, call, result in data['activity']:
      color = GREEN if "selected" in result else RED
      rows.append([(ts.strftime('%H:%M:%S'), None), (call, None), (result, color)])
    self._fill(self.activity_table, rows)

  def _refresh_worked(self, data):
    rows = []
    for ts, call, grid, country, band in data['worked_log']:
      rows.append([
        (ts.strftime('%H:%M:%S'), None), (call, GREEN), (grid or "-", None),
        (country or "-", None), (f"{band}m" if band else "-", None),
      ])
    self._fill(self.worked_table, rows)

  def _refresh_session(self, data):
    selectors = ', '.join(data['selectors']) if data['selectors'] else '-'
    relay = data['relay_endpoint'] or 'not configured'
    last_relay = 'never'
    if data['last_relay']:
      last_relay = f"{(datetime.utcnow() - data['last_relay']).total_seconds():.0f}s ago"

    self.session_label.setText(
      f"Grid: {data['grid'] or '-'}&nbsp;&nbsp;&nbsp;&nbsp;"
      f"Max retries: {data['max_retries'] or '-'}<br>"
      f"Selectors: {selectors}<br>"
      f"Blacklist: {data['blacklist_size']} calls<br>"
      f"WSJT-X: {data['wsjt_endpoint'] or '-'}&nbsp;&nbsp;"
      f"In: {data['packets_in']}&nbsp;&nbsp;Out: {data['packets_out']}<br>"
      f"Logger relay: {relay}&nbsp;&nbsp;Sent: {data['relay_count']}&nbsp;&nbsp;"
      f"Last: {last_relay}"
    )

  def _refresh_footer(self, data):
    counts = data['counts']
    rejected = sum(v for k, v in counts.items() if k.startswith('rejected:'))
    parts = [
      f"Decoded: {counts.get('decoded', 0)}",
      f"Selected: {counts.get('selected', 0)}",
      f"Worked: {counts.get('worked', 0)}",
      f"Rejected: {rejected}",
    ]
    reasons = ', '.join(f"{k.split(': ', 1)[1]}: {v}"
                        for k, v in sorted(counts.items()) if k.startswith('rejected:'))
    if reasons:
      parts.append(f"({reasons})")
    self.footer_label.setText(" | ".join(parts))


def main():
  parser = ArgumentParser(description="ft8ctl wsjt-x automation - Qt GUI")
  parser.add_argument("-c", "--config", help="Name of the configuration file")
  opts = parser.parse_args()

  console_handler = DashboardLogHandler()
  console_handler.setLevel(logging.WARNING)
  config, queue, call_select = setup(opts.config, console_handler)

  main_loop = Sequencer(config, queue, call_select)
  seq_thread = Thread(target=main_loop.run, daemon=True)
  seq_thread.start()

  app = QApplication(sys.argv)
  window = MainWindow(config.my_call)
  window.show()
  sys.exit(app.exec())


if __name__ == '__main__':
  main()
