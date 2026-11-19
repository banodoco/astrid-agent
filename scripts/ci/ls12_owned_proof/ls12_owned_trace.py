"""Read-only parent-process work tracing; not timing qualification.

Chained by the unchanged LS-2 ProcessObserver. No product method is patched.
Record successful Path.read_bytes returns, file_digest readinto calls and
observed stream-position advancement, not physical storage-device reads.
"""
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest


SITES = {
    "astrid.core.pack.identity:pack_digest",
    "astrid.core.pack.identity:_read_pack_tree",
    "astrid.core.pack.imports:validate_selected_pack_map",
    "astrid.core.pack.imports:_validate_selected_pack_map",
    "astrid.core.pack.imports:install_pack_imports",
    "astrid.core.pack.discovery:_tree_fingerprint",
    "astrid.core.pack.discovery:_discovery_fingerprint",
    "astrid.core.execution.generic_host:_verify_admitted_source",
    "astrid.core.execution.generic_host:_verify_action_admission",
    "astrid.core.execution.generic_host:_source_digest_for_roots",
    "astrid.core.execution.generic_host:GenericPackHost._selected_pack_map",
    "astrid.sdk.generation:_snapshot_generation_workflow",
    "astrid.sdk.generation:_check_workflow_source_admission",
    "astrid.core.generation.entries:_load_entry_workflow",
    "astrid.packs.vibecomfy.production_engine:load_workflow_path",
    "vibecomfy.workflow_bundle:load_bundle",
    "vibecomfy.scratchpad_loader:load_scratchpad",
    "vibecomfy.workflow_bundle:_atomic_publish_pair",
    "vibecomfy.workflow_bundle:_atomic_publish_pair_unlocked",
    "vibecomfy.porting.object_info.generation:cache_file_witness",
}
NAMES = {site.rsplit(':', 1)[-1].rsplit('.', 1)[-1] for site in SITES}


def site(frame):
    return str(frame.f_globals.get('__name__', '')) + ':' + frame.f_code.co_qualname


def position(stream):
    try:
        return stream.tell()
    except (AttributeError, OSError, ValueError):
        return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    spans, reads, active, file_digests, fd_active = [], [], {}, [], {}
    previous, previous_thread = sys.getprofile(), threading.getprofile()

    observing = True
    origins = {}

    def observe(frame, event, arg):
        if not observing:
            return
        code = frame.f_code
        module = frame.f_globals.get('__name__', '')
        name = code.co_name
        if name == 'file_digest' and module == 'hashlib':
            key = (threading.get_native_id(), id(frame))
            if event == 'call':
                stream = frame.f_locals.get('fileobj')
                row = {'tid': key[0], 'start_ns': time.perf_counter_ns(), 'end_ns': None,
                       'cpu_start_ns': time.thread_time_ns(), 'cpu_end_ns': None,
                       'path': str(getattr(stream, 'name', '')), 'start_position': position(stream),
                       'end_position': None, 'readinto_calls': 0, 'successful': False}
                cursor = frame.f_back
                row['parents'] = []
                while cursor is not None and len(row['parents']) < 10:
                    row['parents'].append(site(cursor))
                    cursor = cursor.f_back
                fd_active[key] = row
                file_digests.append(row)
            elif event == 'c_call' and getattr(arg, '__name__', '') == 'readinto':
                row = fd_active.get(key)
                if row is not None:
                    row['readinto_calls'] += 1
            elif event == 'return':
                row = fd_active.pop(key, None)
                if row is not None:
                    row.update(end_ns=time.perf_counter_ns(), cpu_end_ns=time.thread_time_ns(),
                               end_position=position(frame.f_locals.get('fileobj')), successful=arg is not None)
            return
        if event not in {'call', 'return'}:
            return
        if name == 'read_bytes' and module == 'pathlib':
            if event == 'return' and isinstance(arg, bytes):
                parents, cursor = [], frame.f_back
                while cursor is not None and len(parents) < 12:
                    parents.append(site(cursor))
                    cursor = cursor.f_back
                reads.append({'at_ns': time.perf_counter_ns(), 'tid': threading.get_native_id(),
                              'path': str(frame.f_locals.get('self')), 'bytes': len(arg), 'parents': parents})
            return
        if name not in NAMES:
            return
        name_site = site(frame)
        if name_site not in SITES:
            return
        origins.setdefault(name_site, set()).add(frame.f_code.co_filename)
        key = (threading.get_native_id(), id(frame))
        if event == 'call':
            parents, cursor = [], frame.f_back
            while cursor is not None and len(parents) < 10:
                parents.append(site(cursor))
                cursor = cursor.f_back
            args = {key: str(value) for key, value in frame.f_locals.items()
                    if key in {'root', 'path', 'pack_root', 'source', 'filename', 'retain_sources'}
                    and isinstance(value, (str, Path, bool))}
            row = {'site': name_site, 'tid': key[0], 'start_ns': time.perf_counter_ns(),
                   'end_ns': None, 'cpu_start_ns': time.thread_time_ns(), 'cpu_end_ns': None,
                   'parents': parents, 'arguments': args}
            active[key] = row
            spans.append(row)
        else:
            row = active.pop(key, None)
            if row is not None:
                row.update(end_ns=time.perf_counter_ns(), cpu_end_ns=time.thread_time_ns())
                if name in {'pack_digest', '_tree_fingerprint'} and isinstance(arg, str):
                    row['result_digest'] = arg
                if name == '_read_pack_tree' and arg is not None:
                    row.update(result_digest=arg.digest, retained_python_files=len(arg.sources),
                               retained_python_bytes=sum(map(len, arg.sources.values())))

    sys.setprofile(observe)
    threading.setprofile(observe)
    try:
        yield
    finally:
        # Threads already running may retain this callback until they exit.
        observing = False
        sys.setprofile(previous)
        threading.setprofile(previous_thread)
        # Snapshot already imported modules only, after the measured pytest call. No imports are induced.
        dependencies = {}
        for name, module in sys.modules.copy().items():
            if name.split('.', 1)[0] in {'astrid_runtime', 'runtime_protocol', 'astrid_runtime_client', 'runpod_lifecycle'}:
                file = vars(module).get('__file__') if module is not None else None
                if isinstance(file, str):
                    dependencies[name] = file
        profile_state = {'prior_thread_hook': previous_thread is not None, 'prior_current_hook': previous is not None,
                         'thread_hook_restored': threading.getprofile() is previous_thread,
                         'current_hook_restored': sys.getprofile() is previous}
        Path(os.environ['LS12_OWNED_TRACE']).write_text(json.dumps({
            'schema': 'r14.ls12.owned-work-trace.v1', 'test_id': item.nodeid,
            'diagnostic_only': True, 'scope': 'parent SDK/host process and Python threads only',
            'spans': spans, 'reads': reads, 'file_digests': file_digests,
            'origins': {key: sorted(paths) for key, paths in origins.items()},
            'loaded_dependencies': dependencies, 'profile_state': profile_state,
        }, separators=(',', ':')) + '\n')
