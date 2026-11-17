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


def classic_event(provider, opcode, timestamp, data, *, pid=4, tid=99, version=3, raw=None):
    body = ('<ProcessingErrorData><ErrorCode>15005</ErrorCode><EventPayload>' + raw + '</EventPayload></ProcessingErrorData>'
            if raw is not None else '<EventData>' + ''.join(f'<Data Name="{k}">{v}</Data>' for k, v in data.items()) + '</EventData>')
    return (f'<Event><System><Provider Guid="{{9e814aad-3204-11d2-9a82-006008a86939}}"/>'
            f'<Version>{version}</Version><Opcode>{opcode}</Opcode><TimeCreated SystemTime="{timestamp}"/>'
            f'<Execution ProcessID="{pid}" ThreadID="{tid}"/></System>{body}'
            '<RenderingInfo><Opcode>Display text, not the opcode</Opcode><Provider>MSNT_SystemTrace</Provider></RenderingInfo>'
            f'<ExtendedTracingInfo><EventGuid>{{{provider}}}</EventGuid></ExtendedTracingInfo></Event>')


def test_actual_tracerpt_classic_shape_preserves_guid_opcode_thread_prefix_and_loss(tmp_path):
    # Same wire shape as retained Windows run 43900844744; synthetic identities.
    path, summary = tmp_path / 'trace.xml', tmp_path / 'summary.txt'
    at = '2026-11-16T12:00:00.100000000+00:00'
    end = '2026-11-16T12:00:00.200000000+00:00'
    header = classic_event('68fdd900-4a3e-11d1-84f4-0000f80464e3', 0, at,
                           {'EventsLost': 0, 'BuffersLost': 0})
    thread = classic_event(THREAD, 1, at, {}, raw='0a0000000b00000000000000')
    flush = classic_event(FILE_IO, 73, at, {'IrpPtr': '0x100', 'TTID': 11})
    done = classic_event(FILE_IO, 76, end, {'IrpPtr': '0x100', 'NtStatus': 0})
    summary.write_text('Total Events  Lost      0\n')
    path.write_text('<Events>' + header + thread + flush + done + '</Events>')
    trace = trace_etw(path, summary)
    result = count_interval(trace, {'pid': 10, 'epoch_start_ns': timestamp_ns(at), 'epoch_reply_ns': timestamp_ns(end)})
    assert result['coverage_gaps'] == []
    assert result['native_flushes'] == 1
    assert result['by_process'] == {'10': 1}
    assert trace['loss_values'] == {'events': [0, 0], 'buffers': [0]}


def test_tracerpt_thread_prefix_must_be_long_enough_and_known_version(tmp_path):
    path, summary = tmp_path / 'trace.xml', tmp_path / 'summary.txt'
    summary.write_text('Events Lost: 0\nBuffers Lost: 0\n')
    path.write_text('<Events>' + classic_event(THREAD, 1, '2026-11-16T12:00:00Z', {}, raw='0a000000') + '</Events>')
    assert 'invalid Thread ProcessId/TThreadId prefix' in trace_etw(path, summary)['gaps']
    path.write_text('<Events>' + classic_event(THREAD, 1, '2026-11-16T12:00:00Z', {}, version=99, raw='0a0000000b000000') + '</Events>')
    assert 'tracerpt could not decode this event' in trace_etw(path, summary)['gaps']


def test_etw_header_cannot_hide_lost_buffers_behind_zero_event_summary(tmp_path):
    path, summary = tmp_path / 'trace.xml', tmp_path / 'summary.txt'
    summary.write_text('Total Events Lost 0\n')
    path.write_text('<Events>' + classic_event('68fdd900-4a3e-11d1-84f4-0000f80464e3', 0,
        '2026-11-16T12:00:00Z', {'EventsLost': 0, 'BuffersLost': 1}) + '</Events>')
    assert 'ETW reports lost events or buffers' in trace_etw(path, summary)['gaps']


def test_etw_offset_time_and_z_time_are_identical_without_precision_loss():
    value = timestamp_ns('2026-11-16T12:00:00.123456700Z')
    assert timestamp_ns('2026-11-16T12:00:00.123456700+00:00') == value
    assert timestamp_ns('2026-11-16T13:00:00.123456700+01:00') == value
    assert timestamp_ns('2026-11-16T07:00:00.123456700-0500') == value
