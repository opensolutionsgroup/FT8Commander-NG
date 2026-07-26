#
# BSD 3-Clause License
#
# Copyright (c) 2023, Fred W6BSD
# All rights reserved.
#
"""Shared, thread-safe runtime status used to feed the live dashboard"""

import re
import threading
import time
from collections import Counter, deque
from datetime import datetime

HISTORY = 100
# A single decode/selection cycle can produce dozens of simultaneous candidates on a
# busy band, so give these two cycle-scoped feeds more room than the rolling-history ones.
DECODE_WINDOW_HISTORY = 30
ACTIVITY_HISTORY = 30
# Persistent (not cycle-cleared) history of decodes/transmissions relevant to me
# specifically - much lower volume than the raw decode window, so it can hold a
# longer scrollback.
MY_TRAFFIC_HISTORY = 60


def mentions_call(message, call):
  """Whole-word match so e.g. a configured call of 'W1A' doesn't false-positive
  on 'W1AB' appearing in an unrelated decode."""
  if not message or not call:
    return False
  return re.search(rf'\b{re.escape(call)}\b', message) is not None


class Status:
  # Singleton class
  #
  # State lives in _init(), called once from __new__ rather than __init__ (so
  # repeated Status() calls don't reset it). pylint can't see that as the
  # initializer, so every field looks "defined outside __init__" to it.
  # pylint: disable=attribute-defined-outside-init

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
    # Persistent (not cycle-cleared), unlike decode_window: everything mentioning
    # my call or from a station whose CQ I've answered, plus my own transmissions -
    # a running transcript, similar to WSJT-X's own Rx Frequency window.
    self.my_traffic = deque(maxlen=MY_TRAFFIC_HISTORY)
    self.decodes = deque(maxlen=HISTORY)
    # Cycle-scoped, like decode_window: cleared and repopulated fresh each selection
    # pass rather than accumulating across cycles.
    self.activity = deque(maxlen=ACTIVITY_HISTORY)
    # Persistent (not cycle-cleared) log of every station actually called, with its
    # outcome updated in place as it resolves (worked/broken) or left "in progress".
    self.attempts = deque(maxlen=HISTORY)
    self.worked_log = deque(maxlen=HISTORY)
    self.logs = deque(maxlen=HISTORY)
    self.counts = Counter()

    self.wsjtx_seen = None
    self.frequency = 0
    self.band = 0
    self.mode = ''
    self.tx_enabled = False
    self.transmitting = False
    self.decoding = False
    self.current_call = None
    self.current_retry = 0
    self.max_retries = 0
    self.current_azimuth = None
    self.current_distance = None
    self.worked_before = False
    # Which step of the CQ -> grid -> report -> RR73 -> 73 exchange our current
    # transmission represents, e.g. "Sending signal report" - lets the UI show
    # progress through a QSO, not just the retry count.
    self.current_stage = None

    # Running average of (ReportReceived - ReportSent) across completed QSOs. A
    # consistently positive value hints there's headroom to reduce Tx power.
    self.rep_diff_sum = 0.0
    self.rep_diff_count = 0

    # Session/config info, set once at startup
    self.mycall = ''
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

    # GUI-driven control: pausing automation ("Enable Tx" toggle)
    self.paused = False
    # GUI-driven control: whether we talk to WSJT-X at all ("Monitor" toggle).
    # When False the sequencer stops reading/sending on the WSJT-X socket, so a
    # closed or stopped WSJT-X can't surface connection errors.
    self.monitoring = True

  def is_paused(self):
    with self.lock:
      return self.paused

  def set_paused(self, value):
    with self.lock:
      self.paused = bool(value)

  def is_monitoring(self):
    with self.lock:
      return self.monitoring

  def set_monitoring(self, value):
    with self.lock:
      self.monitoring = bool(value)

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

  def raw_decode(self, snr, delta_freq, mode, message, cycle=None, is_cq=False, call=None):
    """`cycle` identifies which receive period a decode belongs to (WSJT-X stamps every
    decode from the same period with the same Time value) - when it changes, the window
    is cleared first so it only ever shows the current cycle's decodes, not a rolling
    history across cycles. `call` is the sending station, already parsed by the caller
    (reusing the same CQ/REPLY regex match already done for other purposes) so the UI
    layer doesn't need its own copy of that parsing just to filter/highlight by sender."""
    with self.lock:
      if cycle is not None and cycle != self._decode_cycle:
        self.decode_window.clear()
        self._decode_cycle = cycle
      entry = (datetime.utcnow(), snr, delta_freq, mode, message, is_cq, call)
      self.decode_window.appendleft(entry)
      self.counts['raw_decoded'] += 1
      answered = {a[1] for a in self.attempts}
      # `is_cq` here matters: without it, ANY later message from a station I've
      # once answered would qualify, including their traffic with someone else
      # entirely (a QSO I'm not part of) - restricting to their CQ line keeps
      # this to "I've heard/answered this station" rather than "I once talked to
      # this station, so show me everything they ever say to anyone."
      if mentions_call(message, self.mycall) or (is_cq and call in answered):
        self.my_traffic.appendleft(entry)

  def my_transmit(self, delta_freq, mode, message, call):
    """Record one of my own outgoing transmissions in the persistent My Traffic
    feed - WSJT-X's own Rx Frequency window interleaves both sides of an
    exchange, not just what was received, so this fills the same role here."""
    with self.lock:
      self.my_traffic.appendleft(
        (datetime.utcnow(), None, delta_freq, mode, message, False, call))

  def decode(self, call, extra, snr, band, grid=None, distance=None, country=None,
             already_worked=False):
    with self.lock:
      self.decodes.appendleft((datetime.utcnow(), call, extra, snr, band, grid,
                               distance, country, already_worked))
      self.counts['decoded'] += 1
      if already_worked:
        self.counts['already worked'] += 1

  def clear_activity(self):
    """Called right before each selection pass so the activity feed only ever shows
    the current cycle's candidates and outcomes, not a rolling history across cycles."""
    with self.lock:
      self.activity.clear()

  def select(self, call, selector, worked_before=False, azimuth=None, distance=None,
             grid=None, country=None, band=None, snr=None, delta_freq=None, mode=None,
             message=None):
    with self.lock:
      self.activity.appendleft((datetime.utcnow(), snr, delta_freq, mode, message,
                                f"selected {call} ({selector})"))
      self.counts['selected'] += 1
      self.current_call = call
      self.current_retry = 0
      self.worked_before = worked_before
      self.current_azimuth = azimuth
      self.current_distance = distance
      self.current_stage = 'Sending initial call'
      self.attempts.appendleft([datetime.utcnow(), call, grid, distance, country, band,
                                snr, selector, 'in progress'])
      # Backfill this station's CQ, already seen this cycle, into My Traffic.
      # raw_decode() couldn't have known this station was "answered" yet at the
      # time it was decoded, since selection always happens after decode, so
      # without this its CQ line would never appear there. Restricted to `is_cq`
      # for the same reason as the check in raw_decode() - anything else from
      # this station this cycle is traffic with someone else, not with me.
      for entry in self.decode_window:
        if entry[5] and entry[6] == call and entry not in self.my_traffic:
          self.my_traffic.appendleft(entry)

  def _resolve_attempt(self, call, outcome):
    """Find the most recent still-open attempt for this call and mark its outcome.
    Caller must already hold self.lock."""
    for entry in self.attempts:
      if entry[1] == call and entry[8] == 'in progress':
        entry[8] = outcome
        break

  def broken(self, call):
    """A QSO that was actively being attempted (already past select()) but never
    completed - distinct from reject(), which covers candidates that were never
    even attempted."""
    with self.lock:
      self.counts['broken'] += 1
      self._resolve_attempt(call, 'broken')

  def reject(self, call, reason, category=None, snr=None, delta_freq=None, mode=None,
             message=None):
    """`reason` is the detail shown in the activity feed (may vary per call, e.g. an
    exact SNR value); `category` is a stable label used to group the footer counts so
    per-call variations don't fragment into a separate tally each. The decode-time
    fields (snr/delta_freq/mode/message) aren't always available - some rejections
    happen on an already-in-progress attempt tracked only by callsign, not a fresh
    decode record - and are left blank in that case."""
    with self.lock:
      self.activity.appendleft((datetime.utcnow(), snr, delta_freq, mode, message,
                                f"skipped {call} - {reason}"))
      self.counts[f'rejected: {category or reason}'] += 1

  def worked(self, call, grid=None, country=None, band=None, rep_diff=None,
             distance=None, frequency=None):
    with self.lock:
      self.worked_log.appendleft((datetime.utcnow(), call, grid, distance, country,
                                  band, frequency))
      self.counts['worked'] += 1
      if rep_diff is not None:
        self.rep_diff_sum += rep_diff
        self.rep_diff_count += 1
      self._resolve_attempt(call, 'worked')

  def log(self, level, message):
    with self.lock:
      self.logs.appendleft((datetime.utcnow(), level, message))

  def snapshot(self):
    with self.lock:
      rep_diff = (self.rep_diff_sum / self.rep_diff_count) if self.rep_diff_count else None
      return {
        'start_time': self.start_time,
        'worked_before': self.worked_before,
        'rep_diff': rep_diff,
        'decode_window': list(self.decode_window),
        'my_traffic': list(self.my_traffic),
        'decodes': list(self.decodes),
        'activity': list(self.activity),
        'attempts': [list(entry) for entry in self.attempts],
        'worked_log': list(self.worked_log),
        'logs': list(self.logs),
        'counts': dict(self.counts),
        'wsjtx_seen': self.wsjtx_seen,
        'frequency': self.frequency,
        'band': self.band,
        'mode': self.mode,
        'tx_enabled': self.tx_enabled,
        'transmitting': self.transmitting,
        'decoding': self.decoding,
        'current_call': self.current_call,
        'current_retry': self.current_retry,
        'max_retries': self.max_retries,
        'current_azimuth': self.current_azimuth,
        'current_distance': self.current_distance,
        'current_stage': self.current_stage,
        'grid': self.grid,
        'selectors': list(self.selectors),
        'blacklist_size': self.blacklist_size,
        'wsjt_endpoint': self.wsjt_endpoint,
        'relay_endpoint': self.relay_endpoint,
        'packets_in': self.packets_in,
        'packets_out': self.packets_out,
        'relay_count': self.relay_count,
        'last_relay': self.last_relay,
        'paused': self.paused,
        'monitoring': self.monitoring,
      }
