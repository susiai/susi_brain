"""Check buffering, append-only rotation, archive search and recovery without providers."""

import json
import os
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

import pytest

from susi_brain.shared.observability.daily_log import BufferedDailyLog, MAX_LINE_BYTES


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 25, 23, 59, 50, tzinfo=UTC).timestamp()
    def __call__(self):
        return self.now


def rows(directory):
    return [json.loads(line) for path in sorted(directory.glob('*.jsonl')) for line in path.read_text().splitlines()]


def test_log_only_buffers_and_flush_appends_without_replacing_the_file(tmp_path, monkeypatch):
    clock = Clock()
    log = BufferedDailyLog(tmp_path, clock=clock)
    original, flags = os.open, []
    def opened(path, value, *args, **kwargs):
        if str(path).endswith('.jsonl') and value & os.O_CREAT:
            flags.append(value)
        return original(path, value, *args, **kwargs)
    monkeypatch.setattr(os, 'open', opened)
    with log:
        for n in range(3):
            assert log.log({'message': str(n)}) == n + 1
        assert not list(tmp_path.glob('*.jsonl'))
        assert log.metadata()['buffered_entries'] == 3
        assert log.flush_interval == 60
        log.flush()
        path = tmp_path / 'log-2026-10-25.jsonl'
        original_bytes, inode = path.read_bytes(), path.stat().st_ino
        log.log('a second batch\nwith an embedded newline')
        assert path.read_bytes() == original_bytes
        log.flush()
        assert path.read_bytes().startswith(original_bytes)
        assert path.stat().st_ino == inode
        assert len(path.read_text().splitlines()) == 4
        modified = path.stat().st_mtime_ns
        log.flush()
        assert path.stat().st_mtime_ns == modified
    assert len(flags) == 2 and all(value & os.O_APPEND and not value & os.O_TRUNC for value in flags)


def test_midnight_flush_writes_only_the_current_utc_day_and_keeps_event_times(tmp_path):
    clock = Clock()
    with BufferedDailyLog(tmp_path, clock=clock) as log:
        log.log('earlier batch'); log.flush()
        yesterday = tmp_path / 'log-2026-10-25.jsonl'
        content, modified = yesterday.read_bytes(), yesterday.stat().st_mtime_ns
        clock.now += 9
        log.log('buffered before midnight')
        clock.now += 2
        log.flush()
        assert yesterday.read_bytes() == content and yesterday.stat().st_mtime_ns == modified
        today = json.loads((tmp_path / 'log-2026-10-26.jsonl').read_text())
        assert today['timestamp'].startswith('2026-10-25T23:59:59')
        assert log.search()['entries'][0]['data'] == 'buffered before midnight'


def test_worker_flushes_autonomously_and_close_drains_the_last_batch(tmp_path, monkeypatch):
    log, appended = BufferedDailyLog(tmp_path, flush_interval=.02), Event()
    original = log._append
    def append(*args):
        original(*args); appended.set()
    monkeypatch.setattr(log, '_append', append)
    log.log('automatic')
    assert appended.wait(2)
    log.log('shutdown')
    log.close()
    assert [row['data'] for row in rows(tmp_path)] == ['automatic', 'shutdown']
    assert not log.thread.is_alive()
    log.close()
    with pytest.raises(RuntimeError, match='closed'):
        log.log('late')


def test_search_covers_disk_beyond_cache_and_ram_with_stable_restart_cursors(tmp_path):
    clock = Clock()
    log = BufferedDailyLog(tmp_path, clock=clock, capacity=2)
    for n in range(6):
        log.log({'method': 'GET' if n % 2 else 'POST', 'message': 'Grüße ' + str(n)})
    log.flush()
    clock.now += 20
    log.log({'method': 'GET', 'message': 'still buffered'})
    assert log.metadata()['retained_entries'] == 7 and log.metadata()['cache_entries'] == 2
    assert [entry['sequence'] for entry in log.search(limit=2)['entries']] == [7, 6]
    older = log.search(limit=2, before=6)
    assert [entry['sequence'] for entry in older['entries']] == [5, 4]
    assert older['next_before'] == 4
    assert [entry['sequence'] for entry in log.search(query='GRÜSSE', filters={'method': 'GET'})['entries']] == [6, 4, 2]
    assert log.search(query='Grüße.*')['entries'] == []
    assert [entry['sequence'] for entry in log.search(since=datetime(2026, 10, 26, tzinfo=UTC))['entries']] == [7]
    assert len(log.search(until=datetime(2026, 10, 26, tzinfo=UTC))['entries']) == 6
    log.close()
    with BufferedDailyLog(tmp_path, capacity=2, clock=clock) as restored:
        assert restored.log('new process') == 8
        assert [entry['sequence'] for entry in restored.search(before=older['next_before'])['entries']] == [3, 2, 1]
        assert restored.metadata()['retained_entries'] == 8


def test_recent_cache_pages_need_no_archive_reads(tmp_path, monkeypatch):
    with BufferedDailyLog(tmp_path, capacity=3) as log:
        for _ in range(8):
            log.log('record')
        log.flush()
        monkeypatch.setattr(log, '_reverse', lambda *_: pytest.fail('Latest pages should be served from the cache.'))
        assert len(log.search(limit=2)['entries']) == 2


@pytest.mark.parametrize('partial', [False, True])
def test_failed_append_keeps_the_buffer_and_retry_does_not_duplicate_written_rows(tmp_path, monkeypatch, partial):
    log = BufferedDailyLog(tmp_path, capacity=1)
    for n in range(3):
        log.log({'message': str(n)})
    original = log._append
    def failed(path, payload):
        if partial:
            with path.open('ab') as target:
                first, remaining = payload.split(b'\n', 1)
                target.write(first + b'\n' + remaining[:15])
        else:
            original(path, payload)
        raise OSError('private-disk-error')
    monkeypatch.setattr(log, '_append', failed)
    with pytest.raises(OSError):
        log.flush()
    assert log.metadata()['buffered_entries'] == 3 and log.metadata()['error'] == 'append_failed'
    assert len(log.search()['entries']) == 3
    monkeypatch.setattr(log, '_append', original)
    log.flush()
    assert log.metadata()['buffered_entries'] == 0 and log.metadata()['error'] is None
    log.close()
    with BufferedDailyLog(tmp_path, capacity=1) as restored:
        assert [entry['sequence'] for entry in restored.search()['entries']] == [3, 2, 1]
        assert restored.metadata()['retained_entries'] == 3
        assert restored.metadata()['ignored_lines'] == int(partial)


def test_corrupt_oversized_and_truncated_lines_are_skipped_without_rewriting(tmp_path):
    clock = Clock()
    with BufferedDailyLog(tmp_path, clock=clock) as initial:
        initial.log('valid first line')
    path = tmp_path / 'log-2026-10-25.jsonl'
    with path.open('ab') as target:
        target.write(b'not json\n' + b'x' * (MAX_LINE_BYTES * 3) + b'\n' + b'{"truncated":')
    damaged = path.read_bytes()
    with BufferedDailyLog(tmp_path, capacity=1, clock=clock) as recovered:
        assert recovered.metadata()['ignored_lines'] == 3
        assert recovered.log('valid next line') == 2
        recovered.flush()
        assert path.read_bytes().startswith(damaged + b'\n')
        assert [entry['data'] for entry in recovered.search()['entries']] == ['valid next line', 'valid first line']


def test_log_and_search_detach_caller_owned_data(tmp_path):
    with BufferedDailyLog(tmp_path) as log:
        value = {'nested': {'message': 'original'}}
        log.log(value)
        value['nested']['message'] = 'modified'
        first = log.search()['entries'][0]
        first['data']['nested']['message'] = 'modified again'
        assert log.search()['entries'][0]['data']['nested']['message'] == 'original'


def test_concurrent_callers_get_unique_ordered_ids_without_per_request_disk_writes(tmp_path):
    with BufferedDailyLog(tmp_path, capacity=5) as log:
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(lambda n: log.log({'n': n}), range(300)))
        assert sorted(ids) == list(range(1, 301))
        assert not list(tmp_path.glob('*.jsonl'))
        assert log.metadata()['buffered_entries'] == 300
    assert len(rows(tmp_path)) == 300


def test_a_directory_has_only_one_writer_until_close(tmp_path):
    first, second = BufferedDailyLog(tmp_path), BufferedDailyLog(tmp_path)
    first.log('first writer')
    with pytest.raises(RuntimeError, match='active writer'):
        second.start()
    first.close()
    second.start()
    assert second.log('next writer') == 2
    second.close()


def test_daily_file_symlinks_are_never_followed(tmp_path):
    directory = tmp_path / 'logs'
    outside = tmp_path / 'unrelated.txt'; outside.write_text('keep')
    clock = Clock()
    log = BufferedDailyLog(directory, clock=clock)
    log.log('buffered')
    link = directory / 'log-2026-10-25.jsonl'; link.symlink_to(outside)
    with pytest.raises(OSError):
        log.flush()
    assert outside.read_text() == 'keep' and log.metadata()['buffered_entries'] == 1
    link.unlink()
    log.close()


def test_invalid_entries_do_not_consume_ids_or_replace_prior_logs(tmp_path):
    with BufferedDailyLog(tmp_path) as log:
        log.log('valid')
        for value in ({'bad': float('nan')}, 'x' * MAX_LINE_BYTES, ['not an object or string']):
            with pytest.raises(ValueError):
                log.log(value)
        assert log.log('next valid') == 2


def test_normal_interpreter_exit_drains_a_log_used_without_a_context_manager(tmp_path):
    source = str(Path(__file__).resolve().parents[1] / 'src')
    code = '''import sys
sys.path.insert(0, sys.argv[1])
from susi_brain.shared.observability.daily_log import BufferedDailyLog
log = BufferedDailyLog(sys.argv[2])
log.log('saved on normal exit')
'''
    subprocess.run([sys.executable, '-I', '-c', code, source, str(tmp_path)], check=True, timeout=5)
    assert rows(tmp_path)[0]['data'] == 'saved on normal exit'


def test_search_orders_sequence_ids_even_when_the_wall_clock_crosses_back_to_an_older_day(tmp_path):
    clock = Clock()
    with BufferedDailyLog(tmp_path, capacity=1, clock=clock) as log:
        log.log('day one'); log.flush()
        clock.now += 86400
        log.log('day two'); log.flush()
        clock.now -= 86400
        log.log('day one again'); log.flush()
        assert [entry['sequence'] for entry in log.search()['entries']] == [3, 2, 1]
        assert [entry['sequence'] for entry in log.search(limit=1, before=3)['entries']] == [2]
