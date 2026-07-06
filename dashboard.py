#
# BSD 3-Clause License
#
# Copyright (c) 2023, Fred W6BSD
# All rights reserved.
#
"""Live terminal dashboard showing what ft8ctrl is doing while it waits"""

import logging
import time
from datetime import datetime
from threading import Event, Thread

from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from status import Status

REFRESH = 2
NARROW_WIDTH = 110  # console width below which panels stack instead of side-by-side


class DashboardLogHandler(logging.Handler):
  """Redirect log records into the dashboard's log panel instead of stdout"""

  def emit(self, record):
    Status().log(record.levelname, self.format(record))


class Dashboard(Thread):

  def __init__(self, mycall, refresh=REFRESH):
    super().__init__()
    self.daemon = True
    self.mycall = mycall
    self.refresh = refresh
    self.stop_event = Event()
    self.console = Console()
    self.live = Live(self._layout(), console=self.console, refresh_per_second=refresh,
                     screen=True)

  def stop(self):
    """Signal the render loop to exit so the terminal screen is restored cleanly"""
    self.stop_event.set()
    self.join(timeout=2)

  def run(self):
    with self.live:
      while not self.stop_event.is_set():
        self.live.update(self._layout())
        self.stop_event.wait(1 / self.refresh)

  def _layout(self):
    data = Status().snapshot()
    layout = Layout()
    layout.split_column(
      Layout(self._header(data), name='header', size=3),
      Layout(name='body'),
      Layout(self._footer(data), name='footer', size=3),
    )
    body = layout['body']
    window = Layout(self._decode_window(data), name='window')
    decodes = Layout(self._decode_table(data), name='decodes')
    activity = Layout(self._activity_table(data), name='activity')
    worked = Layout(self._worked_table(data), name='worked')
    session = Layout(self._session_panel(data), name='session', size=8)

    if self.console.width < NARROW_WIDTH:
      body.split_column(window, decodes, activity, worked, session)
    else:
      left = Layout(name='left')
      right = Layout(name='right')
      left.split_column(window, decodes)
      right.split_column(activity, worked, session)
      body.split_row(left, right)
    return layout

  def _header(self, data):
    age = 'never'
    if data['wsjtx_seen']:
      age = f"{(datetime.utcnow() - data['wsjtx_seen']).total_seconds():.0f}s ago"
    band = f"{data['band']}m" if data['band'] else '-'

    if data['transmitting']:
      activity = f"[bold yellow]Calling {data['current_call']}[/] " \
                 f"(attempt {data['current_retry']}/{data['max_retries']})"
    elif not data['tx_enabled']:
      activity = "[dim]Tx disabled - listening only[/]"
    else:
      activity = "[green]Idle - waiting for a candidate[/]"

    text = (f"[bold]{self.mycall}[/]   Band: {band}   Mode: {data['mode'] or '-'}   "
            f"WSJT-X last seen: {age}   |   {activity}")
    return Panel(Align.center(text), style="cyan")

  @staticmethod
  def _decode_window(data):
    table = Table(title="Decode window (current cycle)", expand=True)
    table.add_column("Time", width=8)
    table.add_column("SNR", justify="right")
    table.add_column("Freq(Hz)", justify="right")
    table.add_column("Mode", width=5)
    table.add_column("Message")
    for ts, snr, delta_freq, mode, message, is_cq in data['decode_window']:
      if is_cq:
        message = f"[bold yellow]{message}[/]"
      table.add_row(ts.strftime('%H:%M:%S'), str(snr), str(delta_freq), mode, message)
    return Panel(table)

  @staticmethod
  def _decode_table(data):
    table = Table(title="Recent CQ traffic", expand=True)
    table.add_column("Time", width=8)
    table.add_column("Call")
    table.add_column("Grid", width=6)
    table.add_column("Dist(km)", justify="right")
    table.add_column("Country", max_width=18, no_wrap=True, overflow="ellipsis")
    table.add_column("SNR", justify="right")
    table.add_column("Band", justify="right")
    table.add_column("Type")
    for ts, call, extra, snr, band, grid, distance, country in data['decodes']:
      table.add_row(
        ts.strftime('%H:%M:%S'), call, grid or "-",
        f"{distance:.0f}" if distance is not None else "-",
        country or "-", str(snr), f"{band}m" if band else "-", extra or ""
      )
    return Panel(table)

  @staticmethod
  def _activity_table(data):
    table = Table(title="Selection activity", expand=True)
    table.add_column("Time", width=8)
    table.add_column("Call")
    table.add_column("Result")
    for ts, call, result in data['activity']:
      style = "green" if "selected" in result else "red"
      table.add_row(ts.strftime('%H:%M:%S'), call, f"[{style}]{result}[/]")
    return Panel(table)

  @staticmethod
  def _worked_table(data):
    table = Table(title="Worked", expand=True)
    table.add_column("Time", width=8)
    table.add_column("Call")
    table.add_column("Grid", width=6)
    table.add_column("Country", max_width=18, no_wrap=True, overflow="ellipsis")
    table.add_column("Band", justify="right")
    for ts, call, grid, country, band in data['worked_log']:
      table.add_row(ts.strftime('%H:%M:%S'), f"[bold green]{call}[/]", grid or "-",
                    country or "-", f"{band}m" if band else "-")
    return Panel(table)

  @staticmethod
  def _session_panel(data):
    selectors = ', '.join(data['selectors']) if data['selectors'] else '-'
    relay = data['relay_endpoint'] or 'not configured'
    last_relay = 'never'
    if data['last_relay']:
      last_relay = f"{(datetime.utcnow() - data['last_relay']).total_seconds():.0f}s ago"

    lines = [
      f"Grid: {data['grid'] or '-'}    Max retries: {data['max_retries'] or '-'}",
      f"Selectors: {selectors}",
      f"Blacklist: {data['blacklist_size']} calls",
      f"WSJT-X: {data['wsjt_endpoint'] or '-'}   In: {data['packets_in']}   "
      f"Out: {data['packets_out']}",
      f"Logger relay: {relay}   Sent: {data['relay_count']}   Last: {last_relay}",
    ]
    return Panel("\n".join(lines), title="Session")

  @staticmethod
  def _footer(data):
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
    return Panel(Align.center(" | ".join(parts)))
