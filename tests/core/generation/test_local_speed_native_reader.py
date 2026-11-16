"""Synthetic parser oracles; real native calibration is a separate CI proof."""
from pathlib import Path
import pytest
from tests._local_speed_native import (
    FILE_IO, THREAD, count_interval, seconds_ns, timestamp_ns, trace_etw, trace_strace,
)


def test_complete_linux_flushes_include_sqlite_style_fdatasync_and_descendants(tmp_path):
    (tmp_path / "trace.10").write_text(
        '1.000000 clone(child_stack=NULL, flags=SIGCHLD) = 20 <0.000010>\n'
        '1.100000 fsync(3) = 0 <0.001000>\n'
        '1.200000 clone(child_stack=0, flags=CLONE_VM|CLONE_THREAD) = 11 <0.000010>\n'
        '2.000000 +++ exited with 0 +++\n')
    (tmp_path / "trace.11").write_text('1.300000 fdatasync(5) = 0 <0.002000>\n2.000000 +++ exited with 0 +++\n')
    (tmp_path / "trace.20").write_text('1.400000 fsync(6) = -1 EIO (I/O error) <0.003000>\n2.000000 +++ exited with 0 +++\n')
    result = count_interval(trace_strace(tmp_path), {"pid": 10, "epoch_start_ns": seconds_ns('1.05'), "epoch_reply_ns": seconds_ns('1.9')})
    assert result["native_flushes"] == 2
    assert result["observed_failed_flushes"] == 1
    assert result["by_process"] == {"10": 2}


def test_resumed_fork_and_flush_are_joined_not_dropped(tmp_path):
    (tmp_path / 'trace.1').write_text(
        '1.000000 vfork( <unfinished ...>\n'
        '1.100000 <... vfork resumed>) = 2 <0.100000>\n'
        '1.200000 fsync(3 <unfinished ...>\n'
        '1.300000 <... fsync resumed>) = 0 <0.100000>\n'
        '2.000000 +++ exited with 0 +++\n')
    (tmp_path / 'trace.2').write_text('1.400000 fdatasync(3) = 0 <0.001000>\n2.000000 +++ exited with 0 +++\n')
    result = count_interval(trace_strace(tmp_path), {"pid": 1, "epoch_start_ns": 0, "epoch_reply_ns": seconds_ns('2')})
    assert result['native_flushes'] == 2
    assert result['by_process'] == {'1': 1, '2': 1}


def test_flush_finishing_after_reply_is_not_pre_reply_work(tmp_path):
    (tmp_path / 'trace.10').write_text('1.000000 fsync(3) = 0 <1.000000>\n3.000000 +++ exited with 0 +++\n')
    result = count_interval(trace_strace(tmp_path), {"pid": 10, "epoch_start_ns": 0, "epoch_reply_ns": seconds_ns('1.5')})
    assert result['native_flushes'] == 0


@pytest.mark.parametrize('text', [
    '1.000000 fsync(3 <unfinished ...>\n',
    '1.000000 fsync(3) = 0\n2.000000 +++ exited with 0 +++\n',
    '1.000000 <... fsync resumed>) = 0 <0.100000>\n2.000000 +++ exited with 0 +++\n',
])
def test_incomplete_linux_trace_is_unknown_not_zero(tmp_path, text):
    (tmp_path / 'trace.1').write_text(text)
    result = count_interval(trace_strace(tmp_path), {"pid": 1, "epoch_start_ns": 0, "epoch_reply_ns": seconds_ns('3')})
    assert result['native_flushes'] is None and result['coverage_gaps']


def event(provider, opcode, timestamp, fields, pid=10, tid=11):
    data = ''.join(f'<Data Name="{key}">{value}</Data>' for key, value in fields.items())
    return (f'<Event><System><Provider Guid="{{{provider}}}"/><Opcode>{opcode}</Opcode>'
            f'<TimeCreated SystemTime="{timestamp}"/><Execution ProcessID="{pid}" ThreadID="{tid}"/>'
            f'</System><EventData>{data}</EventData></Event>')


def test_etw_flush_requires_matching_successful_completion_and_thread_identity(tmp_path):
    start, end = '2026-11-16T12:00:00.1000000Z', '2026-11-16T12:00:00.2000000Z'
    path, summary = tmp_path/'trace.xml', tmp_path/'summary.txt'
    summary.write_text('Events Lost: 0\nBuffers Lost: 0\n')
    path.write_text('<Events>' +
        event(THREAD, 1, '2026-11-16T12:00:00.0000000Z', {'ProcessId': 10, 'TThreadId': 11}) +
        event(FILE_IO, 73, start, {'IrpPtr':'0x1','TTID':11},pid=4) +
        event(FILE_IO, 76, end, {'IrpPtr':'0x1','NtStatus':'0x0'},pid=4) + '</Events>')
    # This oracle uses the actual documented ThreadId property spelling.
    path.write_text(path.read_text().replace('TThreadId','ThreadId'))
    trace = trace_etw(path, summary)
    result = count_interval(trace, {'pid':10,'epoch_start_ns':timestamp_ns(start),'epoch_reply_ns':timestamp_ns(end)})
    assert result['native_flushes'] == 1
    assert result['by_process'] == {'10':1}


def test_etw_entry_without_completion_is_not_durability_proof(tmp_path):
    path, summary = tmp_path/'trace.xml', tmp_path/'summary.txt'
    summary.write_text('Events Lost: 0\n')
    path.write_text('<Events>'+event(FILE_IO,73,'2026-11-16T12:00:00.0000000Z',{'IrpPtr':'1','TTID':11})+'</Events>')
    trace = trace_etw(path,summary)
    assert trace['events'] == []
    assert any('lack completion' in gap for gap in trace['gaps'])


def test_etw_missing_or_positive_loss_accounting_is_unknown(tmp_path):
    path, summary = tmp_path/'trace.xml', tmp_path/'summary.txt'
    path.write_text('<Events/>')
    assert 'ETW loss accounting unavailable' in trace_etw(path,summary)['gaps']
    summary.write_text('Events Lost: 3\n')
    assert 'ETW reports lost events or buffers' in trace_etw(path,summary)['gaps']


def test_etw_timestamp_keeps_100ns_precision():
    assert timestamp_ns('2026-11-16T12:00:00.1234567Z') - timestamp_ns('2026-11-16T12:00:00Z') == 123456700
