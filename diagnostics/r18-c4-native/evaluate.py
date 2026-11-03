#!/usr/bin/env python3
"""Non-root diagnostic checks; unknowns always refuse, never emit authority."""
import hashlib
import json
import os
from pathlib import Path
import sys

root = Path(os.environ['C4_ROOT'])
assert os.getuid() != 0 and os.geteuid() != 0
before = json.loads((root/'results/before.json').read_text())
after = json.loads((root/'results/after.json').read_text())

def properties(observation):
    if observation.get('exit') != 0:
        return {}
    return dict(line.split('=', 1) for line in observation.get('stdout', '').splitlines() if '=' in line)

def command_ok(obj):
    return obj.get('exit') == 0

def exists_named(result, name):
    return any(x.get('name') == name for x in result.get('entries', []))

pre = properties(before['stopped_before_suppression'])
supp = properties(before['suppressed_before_restart'])
blocked_pre = properties(before['after_blocked_start'])
post = properties(after['post_manager_restart'])
blocked_post = properties(after['after_blocked_start'])
invoked = properties(before['invoked'])
boot_pre, boot_post = before['boot_id']['value'], after['boot_id']['value']
content = after['dropin'].get('value')
retained_hash = hashlib.sha256(content.encode()).hexdigest() if content is not None else None
locks = after['locks']
routes = after['routes']
cron, at, xdg = routes['cron_control'], routes['at_control'], routes['xdg_control']
route_dirs = {Path(d['path']).name: d for d in routes['dirs'] if '/route-controls/' in d['path']}
checks = {
    'boot_id': {'pre': boot_pre, 'post': boot_post, 'same_after_restart': boot_pre is not None and boot_pre == boot_post,
                'changed_reboot_provenance': 'unknown', 'reason': 'same-boot manager restart and fixture snapshots cannot authenticate an earlier boot'},
    'invocation': {'positive_invocation_id': invoked.get('InvocationID'), 'positive_start_monotonic': invoked.get('ExecMainStartTimestampMonotonic'),
                   'post_restart_invocation_id': post.get('InvocationID'), 'post_restart_start_monotonic': post.get('ExecMainStartTimestampMonotonic'),
                   'journal_exit': after['journal_this_boot'].get('exit'), 'absence_this_boot': 'unknown',
                   'reason': 'a manager reset or empty/inaccessible journal does not establish complete per-boot invocation history'},
    'locks': {'held_blocked': isinstance(locks['held'], dict) and locks['held'].get('result') == 'blocked', 'free_acquired': locks['free'] == 'acquired',
              'limit': locks['scope']},
    'suppression': {'stopped_before_install': pre.get('ActiveState') == 'inactive' and pre.get('SubState') == 'dead',
                    'pre_restart_effective': supp.get('Restart') == 'no' and '90-c4-fixture.conf' in supp.get('DropInPaths', ''),
                    'pre_restart_start_condition_failed': command_ok(before['blocked_start']) and blocked_pre.get('ConditionResult') == 'no' and not before['marker_after_blocked_start'],
                    'same_dropin_after_restart': retained_hash == before['dropin']['sha256'],
                    'guard_retained': after['guard'].get('value') == 'fixture active\n',
                    'post_restart_effective': post.get('Restart') == 'no' and '90-c4-fixture.conf' in post.get('DropInPaths', ''),
                    'post_restart_start_condition_failed': command_ok(after['blocked_start']) and blocked_post.get('ConditionResult') == 'no' and not after['marker_after_blocked_start'],
                    'limit': 'fixture service and sampled manager readback; no atomic install or independent authority established'},
    'routes': {'cron_visible_control': 'C4-DISPOSABLE' in cron.get('after', {}).get('stdout', ''),
               'cron_install_exit': cron.get('install', {}).get('exit'), 'cron_remove_exit': cron.get('remove', {}).get('exit'),
               'at_visible_control': bool(at.get('inspect', {}).get('exit') == 0 and 'C4-DISPOSABLE' in at.get('inspect', {}).get('stdout', '')),
               'at_install_exit': at.get('install', {}).get('exit'), 'at_remove_exit': at.get('remove', {}).get('exit'),
               'xdg_visible_control': exists_named(xdg['after'], 'c4-disposable.desktop'),
               'xdg_removed': not exists_named(xdg['final'], 'c4-disposable.desktop'),
               'system_unit_show': routes['commands'][-1],
               'inaccessible_is_unknown': route_dirs.get('inaccessible', {}).get('disposition') == 'unknown',
               'foreign_is_unresolved': any(e.get('disposition') == 'foreign_or_unresolved' for e in route_dirs.get('foreign', {}).get('entries', [])),
               'clean_complete_audit': 'unknown', 'reason': routes['scope']},
}
result = {'uid': os.getuid(), 'euid': os.geteuid(), 'checks': checks, 'admission': 'refuse', 'authority': False,
          'note': 'Diagnostic checks, not a product consumer or protected implementation proof.'}
(root/'results/evaluation.json').write_text(json.dumps(result, sort_keys=True, indent=2)+'\n')
sys.exit(1)
