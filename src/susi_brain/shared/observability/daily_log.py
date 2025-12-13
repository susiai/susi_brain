"""Buffered JSONL logging, daily append-only files and bounded-memory search."""

import atexit
import fcntl
import heapq
import json
import logging
import os
import re
import stat
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, RLock, Thread, current_thread

MAX_LINE_BYTES = 64 * 1024


def utc(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


class BufferedDailyLog:
    """Call log(data); this class owns buffering, rotation, flushing and search.

    A directory has one writer, protected by a lifetime advisory lock. Files are
    named by the UTC day at flush time, even when buffered events predate midnight.
    Normal writes happen every 60 seconds; close() also drains the final batch.
    directory=None provides a bounded in-memory store for isolated callers.
    """

    def __init__(self, directory, *, prefix='log', capacity=10000, flush_interval=60,
                 clock=None, sequence_field=None, validate=None, on_restore=None):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', prefix) or capacity < 1 or flush_interval <= 0:
            raise ValueError('Invalid daily log configuration.')
        self.directory = Path(directory) if directory is not None else None
        self.prefix, self.capacity, self.flush_interval = prefix, capacity, flush_interval
        self.clock = clock or time.time
        self.sequence_field, self.validate, self.on_restore = sequence_field, validate, on_restore
        self.lock, self.flush_lock, self.stop_event = RLock(), Lock(), Event()
        self.cache, self.pending = deque(maxlen=capacity), deque()
        self.sequence = self.total = self.ignored_lines = 0
        self.files = {}
        self.started = self.closed = self.closing = self.retry = False
        self.thread = self.lock_fd = None
        self.last_flush_at = self.last_error = None

    def _paths(self):
        pattern = re.compile(re.escape(self.prefix) + r'-\d{4}-\d{2}-\d{2}\.jsonl')
        return sorted(path for path in self.directory.iterdir()
                      if pattern.fullmatch(path.name) and path.is_file() and not path.is_symlink())

    def _open(self, path, flags, mode=0o600):
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError('A daily log file must be a regular file.')
        return fd

    def _parse(self, line):
        try:
            if len(line) > MAX_LINE_BYTES:
                return None
            entry = json.loads(line)
            if (not isinstance(entry, dict) or set(entry) != {'sequence', 'timestamp', 'data'}
                    or type(entry['sequence']) is not int or entry['sequence'] < 1
                    or not isinstance(entry['timestamp'], str)
                    or datetime.fromisoformat(entry['timestamp']).tzinfo is None
                    or not isinstance(entry['data'], (dict, str))
                    or self.sequence_field is not None and (not isinstance(entry['data'], dict)
                        or entry['data'].get(self.sequence_field) != entry['sequence'])
                    or self.validate is not None and not self.validate(entry['data'])):
                return None
            json.dumps(entry, allow_nan=False)
            return entry
        except (ValueError, TypeError, OverflowError, RecursionError):
            return None

    def _forward(self, path):
        with os.fdopen(self._open(path, os.O_RDONLY), 'rb') as source:
            while line := source.readline(MAX_LINE_BYTES + 1):
                if len(line) > MAX_LINE_BYTES:
                    while line and not line.endswith(b'\n'):
                        line = source.readline(MAX_LINE_BYTES + 1)
                    yield None
                elif line.strip():
                    yield self._parse(line)

    def _reverse(self, path):
        with os.fdopen(self._open(path, os.O_RDONLY), 'rb') as source:
            position, tail, oversized = source.seek(0, os.SEEK_END), b'', False
            while position:
                size = min(MAX_LINE_BYTES, position)
                position -= size
                source.seek(position)
                lines = source.read(size).split(b'\n')
                if len(lines) == 1:
                    if not oversized:
                        tail = lines[0] + tail
                        if len(tail) > MAX_LINE_BYTES:
                            tail, oversized = b'', True
                    continue
                complete = ([lines[-1] + tail] if not oversized else []) + list(reversed(lines[1:-1]))
                for line in complete:
                    if line.strip():
                        entry = self._parse(line)
                        if entry is not None:
                            yield entry
                tail, oversized = lines[0], False
            if tail.strip() and not oversized:
                entry = self._parse(tail)
                if entry is not None:
                    yield entry

    def start(self):
        with self.lock:
            if self.closed or self.closing:
                raise RuntimeError('The daily log is closed.')
            if self.started:
                return
            if self.directory is not None:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = self._open(self.directory / ('.' + self.prefix + '.lock'), os.O_RDWR | os.O_CREAT)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    raise RuntimeError('The daily log directory already has an active writer.') from None
                self.lock_fd = fd
                try:
                    latest = []
                    for path in self._paths():
                        maximum = 0
                        for entry in self._forward(path):
                            if entry is None or entry['sequence'] <= maximum:
                                self.ignored_lines += 1
                                continue
                            maximum = entry['sequence']
                            self.sequence = max(self.sequence, maximum)
                            self.total += 1
                            # The serial tie breaker tolerates manually duplicated IDs.
                            item = (maximum, self.total, entry)
                            if len(latest) < self.capacity:
                                heapq.heappush(latest, item)
                            elif maximum > latest[0][0]:
                                heapq.heapreplace(latest, item)
                            if self.on_restore is not None:
                                self.on_restore(entry['data'])
                        self.files[path] = maximum
                    self.cache.extend(item[2] for item in sorted(latest))
                except BaseException:
                    os.close(fd)
                    self.lock_fd = None
                    raise
            self.started = True
            if self.directory is not None:
                atexit.register(self.close)
                self.thread = Thread(target=self._run, name='daily-log-' + self.prefix, daemon=True)
                self.thread.start()

    def _run(self):
        while not self.stop_event.wait(self.flush_interval):
            try:
                self.flush()
            except OSError:
                logging.getLogger(__name__).error('Daily log flush failed; buffered entries retained.')

    def log(self, data, *, at=None):
        """Queue a JSON object/string; optional at is its event time in epoch seconds."""
        self.start()
        with self.lock:
            if self.closed or self.closing:
                raise RuntimeError('The daily log is closed.')
            sequence = self.sequence + 1
            # Round trip to detach caller-owned dictionaries and validate JSON values.
            data = json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
            if self.sequence_field is not None:
                data[self.sequence_field] = sequence
            entry = {'sequence': sequence, 'timestamp': utc(self.clock() if at is None else at), 'data': data}
            encoded = (json.dumps(entry, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode()
            if len(encoded) > MAX_LINE_BYTES or self._parse(encoded) is None:
                raise ValueError('A log entry must be valid JSON and fit within 64 KiB.')
            self.sequence = sequence
            self.total += 1
            self.cache.append(entry)
            if self.directory is not None:
                self.pending.append((entry, encoded))
            return sequence

    def _append(self, path, payload):
        with os.fdopen(self._open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT), 'ab', buffering=0) as output:
            size = os.fstat(output.fileno()).st_size
            if size and os.pread(output.fileno(), 1, size - 1) != b'\n':
                payload = b'\n' + payload  # Keep a crash-truncated tail separate; never rewrite it.
            remaining = memoryview(payload)
            while remaining:
                written = output.write(remaining)
                if not written:
                    raise OSError('The daily log append made no progress.')
                remaining = remaining[written:]
            os.fsync(output.fileno())
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def flush(self):
        """Append the pending batch to today's file, preserving the queue on error."""
        if self.directory is None:
            return
        with self.flush_lock:
            with self.lock:
                batch = list(self.pending)
            if not batch:
                return
            path = self.directory / (self.prefix + '-' + datetime.fromtimestamp(self.clock(), UTC).date().isoformat() + '.jsonl')
            try:
                found = set()
                if self.retry:
                    # A failed append/fsync may already have written complete lines.
                    # Recovery reads only; matching IDs are not appended a second time.
                    wanted = {entry['sequence'] for entry, _ in batch}
                    for existing in self._paths():
                        maximum = 0
                        matched = False
                        for entry in self._forward(existing):
                            if entry is not None:
                                maximum = max(maximum, entry['sequence'])
                                if entry['sequence'] in wanted:
                                    found.add(entry['sequence'])
                                    matched = True
                        if matched:
                            fd = self._open(existing, os.O_RDWR)
                            try:
                                os.fsync(fd)
                            finally:
                                os.close(fd)
                        with self.lock:
                            self.files[existing] = maximum
                payload = b''.join(encoded for entry, encoded in batch if entry['sequence'] not in found)
                if payload:
                    self._append(path, payload)
                with self.lock:
                    if payload:
                        self.files[path] = max(self.files.get(path, 0), batch[-1][0]['sequence'])
                    for _ in batch:
                        self.pending.popleft()
                    self.last_flush_at, self.last_error, self.retry = utc(self.clock()), None, False
            except OSError:
                with self.lock:
                    self.last_error, self.retry = 'append_failed', True
                raise

    def search(self, *, limit=100, before=None, query=None, filters=None, exclude=None, since=None, until=None):
        """Search disk plus RAM, newest sequence first, with a stable exclusive cursor.

        query is a case-insensitive literal JSON-text search, never a regex/shell.
        filters/exclude match dictionary fields; since is inclusive, until exclusive.
        Cached latest pages avoid disk reads; older searches stream bounded blocks.
        """
        if limit < 1:
            raise ValueError('Search limit must be positive.')
        if not self.started and not self.closed:
            self.start()
        with self.lock:
            cache, pending = list(self.cache), list(self.pending)
            files, upper, total = self.files.copy(), self.sequence, self.total
        query = query.casefold() if query else None

        def matches(entry):
            if entry['sequence'] > upper or before is not None and entry['sequence'] >= before:
                return False
            value, at = entry['data'], datetime.fromisoformat(entry['timestamp'])
            if since is not None and at < since or until is not None and at >= until:
                return False
            if filters and (not isinstance(value, dict) or any(value.get(key) != item for key, item in filters.items())):
                return False
            if exclude and isinstance(value, dict) and any(value.get(key) == item for key, item in exclude.items()):
                return False
            return not query or query in json.dumps(value, ensure_ascii=False).casefold()

        rows = []
        for entry in reversed(cache):
            if matches(entry):
                rows.append(entry)
                if len(rows) > limit:
                    break
        if self.directory is not None and len(rows) <= limit and total > len(cache):
            found, best = {}, []

            def offer(entry):
                key = entry['sequence']
                if key in found:
                    return
                if len(best) < limit + 1:
                    heapq.heappush(best, key)
                elif key > best[0]:
                    del found[heapq.heapreplace(best, key)]
                else:
                    return
                found[key] = entry

            for entry, _ in pending:
                if matches(entry):
                    offer(entry)
            for path, maximum in sorted(files.items(), key=lambda item: item[1], reverse=True):
                if len(best) > limit and maximum < best[0]:
                    break
                for entry in self._reverse(path):
                    if len(best) > limit and entry['sequence'] < best[0]:
                        break
                    if matches(entry):
                        offer(entry)
            rows = [found[key] for key in sorted(found, reverse=True)]
        page = rows[:limit]
        return {'entries': json.loads(json.dumps(page)),
                'next_before': page[-1]['sequence'] if len(rows) > limit else None}

    def metadata(self):
        with self.lock:
            durable = self.directory is not None
            return {'storage': 'daily-jsonl' if durable else 'process-local', 'cache_entries': len(self.cache),
                    'retained_entries': self.total if durable else len(self.cache),
                    'evicted_entries': 0 if durable else max(0, self.total - len(self.cache)),
                    'buffered_entries': len(self.pending), 'flush_interval_seconds': self.flush_interval,
                    'last_flush_at': self.last_flush_at, 'error': self.last_error, 'ignored_lines': self.ignored_lines}

    def close(self):
        with self.lock:
            if self.closed or self.closing:
                return
            self.closing = True
        self.stop_event.set()
        if self.thread is not None and self.thread is not current_thread():
            self.thread.join()
        try:
            self.flush()
        finally:
            with self.lock:
                self.closed = True
                atexit.unregister(self.close)
                if self.lock_fd is not None:
                    os.close(self.lock_fd)
                    self.lock_fd = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.close()
