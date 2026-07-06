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
from dbutils import DBCommand, DBInsert, Purge, create_db, get_band
from plugins.base import BlackList
from status import Status

SEQUENCE_TIME = {
  'FT8': {2, 17, 32, 47},
  'FT4': {0, 6, 12, 18, 24, 30, 36, 42, 48, 54},
}

PARSERS = {
  'REPLY': re.compile(r'^((?!CQ)(?P<to>\w+)(|/\w+)) (?P<call>\w+)(|/\w+) .*'),
  'CQ': re.compile(r'''^CQ\s(?:CQ\s|(?P<extra>[\S.]+)\s|)
                   (?P<call>\w+(|/\w+))\s
                   (?P<grid>[A-Z]{2}[0-9]{2})''', re.VERBOSE),
  'BROKENCQ': re.compile(r'^CQ\s(?P<call>\w+(|/\w+))$'),
}

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
    Status().select(data['call'], data['selector'])
    pkt = data['packet']
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

  def stop_transmit(self, ip_from):
    stop_pkt = wsjtx.WSHaltTx()
    stop_pkt.mode = True
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
    Status().worked(packet.DXCall, packet.DXGrid, country, get_band(frequency))
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
    current_retries = 0
    last_tx_message = ""
    was_transmitting = False
    sequence = []
    LOG.info('ft8ctl running...')

    while True:
      fds, _, _ = select.select([self.sock], [], [], .7)
      for fdin in fds:
        rawdata, ip_from = fdin.recvfrom(1024)
        packet = wsjtx.ft8_decode(rawdata)
        Status().heartbeat()
        match packet:
          case wsjtx.WSHeartbeat() | wsjtx.WSADIF():
            pass
          case wsjtx.WSLogged():
            self.log_call(packet)
            current = None
          case wsjtx.WSDecode():
            name, match = self.decode(packet)
            Status().raw_decode(packet.SNR, packet.DeltaFrequency, packet.Mode, packet.Message,
                                cycle=packet.Time, is_cq=(name == 'CQ'))
            if name == 'REPLY' and match['call'] == current and match['to'] != self.mycall:
              LOG.info("Stop Transmit: %s Replying to %s ", match['call'], match['to'])
              self.stop_transmit(ip_from)
              self.queue.put((DBCommand.DELETE,
                              {"call": match['call'], "band": get_band(frequency)}))
              Status().reject(match['call'], f"replied to {match['to']}",
                              category="replied to another station")
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
              Status().decode(match['call'], match.get('extra'), packet.SNR, match['band'],
                              match.get('grid'), distance, country)
              self.queue.put((DBCommand.INSERT, match))
            continue
          case wsjtx.WSStatus():
            tx = not packet.Decoding and packet.Transmitting
            if packet.Transmitting and packet.DXCall:
              self.queue.put(
                (DBCommand.STATUS,
                 {"call": packet.DXCall, "status": 1, "band": get_band(packet.Frequency)})
              )

            # WSJT-X sends multiple status packets during a single transmission.
            # Only count a retry on the False -> True edge, i.e. once per actual
            # transmission, instead of once per status packet.
            if tx and not was_transmitting:
              if last_tx_message != packet.TxMessage:
                current_retries = 0
                last_tx_message = packet.TxMessage
              current_retries += 1
              if current_retries >= self.tx_retries:
                LOG.info("Retries exceeded, stopping transmit")
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
                current_retries = 0
            was_transmitting = tx

            sequence = SEQUENCE_TIME[packet.TXMode]
            frequency = packet.Frequency
            tx_status = any([packet.Transmitting, packet.TXEnabled])
            Status().state(frequency=frequency, band=get_band(frequency), mode=packet.TXMode,
                          tx_enabled=packet.TXEnabled, transmitting=packet.Transmitting,
                          current_retry=current_retries, max_retries=self.tx_retries)
            if packet.DXCall:
              LOG.debug("%s => TX: %s, TXEnabled: %s - TXWatchdog: %s", packet.DXCall,
                        packet.Transmitting, packet.TXEnabled, packet.TXWatchdog)
          case _:
            LOG.debug('Packet type "%r" not processed', packet)

      # Outside the for loop
      if not tx_status:
        _now = datetime.utcnow()
        if _now.second in sequence:
          data = self.selector(get_band(frequency))
          if data:
            self.call_station(ip_from, data)
            current = data.get('call')
            current_retries = 0
          else:
            current = None
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
    grid=config.my_grid,
    selectors=[s.__class__.__name__ for s in call_select.call_select],
    blacklist_size=len(BlackList().blacklist),
    wsjt_endpoint=f"{config.wsjt_ip}:{config.wsjt_port}",
    relay_endpoint=(f"{config.logger_ip}:{config.logger_port}"
                    if getattr(config, 'logger_ip', None) else None),
  )

  return config, queue, call_select


def main():
  parser = ArgumentParser(description="ft8ctl wsjt-x automation")
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
