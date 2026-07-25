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

from status import Status, mentions_call

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
    window = Layout(name='window')
    window.split_column(
      Layout(self._decode_window(data), name='decode_all', ratio=3),
      Layout(self._mycall_window(data), name='decode_mine', ratio=1),
    )
    decodes = Layout(self._decode_table(data), name='decodes')
    # Activity (top) and Attempts (bottom) share one region, same split as the
    # Decode window above - Activity is the cycle-scoped "what was considered and
    # why", Attempts is the persistent "what was actually called and how it
    # turned out".
    attempts = Layout(name='attempts')
    attempts.split_column(
      Layout(self._activity_table(data), name='activity_all', ratio=1),
      Layout(self._attempts_table(data), name='attempts_all', ratio=2),
    )
    worked = Layout(self._worked_table(data), name='worked')
    session = Layout(self._session_panel(data), name='session', size=8)

    if self.console.width < NARROW_WIDTH:
      body.split_column(window, decodes, attempts, worked, session)
    else:
      left = Layout(name='left')
      right = Layout(name='right')
      left.split_column(window, decodes)
      right.split_column(attempts, worked, session)
      body.split_row(left, right)
    return layout

  def _header(self, data):
    age = 'never'
    if data['wsjtx_seen']:
      age = f"{(datetime.utcnow() - data['wsjtx_seen']).total_seconds():.0f}s ago"
    band = f"{data['band']}m" if data['band'] else '-'

    if data['current_call']:
      # `current_call` stays set for the whole QSO exchange, not just the literal
      # seconds we're transmitting a burst - showing retry progress here too means
      # you can see where you are while waiting for a reply, not just mid-Tx.
      call_style = "green" if data['worked_before'] else "red"
      verb = "Calling" if data['transmitting'] else "Waiting for reply from"
      stage = f" - {data['current_stage']}" if data['current_stage'] else ""
      activity = f"{verb} [bold {call_style}]{data['current_call']}[/] " \
                 f"(attempt {data['current_retry']}/{data['max_retries']}){stage}"
    else:
      # WSJT-X's self-reported `tx_enabled` isn't a useful idle/ready signal: it's
      # False the entire time we're idle and only flips True the moment a call
      # actually starts, so it's redundant with `transmitting` here.
      activity = "[green]Idle - waiting for a candidate[/]"

    text = (f"[bold]{self.mycall}[/]   Band: {band}   Mode: {data['mode'] or '-'}   "
            f"WSJT-X last seen: {age}   |   {activity}")
    return Panel(Align.center(text), style="cyan")

  def _add_decode_row(self, table, ts, snr, delta_freq, mode, message, is_cq, call,
                      distinguish_reply=False):
    mentions_me = mentions_call(message, self.mycall)
    # In My Traffic, split "mentions me" into two cases: a reply from the other
    # station addressed to me (blue, matching the idle/"Listening" banner) versus
    # my own outgoing Tx (red, matching the active-QSO banner). The plain Decode
    # window doesn't make this distinction (it never even shows my own Tx).
    if distinguish_reply and mentions_me and call != self.mycall:
      row_style = "white on #1976d2"
    elif mentions_me:
      row_style = "white on red"
    elif is_cq:
      row_style = None
      message = f"[bold yellow]{message}[/]"
    else:
      row_style = None
    snr_text = str(snr) if snr is not None else "Tx"
    table.add_row(ts.strftime('%H:%M:%S'), snr_text, str(delta_freq), mode, message,
                  style=row_style)

  def _decode_window(self, data):
    table = Table(title="Decode window (current cycle)", expand=True)
    table.add_column("Time", width=8)
    table.add_column("SNR", justify="right")
    table.add_column("Freq(Hz)", justify="right")
    table.add_column("Mode", width=5)
    table.add_column("Message")
    # WSJT-X's own Band Activity window lists decodes oldest-first, newest at the
    # bottom - our deque stores newest-first, so reverse just for display here.
    for ts, snr, delta_freq, mode, message, is_cq, call in reversed(data['decode_window']):
      self._add_decode_row(table, ts, snr, delta_freq, mode, message, is_cq, call)
    return Panel(table)

  def _mycall_window(self, data):
    """Similar to WSJT-X's own Rx Frequency window: everything mentioning my call
    plus every decode (including the original CQ) from a station whose CQ I've
    answered this session, plus my own transmissions - not just the current QSO.
    Persistent (not cycle-cleared, unlike decode_window above) and already
    pre-filtered by Status."""
    table = Table(title="My Traffic", expand=True)
    table.add_column("Time", width=8)
    table.add_column("SNR", justify="right")
    table.add_column("Freq(Hz)", justify="right")
    table.add_column("Mode", width=5)
    table.add_column("Message")
    for ts, snr, delta_freq, mode, message, is_cq, call in reversed(data['my_traffic']):
      self._add_decode_row(table, ts, snr, delta_freq, mode, message, is_cq, call,
                           distinguish_reply=True)
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
    table.add_column("Worked?")
    for ts, call, extra, snr, band, grid, distance, country, already_worked in data['decodes']:
      cells = [
        ts.strftime('%H:%M:%S'), call, grid or "-",
        f"{distance:.0f}" if distance is not None else "-",
        country or "-", str(snr), f"{band}m" if band else "-", extra or "",
        "[bold red]already worked[/]" if already_worked else "",
      ]
      if already_worked:
        cells = [f"[dim]{c}[/]" if i < 8 else c for i, c in enumerate(cells)]
      table.add_row(*cells)
    return Panel(table)

  @staticmethod
  def _activity_table(data):
    table = Table(title="Selection activity (current cycle)", expand=True)
    table.add_column("Time", width=8)
    table.add_column("SNR", justify="right")
    table.add_column("Freq(Hz)", justify="right")
    table.add_column("Mode", width=5)
    table.add_column("Message")
    table.add_column("Result")
    for ts, snr, delta_freq, mode, message, result in data['activity']:
      is_selected = "selected" in result
      # Highlight the selected entry's whole row the same way the GUI does, so it
      # stands out from the surrounding rejections at a glance.
      row_style = "black on bright_green" if is_selected else None
      # Result cell keeps its own text color when not selected; when selected the
      # row style above already sets black-on-green, so leave it unstyled here to
      # avoid clashing with that background.
      style = "black" if is_selected else "red"
      table.add_row(ts.strftime('%H:%M:%S'), str(snr) if snr is not None else "-",
                   str(delta_freq) if delta_freq is not None else "-", mode or "-",
                   message or "-", f"[{style}]{result}[/]", style=row_style)
    return Panel(table)

  @staticmethod
  def _attempts_table(data):
    table = Table(title="Attempts", expand=True)
    table.add_column("Time", width=8)
    table.add_column("Call")
    table.add_column("Grid", width=6)
    table.add_column("Dist(km)", justify="right")
    table.add_column("Country", max_width=18, no_wrap=True, overflow="ellipsis")
    table.add_column("SNR", justify="right")
    table.add_column("Band", justify="right")
    table.add_column("Selector")
    table.add_column("Result")
    # Same colors as the GUI's ATTEMPT_COLOR, for visual parity between the two.
    outcome_style = {"worked": "#2e7d32", "broken": "#c62828", "in progress": "#1976d2"}
    for ts, call, grid, distance, country, band, snr, selector, outcome in data['attempts']:
      style = outcome_style.get(outcome)
      table.add_row(ts.strftime('%H:%M:%S'), call, grid or "-",
                   f"{distance:.0f}" if distance is not None else "-",
                   country or "-", str(snr) if snr is not None else "-",
                   f"{band}m" if band else "-", selector or "-",
                   f"[{style}]{outcome}[/]" if style else outcome)
    return Panel(table)

  @staticmethod
  def _worked_table(data):
    table = Table(title="Worked", expand=True)
    table.add_column("Time", width=8)
    table.add_column("Call")
    table.add_column("Grid", width=6)
    table.add_column("Dist(km)", justify="right")
    table.add_column("Country", max_width=18, no_wrap=True, overflow="ellipsis")
    table.add_column("Band", justify="right")
    table.add_column("Freq(Hz)", justify="right")
    for ts, call, grid, distance, country, band, frequency in data['worked_log']:
      table.add_row(ts.strftime('%H:%M:%S'), f"[bold green]{call}[/]", grid or "-",
                    f"{distance:.0f}" if distance is not None else "-",
                    country or "-", f"{band}m" if band else "-",
                    f"{frequency:,}" if frequency is not None else "-")
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
    rep_diff = f"{data['rep_diff']:+.1f}dB" if data['rep_diff'] is not None else "-"
    parts = [
      f"Decoded: {counts.get('decoded', 0)}",
      f"Selected: {counts.get('selected', 0)}",
      f"Worked: {counts.get('worked', 0)}",
      f"Broken: {counts.get('broken', 0)}",
      f"Rep Diff: {rep_diff}",
      f"Rejected: {rejected}",
    ]
    reasons = ', '.join(f"{k.split(': ', 1)[1]}: {v}"
                        for k, v in sorted(counts.items()) if k.startswith('rejected:'))
    if reasons:
      parts.append(f"({reasons})")
    return Panel(Align.center(" | ".join(parts)))
