#!/usr/bin/env python3
#
# BSD 3-Clause License
#
# Copyright (c) 2023, Fred W6BSD
# All rights reserved.
#

import logging
import os
import re
import select
import socket
import sys
import time
from argparse import ArgumentParser
from datetime import datetime
from importlib import import_module
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Queue

import DXEntity
import geo
import wsjtx
from config import Config
from dashboard import Dashboard, DashboardLogHandler
from dbutils import DBCommand, DBInsert, Purge, create_db, get_band, has_worked
from plugins.base import BlackList
from status import Status

SEQUENCE_TIME = {
  # FT8's period is 15s; checking only 4x/minute left a real gap where Decode
  # Window (which refreshes on WSJT-X's own, independent ~15s decode-period
  # clock) could show CQs that hadn't been evaluated - and might already be
  # cleared and replaced - before the next check caught up to them. Checking
  # every 5s instead gives several chances per period; the selector's own
  # 3-second result cache keeps this from hammering the database.
  'FT8': {2, 7, 12, 17, 22, 27, 32, 37, 42, 47, 52, 57},
  'FT4': {0, 6, 12, 18, 24, 30, 36, 42, 48, 54},
}

PARSERS = {
  'REPLY': re.compile(r'^((?!CQ)(?P<to>\w+)(|/\w+)) (?P<call>\w+)(|/\w+) .*'),
  'CQ': re.compile(r'''^CQ\s(?:CQ\s|(?P<extra>[\S.]+)\s|)
                   (?P<call>\w+(|/\w+))\s
                   (?P<grid>[A-Z]{2}[0-9]{2})''', re.VERBOSE),
  'BROKENCQ': re.compile(r'^CQ\s(?P<call>\w+(|/\w+))$'),
}

# Last token of our own outgoing message tells us where we are in the standard
# CQ -> grid -> report -> RR73 -> 73 exchange, since each stage has a distinct
# trailing token (WSJT-X always puts the report/roger/sign-off token last).
TX_STAGES = (
  (re.compile(r'^RR73$'), 'Confirming report (RR73)'),
  (re.compile(r'^RRR$'), 'Confirming report (RRR)'),
  (re.compile(r'^73$'), 'Signing off (73)'),
  (re.compile(r'^R[+-]\d{2}$'), 'Sending signal report'),
  (re.compile(r'^[+-]\d{2}$'), 'Sending signal report'),
  (re.compile(r'^[A-R]{2}[0-9]{2}([A-X]{2})?$'), 'Sending initial call'),
)


def classify_tx_stage(message):
  """Classify our current outgoing FT8/FT4 message into a short human label
  describing where we are in the exchange, or None if it doesn't match a
  recognized stage (e.g. blank, or a non-standard free-text message)."""
  if not message:
    return None
  last = message.split()[-1]
  for regexp, label in TX_STAGES:
    if regexp.match(last):
      return label
  return None


LOGFILE_SIZE = 2 << 20
LOGFILE_NAME = 'ft8ctrl-debug.log'
LOG = None


class Sequencer:
  # pylint: disable=too-many-instance-attributes
  def __init__(self, config, queue, call_select):
    self.mycall = config.my_call
    self.queue = queue
    self.selector = call_select
    self.follow_frequency = config.follow_frequency
    self.tx_power = getattr(config, 'tx_power')
    self.tx_retries = getattr(config, 'tx_retries', 5)
    self.origin = geo.grid2latlon(config.my_grid)
    self.dxe_lookup = DXEntity.DXCC().lookup
    self.db_name = Path(config.db_name).expanduser()

    bind_addr = socket.gethostbyname(config.wsjt_ip)
    self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.sock.setblocking(False)  # Set socket to non-blocking mode
    self.sock.bind((bind_addr, config.wsjt_port))

    self.logger_ip = getattr(config, 'logger_ip', None)
    self.logger_port = getattr(config, 'logger_port', None)
    self.logger_socket = None

  def call_station(self, ip_from, data):
    LOG.info(('Calling: %s (%s), From: %s, SNR: %d, Distance: %d, Band: %dm '
             '- %s - https://www.qrz.com/db/%s'),
             data['call'], data['extra'], data['country'], data['snr'], data['distance'],
             data['band'], data['selector'], data['call'])
    pkt = data['packet']
    Status().select(data['call'], data['selector'],
                    worked_before=has_worked(self.db_name, data['call']),
                    azimuth=data.get('azimuth'), distance=data.get('distance'),
                    grid=data.get('grid'), country=data.get('country'),
                    band=data.get('band'), snr=data.get('snr'),
                    delta_freq=pkt.get('DeltaFrequency'), mode=pkt.get('Mode'),
                    message=pkt.get('Message'))
    packet = wsjtx.WSReply()
    packet.call = data['call']
    packet.Time = data['time']
    packet.SNR = data['snr']
    packet.DeltaTime = pkt['DeltaTime']
    packet.DeltaFrequency = pkt['DeltaFrequency']
    packet.Mode = pkt['Mode']
    packet.Message = pkt['Message']
    if self.follow_frequency:
      packet.Modifiers = wsjtx.Modifiers.SHIFT

    LOG.debug('Transmitting %s', packet)
    try:
      self.sock.sendto(packet.raw(), ip_from)
      Status().packet_out()
    except IOError as err:
      LOG.error("%s - %r", err, packet)

  def stop_transmit(self, ip_from, graceful=True):
    """`graceful=True` (default) waits for the current sequence to finish before
    stopping, avoiding an abrupt on-air cutoff. `graceful=False` halts immediately,
    but appears to also uncheck WSJT-X's own Enable Tx checkbox as a side effect,
    which then stays off with no documented UDP command to remotely re-enable it -
    so prefer graceful unless immediate really is required."""
    stop_pkt = wsjtx.WSHaltTx()
    stop_pkt.mode = graceful
    try:
      self.sock.sendto(stop_pkt.raw(), ip_from)
      Status().packet_out()
    except socket.error as err:
      LOG.error(err)

  def sendto_log(self, packet):
    if not self.logger_ip or not self.logger_port:
      return
    packet.TXPower = str(self.tx_power or packet.TXPower)
    packet.Comments = "[ft8ctrl] " + packet.Comments
    if not self.logger_socket:
      self.logger_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.logger_socket.sendto(packet.raw(), (self.logger_ip, self.logger_port))
    Status().relayed()

  def parser(self, message):
    for name, regexp in PARSERS.items():
      if not (match := regexp.match(message)):
        continue
      data = match.groupdict()
      if name == 'BROKENCQ':
        name = 'CQ'
        data['extra'] = data['grid'] = None
      elif name == 'CQ':
        LOG.debug("%s = %r, %s", name, data, message)
      return (name, data)
    LOG.debug('Unmatched: %s', message)
    return (None, None)

  def log_call(self, packet):
    self.sendto_log(packet)
    frequency = packet.DialFrequency
    self.queue.put(
      (DBCommand.STATUS, {"call": packet.DXCall, "status": 2, "band": get_band(frequency)})
    )
    country = None
    try:
      country = self.dxe_lookup(packet.DXCall).country
    except KeyError:
      pass
    rep_diff = None
    try:
      rep_diff = int(packet.ReportReceived.lstrip('R')) - int(packet.ReportSent.lstrip('R'))
    except (ValueError, TypeError, AttributeError):
      pass
    distance = None
    try:
      distance = geo.distance(self.origin, geo.grid2latlon(packet.DXGrid))
    except (KeyError, RuntimeError, TypeError):
      pass
    Status().worked(packet.DXCall, packet.DXGrid, country, get_band(frequency), rep_diff,
                    distance=distance, frequency=frequency)
    LOG.info("** Logged call: %s, Grid: %s, Mode: %s",
             packet.DXCall, packet.DXGrid, wsjtx.Mode(packet.Mode).name)

  def decode(self, packet):
    try:
      return self.parser(packet.Message)
    except TypeError as err:
      LOG.error('Error: %s - Message: %s', err, packet.Message)
    return (None, None)

  def run(self):
    # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    ip_from = None
    tx_status = False
    frequency = 0
    current = None
    current_since = None
    current_retries = 0
    last_tx_message = ""
    was_transmitting = False
    was_paused = False
    giving_up = False
    # WSJT-X can log the QSO (send WSLogged) as soon as it decodes RR73, which is
    # often before it's actually finished transmitting our own final 73 - this
    # defers clearing `current` until that transmission really ends, same idea as
    # `giving_up` below for the retries-exceeded path.
    completing = False
    sequence = []
    LOG.info('ft8ctl running...')

    while True:
      if not Status().is_monitoring():
        # Monitoring turned off from the GUI (Monitor button): don't act on
        # anything WSJT-X sends and never transmit, so a stopped/closed WSJT-X
        # can't surface connection errors. Drop any in-flight QSO tracking so we
        # don't resume mid-exchange against stale state, and reflect the "not
        # talking to WSJT-X" state in the UI.
        if current is not None or was_transmitting:
          current = current_since = None
          current_retries = 0
          giving_up = completing = was_transmitting = False
        Status().state(current_call=None, transmitting=False, decoding=False,
                       tx_enabled=False)
        # Still drain the socket, discarding everything. WSJT-X keeps
        # broadcasting regardless of our button, and the kernel receive buffer
        # fills in minutes (~208KB) - if we simply ignored it, re-enabling
        # Monitor after being away would replay hours of stale decodes and
        # status packets as though they were live: calling stations heard
        # yesterday, retry-counting against an ancient TxMessage, bogus
        # frequency/Transmitting state. Discarding here guarantees that turning
        # Monitor back on always resumes from genuinely current traffic.
        try:
          while True:
            self.sock.recvfrom(1024)
        except (BlockingIOError, OSError):
          pass          # drained (non-blocking socket raises once empty)
        time.sleep(0.5)
        continue

      try:
        fds, _, _ = select.select([self.sock], [], [], .7)
        rawdata_list = [fdin.recvfrom(1024) for fdin in fds]
      except OSError as err:
        # WSJT-X likely went away mid-session; the OS delivers the resulting ICMP
        # error on the next socket op. Don't let it crash the sequencer thread -
        # log quietly and retry (the user can hit Monitor off to stop entirely).
        LOG.debug("WSJT-X socket error (is WSJT-X running?): %s", err)
        time.sleep(1)
        continue

      for rawdata, ip_from in rawdata_list:
        packet = wsjtx.ft8_decode(rawdata)
        Status().heartbeat()
        match packet:
          case wsjtx.WSHeartbeat() | wsjtx.WSADIF():
            pass
          case wsjtx.WSLogged():
            self.log_call(packet)
            if was_transmitting:
              # Still audibly sending the final 73 - don't jump the UI back to
              # "Listening" mid-transmission. Cleared below once it actually ends.
              completing = True
            else:
              current = None
              current_since = None
              giving_up = False
          case wsjtx.WSDecode():
            name, match = self.decode(packet)
            Status().raw_decode(packet.SNR, packet.DeltaFrequency, packet.Mode, packet.Message,
                                cycle=packet.Time, is_cq=(name == 'CQ'),
                                call=match.get('call') if match else None)
            if name == 'REPLY' and match['call'] == current and match['to'] != self.mycall:
              LOG.info("Stop Transmit: %s Replying to %s ", match['call'], match['to'])
              self.stop_transmit(ip_from)
              self.queue.put((DBCommand.DELETE,
                              {"call": match['call'], "band": get_band(frequency)}))
              Status().reject(match['call'], f"replied to {match['to']}",
                              category="replied to another station")
              Status().broken(match['call'])
              current = None
              current_since = None
              giving_up = False
            elif name == 'CQ':
              match['frequency'] = frequency
              match['band'] = get_band(frequency)
              match['packet'] = packet.as_dict()
              distance, country = None, None
              try:
                distance = geo.distance(self.origin, geo.grid2latlon(match['grid']))
                country = self.dxe_lookup(match['call']).country
              except (KeyError, RuntimeError):
                pass
              already_worked = has_worked(self.db_name, match['call'], match['band'])
              Status().decode(match['call'], match.get('extra'), packet.SNR, match['band'],
                              match.get('grid'), distance, country, already_worked)
              self.queue.put((DBCommand.INSERT, match))
            continue
          case wsjtx.WSStatus():
            tx = not packet.Decoding and packet.Transmitting
            # TxMessage reflects the next/current queued transmission even while
            # we're waiting for a reply (not just mid-burst), so this stays up to
            # date through the whole exchange, not just during Tx.
            if packet.DXCall and packet.DXCall == current:
              stage = classify_tx_stage(packet.TxMessage)
              if stage:
                Status().state(current_stage=stage)
            # Only re-mark "in progress" while this is still our tracked active call
            # and we haven't already decided to give up on it (see `giving_up` below).
            # WSJT-X can keep reporting Transmitting=True for several more seconds
            # while it finishes a graceful halt - without this check, those late
            # packets kept flipping the abandoned (3) status back to 1.
            if (packet.Transmitting and packet.DXCall and packet.DXCall == current
                and not giving_up):
              self.queue.put(
                (DBCommand.STATUS,
                 {"call": packet.DXCall, "status": 1, "band": get_band(packet.Frequency)})
              )

            # WSJT-X sends multiple status packets during a single transmission.
            # Only count a retry on the False -> True edge, i.e. once per actual
            # transmission, instead of once per status packet.
            if tx and not was_transmitting:
              # WSJT-X doesn't decode its own Tx as a received signal, so this is
              # the only place we ever see our own outgoing message - record it
              # here so the "My Traffic" feed shows both sides of the exchange,
              # not just what was received. `call` is the sender, same convention
              # as decode_window - for our own Tx, that's us, not the DX station.
              Status().my_transmit(packet.TXdf, packet.TXMode, packet.TxMessage,
                                   self.mycall)
              if last_tx_message != packet.TxMessage:
                # The message changed - either a fresh QSO, or the DX station
                # replied and WSJT-X auto-advanced to the next stage - either way
                # that's progress, so this station gets a fresh retry budget for
                # the new stage instead of being given up on.
                current_retries = 0
                last_tx_message = packet.TxMessage
                giving_up = False
              current_retries += 1
              if current_retries > self.tx_retries and not giving_up:
                # Strictly greater, not >=, and this is a deliberate, user-chosen
                # tradeoff (informed decision, not an off-by-one - do NOT "fix" to
                # >=): with tx_retries=N, we send N copies of the message and then
                # leave Enable Tx armed for one more receive period so the DX gets
                # a full cycle to come back to us. If they reply, WSJT-X auto-
                # advances (message changes -> retries reset above) and the QSO
                # completes. If they don't, WSJT-X starts repeating the same
                # message an (N+1)th time - that's the tx edge we catch here, and
                # only now do we halt. Cost: one extra (repeat) transmission goes
                # out on air in the unanswered case. Benefit: marginal QSOs where
                # the DX is slow to decode us still complete instead of being
                # abandoned the instant our last intended transmission ends. In
                # WSJT-X, "stay able to answer a reply" and "send a repeat" are the
                # same Enable-Tx-on state, so this grace period necessarily allows
                # that one trailing repeat.
                LOG.info("Retries exceeded, stopping transmit after this attempt")
                self.stop_transmit(ip_from)
                if packet.DXCall:
                  # Mark abandoned (status 3) rather than deleting: WSJT-X will keep
                  # decoding this station's CQ, and a fresh INSERT would otherwise
                  # make it immediately selectable again. It cools down and becomes
                  # eligible again after retry_time, same as a stale status 0 record.
                  self.queue.put((DBCommand.STATUS,
                                  {"call": packet.DXCall, "status": 3,
                                   "band": get_band(packet.Frequency)}))
                  Status().reject(packet.DXCall, 'retries exceeded')
                  Status().broken(packet.DXCall)
                # Don't clear `current`/`current_retries` yet - the graceful halt
                # lets this attempt actually finish transmitting on air, so the UI
                # should keep showing retry progress for its duration instead of
                # jumping back to Idle while it's still audibly transmitting.
                # Cleared below once this attempt actually ends.
                giving_up = True
            elif was_transmitting and not tx and (giving_up or completing):
              current = None
              current_since = None
              current_retries = 0
              giving_up = False
              completing = False
            was_transmitting = tx

            sequence = SEQUENCE_TIME[packet.TXMode]
            frequency = packet.Frequency
            tx_status = any([packet.Transmitting, packet.TXEnabled])
            Status().state(frequency=frequency, band=get_band(frequency), mode=packet.TXMode,
                           tx_enabled=packet.TXEnabled, transmitting=packet.Transmitting,
                           decoding=packet.Decoding, current_call=current,
                           current_retry=min(current_retries, self.tx_retries),
                           max_retries=self.tx_retries)
            if packet.DXCall:
              LOG.debug("%s => TX: %s, TXEnabled: %s - TXWatchdog: %s", packet.DXCall,
                        packet.Transmitting, packet.TXEnabled, packet.TXWatchdog)
          case _:
            LOG.debug('Packet type "%r" not processed', packet)

      # Outside the for loop
      paused = Status().is_paused()
      if paused and not was_paused and tx_status and current:
        # Pausing only stops *new* calls from starting - WSJT-X keeps working an
        # already-initiated QSO autonomously otherwise, so abort it explicitly here,
        # the same way an exceeded-retries abort is handled.
        LOG.info("Paused: halting current transmission to %s", current)
        # Graceful, not immediate: an immediate halt appears to also uncheck WSJT-X's
        # own Enable Tx checkbox, which then stays off with no documented UDP command
        # to remotely re-enable it - graceful avoids that side effect.
        self.stop_transmit(ip_from)
        self.queue.put((DBCommand.STATUS,
                        {"call": current, "status": 3, "band": get_band(frequency)}))
        Status().reject(current, 'paused - automation stopped',
                        category='paused - automation stopped')
        Status().broken(current)
        current = None
        current_since = None
        current_retries = 0
        giving_up = False
        completing = False
      was_paused = paused

      # Safety net: if a QSO attempt has been "in progress" for far longer than any
      # legitimate exchange (including retries) should ever take, resolve it as
      # broken rather than let it - and the automation behind it - get stuck forever.
      # This is a backstop, not the primary way attempts resolve; it exists in case
      # some WSJT-X-side condition (e.g. its own Tx watchdog) ends a QSO in a way
      # none of the normal resolution paths above ever observe.
      if current and current_since and (datetime.utcnow() - current_since).total_seconds() > 300:
        LOG.info("QSO with %s timed out without resolving, giving up", current)
        self.queue.put((DBCommand.STATUS,
                        {"call": current, "status": 3, "band": get_band(frequency)}))
        Status().reject(current, 'timed out', category='timed out')
        Status().broken(current)
        current = None
        current_since = None
        current_retries = 0
        giving_up = False
        completing = False

      _now = datetime.utcnow()
      if _now.second in sequence:
        Status().clear_activity()
        # Evaluate every cycle regardless of tx_status, so candidates still get a
        # visible reason (e.g. "busy") instead of just aging out silently while we're
        # occupied with an existing QSO. Only actually act on the result when free.
        data = self.selector(get_band(frequency))
        if data:
          _pkt = data.get('packet') or {}
          _snr, _delta_freq = data.get('snr'), _pkt.get('DeltaFrequency')
          _mode, _message = _pkt.get('Mode'), _pkt.get('Message')
        if Status().is_paused():
          if data:
            Status().reject(data['call'], 'paused - automation stopped',
                            category='paused - automation stopped',
                            snr=_snr, delta_freq=_delta_freq, mode=_mode, message=_message)
        elif current is not None or tx_status:
          # Busy: either already mid-QSO with `current` (transmitting or waiting
          # for a reply) or WSJT-X reports Tx activity outside our tracking. Don't
          # let a fresh candidate steal focus - without this guard, if tx_status
          # ever briefly read False mid-QSO (a real, if narrow, timing window),
          # this would silently overwrite/clear `current` without ever resolving
          # the attempt, leaving it stuck "in progress" forever.
          if data:
            reason = f"busy with {current}" if current else "busy - currently transmitting"
            category = "busy with another QSO" if current else "busy - currently transmitting"
            Status().reject(data['call'], reason, category=category,
                            snr=_snr, delta_freq=_delta_freq, mode=_mode, message=_message)
        elif data:
          self.call_station(ip_from, data)
          current = data.get('call')
          current_since = datetime.utcnow()
          current_retries = 0
          giving_up = False
        time.sleep(1)


class LoadPlugins:

  def __init__(self, plugins):
    """Load and initialize plugins"""
    self.call_select = []
    if isinstance(plugins, str):
      plugins = [plugins]

    LOG.info('Call selector: %s', ', '.join(plugins))
    for plugin in plugins:
      *module_name, class_name = plugin.split('.')
      module_name = '.'.join(['plugins'] + module_name)
      module = import_module(module_name)
      try:
        klass = getattr(module, class_name)
      except AttributeError:
        LOG.error('Call selector plugin %s not found', class_name)
        raise SystemExit(f'"{class_name}" not found') from None
      self.call_select.append(klass())

  def __call__(self, band):
    for selector in self.call_select:
      data = selector.get(band)
      if not data:
        continue
      data['selector'] = selector.__class__.__name__
      LOG.debug('Select: %s, From: %s, SNR: %d, Distance: %dKm, Band: %dm, Selector: %s',
                data['call'], data['country'], data['snr'], data['distance'],
                data['band'], data['selector'])
      return data
    return None

  def __repr__(self):
    return '<LoadPlugins> ' + ', '.join(p.__class__.__name__ for p in self.call_select)


def get_log_level():
  loglevel = os.getenv('LOG_LEVEL', 'INFO').upper()
  if loglevel not in logging._nameToLevel:  # pylint: disable=protected-access
    logging.error('Log level "%s" does not exist, defaulting to INFO', loglevel)
    loglevel = logging.INFO
  return loglevel


def setup(config_path, console_handler):
  """Load config, set up logging/DB threads/plugins. Shared by the terminal and
  Qt entry points so neither has to duplicate startup order/behavior.
  `console_handler` is provided by the caller since the terminal dashboard and the
  Qt GUI each want log records routed differently."""
  # pylint: disable=global-statement
  global LOG

  config = Config(config_path)
  config = config['ft8ctrl']

  formatter = logging.Formatter(
    fmt='%(asctime)s - %(levelname)-7s %(lineno)3d:%(module)-8s - %(message)s',
    datefmt='%H:%M:%S',
  )
  LOG = logging.getLogger()
  LOG.setLevel(logging.DEBUG)

  console_handler.setFormatter(formatter)
  LOG.addHandler(console_handler)

  logfile_name = Path(getattr(config, 'logfile_name', LOGFILE_NAME)).expanduser()
  file_handler = RotatingFileHandler(logfile_name, maxBytes=LOGFILE_SIZE, backupCount=5)
  file_handler.setLevel(logging.DEBUG)
  file_handler.setFormatter(formatter)
  LOG.addHandler(file_handler)

  db_name = Path(config.db_name).expanduser()
  create_db(db_name)

  queue = Queue()
  try:
    db_thread = DBInsert(db_name, queue, config.my_grid)
    db_thread.daemon = True
    db_thread.start()
  except RuntimeError as err:
    LOG.error("Configuration error: %s", err)
    raise SystemExit('Configuration Error') from None

  db_purge = Purge(db_name, config.retry_time)
  db_purge.daemon = True
  db_purge.start()

  call_select = LoadPlugins(config.call_selector)

  Status().state(
    mycall=config.my_call,
    grid=config.my_grid,
    selectors=[s.__class__.__name__ for s in call_select.call_select],
    blacklist_size=len(BlackList().blacklist),
    wsjt_endpoint=f"{config.wsjt_ip}:{config.wsjt_port}",
    relay_endpoint=(f"{config.logger_ip}:{config.logger_port}"
                    if getattr(config, 'logger_ip', None) else None),
  )

  return config, queue, call_select


def main():
  parser = ArgumentParser(description="FT8Commander-NG - WSJT-X automation")
  parser.add_argument("-c", "--config", help="Name of the configuration file")
  parser.add_argument("--no-dashboard", action="store_true",
                      help="Disable the live dashboard and use plain console logging")
  opts = parser.parse_args()

  use_dashboard = sys.stdout.isatty() and not opts.no_dashboard
  if use_dashboard:
    console_handler = DashboardLogHandler()
    console_handler.setLevel(logging.WARNING)
  else:
    console_handler = logging.StreamHandler()
    console_handler.setLevel(get_log_level())

  config, queue, call_select = setup(opts.config, console_handler)

  dashboard = None
  if use_dashboard:
    dashboard = Dashboard(config.my_call)
    dashboard.start()

  try:
    main_loop = Sequencer(config, queue, call_select)
    main_loop.run()
  except OSError as err:
    LOG.error('%s - %s', config.wsjt_ip, err.strerror)
  except KeyboardInterrupt:
    LOG.info('^C pressed exiting')
  finally:
    if dashboard:
      dashboard.stop()


if __name__ == '__main__':
  main()
