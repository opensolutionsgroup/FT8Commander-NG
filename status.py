#
# BSD 3-Clause License
#
# Copyright (c) 2023, Fred W6BSD
# All rights reserved.
#
"""Shared, thread-safe runtime status used to feed the live dashboard"""

import threading
import time
from collections import Counter, deque
from datetime import datetime

HISTORY = 15
# A single decode cycle can produce dozens of simultaneous decodes on a busy band,
# so give the raw decode window more room than the other, more selective, feeds.
DECODE_WINDOW_HISTORY = 30


class Status:
  # Singleton class

  def __new__(cls):
    if hasattr(cls, '_instance') and isinstance(cls._instance, cls):
      return cls._instance
    cls._instance = super(Status, cls).__new__(cls)
    cls._instance._init()
    return cls._instance

  def _init(self):
    self.lock = threading.Lock()
    self.start_time = time.time()
    self.decode_window = deque(maxlen=DECODE_WINDOW_HISTORY)
    self._decode_cycle = None
    self.decodes = deque(maxlen=HISTORY)
    self.activity = deque(maxlen=HISTORY)
    self.worked_log = deque(maxlen=HISTORY)
    self.logs = deque(maxlen=HISTORY)
    self.counts = Counter()

    self.wsjtx_seen = None
    self.frequency = 0
    self.band = 0
    self.mode = ''
    self.tx_enabled = False
    self.transmitting = False
    self.current_call = None
    self.current_retry = 0
    self.max_retries = 0

    # Session/config info, set once at startup
    self.grid = ''
    self.selectors = []
    self.blacklist_size = 0
    self.wsjt_endpoint = ''
    self.relay_endpoint = None

    # Networking counters
    self.packets_in = 0
    self.packets_out = 0
    self.relay_count = 0
    self.last_relay = None

  def heartbeat(self):
    with self.lock:
      self.wsjtx_seen = datetime.utcnow()
      self.packets_in += 1

  def packet_out(self):
    with self.lock:
      self.packets_out += 1

  def relayed(self):
    with self.lock:
      self.relay_count += 1
      self.last_relay = datetime.utcnow()

  def state(self, **kwargs):
    with self.lock:
      for key, val in kwargs.items():
        setattr(self, key, val)

  def raw_decode(self, snr, delta_freq, mode, message, cycle=None, is_cq=False):
    """`cycle` identifies which receive period a decode belongs to (WSJT-X stamps every
    decode from the same period with the same Time value) - when it changes, the window
    is cleared first so it only ever shows the current cycle's decodes, not a rolling
    history across cycles."""
    with self.lock:
      if cycle is not None and cycle != self._decode_cycle:
        self.decode_window.clear()
        self._decode_cycle = cycle
      self.decode_window.appendleft((datetime.utcnow(), snr, delta_freq, mode, message, is_cq))
      self.counts['raw_decoded'] += 1

  def decode(self, call, extra, snr, band, grid=None, distance=None, country=None):
    with self.lock:
      self.decodes.appendleft((datetime.utcnow(), call, extra, snr, band, grid,
                               distance, country))
      self.counts['decoded'] += 1

  def select(self, call, selector):
    with self.lock:
      self.activity.appendleft((datetime.utcnow(), call, f"selected ({selector})"))
      self.counts['selected'] += 1
      self.current_call = call
      self.current_retry = 0

  def reject(self, call, reason, category=None):
    """`reason` is the detail shown in the activity feed (may vary per call, e.g. an
    exact SNR value); `category` is a stable label used to group the footer counts so
    per-call variations don't fragment into a separate tally each"""
    with self.lock:
      self.activity.appendleft((datetime.utcnow(), call, f"skipped - {reason}"))
      self.counts[f'rejected: {category or reason}'] += 1

  def worked(self, call, grid=None, country=None, band=None):
    with self.lock:
      self.worked_log.appendleft((datetime.utcnow(), call, grid, country, band))
      self.counts['worked'] += 1

  def log(self, level, message):
    with self.lock:
      self.logs.appendleft((datetime.utcnow(), level, message))

  def snapshot(self):
    with self.lock:
      return {
        'start_time': self.start_time,
        'decode_window': list(self.decode_window),
        'decodes': list(self.decodes),
        'activity': list(self.activity),
        'worked_log': list(self.worked_log),
        'logs': list(self.logs),
        'counts': dict(self.counts),
        'wsjtx_seen': self.wsjtx_seen,
        'frequency': self.frequency,
        'band': self.band,
        'mode': self.mode,
        'tx_enabled': self.tx_enabled,
        'transmitting': self.transmitting,
        'current_call': self.current_call,
        'current_retry': self.current_retry,
        'max_retries': self.max_retries,
        'grid': self.grid,
        'selectors': list(self.selectors),
        'blacklist_size': self.blacklist_size,
        'wsjt_endpoint': self.wsjt_endpoint,
        'relay_endpoint': self.relay_endpoint,
        'packets_in': self.packets_in,
        'packets_out': self.packets_out,
        'relay_count': self.relay_count,
        'last_relay': self.last_relay,
      }
