from contextvars import copy_context
import threading
import time

from astrid.sdk.command_timing import TimingCollector, stage, timing_scope


def test_overlapping_contexts_do_not_double_count_or_crash():
    collector = TimingCollector(started=time.monotonic())
    both = threading.Barrier(2)
    release = threading.Event()
    with timing_scope(collector):
        def first():
            with stage('first'):
                both.wait()
                release.wait()

        thread = threading.Thread(target=copy_context().run, args=(first,))
        thread.start()
        with stage('second'):
            both.wait()
            time.sleep(0.05)
            release.set()
            thread.join()
    rows = collector.snapshot()
    assert {row['stage'] for row in rows} >= {'first', 'second'}
    assert all(row['seconds'] >= 0 for row in rows)
    assert sum(row['seconds'] for row in rows) < 0.2


def test_long_parent_wait_is_reported_across_short_children(capsys):
    collector = TimingCollector(started=time.monotonic())
    with timing_scope(collector):
        with stage('parent'):
            with stage('first child'):
                time.sleep(1.1)
            with stage('second child'):
                time.sleep(1.1)
                assert 'Waiting for parent' in capsys.readouterr().err
