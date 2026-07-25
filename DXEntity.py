#
# BSD 3-Clause License
#
# Copyright (c) 2023, Fred W6BSD
# All rights reserved.
#
# pylint: disable=invalid-name

import dbm.gnu as dbm
import http.client
import logging
import marshal
import os
import plistlib
import time
from collections import defaultdict
from functools import lru_cache
from urllib.error import URLError
from urllib.request import urlretrieve

CTY_URL = "https://www.country-files.com/cty/cty.plist"
CTY_HOME = "/tmp"
CTY_FILE = "cty.plist"
CTY_DB = "cty.db"
CTY_EXPIRE = 86400 * 7          # One week

LRU_CACHE_SIZE = 8192

class DXCCRecord:
  # pylint: disable=too-few-public-methods
  __slots__ = ['prefix', 'country', 'continent', 'cqzone', 'ituzone', 'latitude', 'longitude',
               'gmtoffset']

  def __init__(self, record):
    for key, val in record.items():
      try:
        setattr(self, key.lower(), val)
      except AttributeError:
        pass

  def __repr__(self):
    buffer = ', '.join([f"{f}: {getattr(self, f)}" for f in DXCCRecord.__slots__])
    return f"<DXCCRecord> {buffer}"


class DXCC:

  def __init__(self, db_path=CTY_HOME):
    self._entities = defaultdict(set)
    self._max_len = 0
    self.get_prefix = lru_cache(maxsize=LRU_CACHE_SIZE)(self.get_prefix)
    self._db = os.path.join(os.path.expanduser(db_path), CTY_DB)
    cty_file = os.path.join(os.path.expanduser(db_path), CTY_FILE)

    try:
      fstat = os.stat(self._db)
      fresh = fstat.st_mtime + CTY_EXPIRE > time.time()
    except FileNotFoundError:
      fresh = False

    if fresh and self._load_cache():
      return

    logging.info('Download %s', cty_file)
    if self.load_cty(cty_file) and self._build_cache(cty_file):
      return

    # Download (or parsing the download) failed - country lookups are a
    # non-essential nice-to-have elsewhere in this app (already handled with a
    # plain KeyError at every call site), so fall back to whatever cache is on
    # disk, even if stale, rather than let a transient network error crash
    # startup entirely. If there's no cache at all yet, entities/max_len just
    # stay at the empty defaults set above.
    logging.warning('DXEntity: using stale/empty cache after download failure')
    self._load_cache()

  def _load_cache(self):
    """Load entities/max_len from the on-disk dbm cache, if present and
    readable. Returns True on success."""
    try:
      with dbm.open(self._db, 'r') as cdb:
        self._entities, self._max_len = marshal.loads(cdb['_meta_data_'])
      logging.info('Using DXCC cache %s', self._db)
      return True
    except FileNotFoundError:
      logging.error('DXEntity cache not found')
    except dbm.error as err:
      logging.error(err)
    return False

  def _build_cache(self, cty_file):
    """Parse the freshly downloaded cty file and (re)build the dbm cache from
    it. Returns True on success."""
    try:
      with open(cty_file, 'rb') as fdc:
        cty_data = plistlib.load(fdc)
      self._max_len = max(len(k) for k in cty_data)

      logging.info('Create cty cache: %s', self._db)
      with dbm.open(self._db, 'c') as cdb:
        for key, val in cty_data.items():
          cdb[key] = marshal.dumps(val)
          self._entities[val['Country']].add(key)
        cdb['_meta_data_'] = marshal.dumps([dict(self._entities), self._max_len])
      return True
    except (OSError, plistlib.InvalidFileException, dbm.error) as err:
      logging.error('DXEntity: failed to build cache from %s: %s', cty_file, err)
      return False

  def lookup(self, call):
    _, info = self.get_prefix(call)
    return info

  def get_prefix(self, call):
    call = call.upper()
    prefixes = list({call[:c] for c in range(self._max_len, 0, -1)})
    prefixes.sort(key=lambda x: -len(x))
    try:
      with dbm.open(self._db, 'r') as cdb:
        for prefix in prefixes:
          if prefix in cdb:
            return (prefix, DXCCRecord(marshal.loads(cdb[prefix])))
    except dbm.error as err:
      # No usable cache on disk at all (e.g. first run with no network yet) -
      # every caller already treats "not found" as a normal, expected case.
      logging.debug('DXEntity cache unavailable for lookup: %s', err)
    raise KeyError(f"{call} not found")

  def isentity(self, country):
    if country in self._entities:
      return True
    return False

  @property
  def entities(self):
    return self._entities

  def get_entity(self, key):
    if key in self._entities:
      return self._entities[key]
    raise KeyError(f'Entity {key} not found')

  def __str__(self):
    return f"{self.__class__} {id(self)} ({self._db})"

  def __repr__(self):
    return str(self)

  @staticmethod
  def load_cty(cty_file):
    """Returns True on success. Network hiccups here (DNS failure, timeout, the
    host closing the connection mid-download...) shouldn't be able to crash the
    whole app over a non-essential country-lookup refresh - caller falls back to
    a stale cache, or an empty one, rather than propagating this."""
    cty_tmp = cty_file + '.tmp'
    try:
      urlretrieve(CTY_URL, cty_tmp)
      if os.path.exists(cty_file):
        os.unlink(cty_file)
      os.rename(cty_tmp, cty_file)
      return True
    except (URLError, OSError, http.client.HTTPException) as err:
      logging.error('DXEntity: failed to download %s: %s', CTY_URL, err)
      return False
      return
