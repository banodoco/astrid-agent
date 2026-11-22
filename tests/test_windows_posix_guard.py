"""Guard the installed controller/attached generation import boundary on Linux.

The manifest is a conservative local import closure, including lazy imports:
modules belonging to remote operations must still import on the controller.
Native Windows handles are qualified by windows-install-guard, not this test.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
POSIX = frozenset('''O_DIRECTORY O_NOFOLLOW O_CLOEXEC O_NONBLOCK O_NOCTTY O_SYNC O_DSYNC
O_ASYNC O_DIRECT O_LARGEFILE O_PATH O_TMPFILE fork forkpty getuid geteuid getgid getegid
getgroups getpgid getpgrp setsid setpgid killpg mkfifo mknod chown lchown fchmod fchown
chroot setuid seteuid setgid setegid setgroups sysconf confstr uname WNOHANG WUNTRACED
WCONTINUED wait waitpid waitid posix_spawn posix_spawnp sched_getaffinity sched_setaffinity
statvfs fstatvfs major minor makedev getloadavg lockf fadvise posix_fadvise
'''.split())
UNIX_MODULES = frozenset('fcntl pwd grp resource termios tty pty'.split())
# No file exemptions: even target-only helpers must be safe to import.
IMPORT_ALLOWLIST = {}


def import_time_findings(source):
    tree = ast.parse(source)
    aliases = {n.asname or n.name for n in ast.walk(tree) if isinstance(n, ast.Import)
               for n in n.names if n.name == 'os'}
    # Runtime's adapter defines these flags on Windows too; it is not stdlib os.
    adapter = any(isinstance(n, ast.ImportFrom) and n.module == 'runtime_protocol.dirfd'
           and any(a.name == 'filesystem' and a.asname == 'os' for a in n.names) for n in ast.walk(tree))
    if adapter:
        aliases.add('os')
    findings = []

    def windows_truth(node):
        if isinstance(node, ast.BoolOp):
            values = [windows_truth(v) for v in node.values]
            if isinstance(node.op, ast.And):
                return False if False in values else True if all(v is True for v in values) else None
            return True if True in values else False if all(v is False for v in values) else None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            value = windows_truth(node.operand)
            return None if value is None else not value
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left = ast.unparse(node.left)
            actual = {'os.name': 'nt', 'sys.platform': 'win32'}.get(left)
            right = node.comparators[0]
            if actual is not None and isinstance(right, ast.Constant):
                if isinstance(node.ops[0], ast.Eq): return actual == right.value
                if isinstance(node.ops[0], ast.NotEq): return actual != right.value
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'hasattr' and len(node.args) == 2
                and isinstance(node.args[0], ast.Name) and node.args[0].id in aliases
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in POSIX):
            return False
        return None

    class Visitor(ast.NodeVisitor):
        optional_import = False
        def visit_FunctionDef(self, node):
            for item in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
                if item is not None:
                    self.visit(item)
            # Annotations are evaluated unless postponed.
            if not any(isinstance(n, ast.ImportFrom) and n.module == '__future__'
                       and any(a.name == 'annotations' for a in n.names) for n in tree.body):
                for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                    if arg.annotation:
                        self.visit(arg.annotation)
                if node.returns:
                    self.visit(node.returns)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node):
            for item in [*node.args.defaults, *node.args.kw_defaults]:
                if item is not None:
                    self.visit(item)

        def visit_If(self, node):
            value = windows_truth(node.test)
            self.visit(node.test)
            for item in node.body if value is True else node.orelse if value is False else [*node.body, *node.orelse]:
                self.visit(item)

        def visit_Try(self, node):
            # Optional POSIX imports with an ImportError fallback are portable.
            catches = any(h.type and any(isinstance(n, ast.Name) and n.id in {'ImportError', 'ModuleNotFoundError'}
                                        for n in ast.walk(h.type)) for h in node.handlers)
            previous = self.optional_import
            self.optional_import |= catches
            for item in node.body:
                self.visit(item)
            self.optional_import = previous
            for item in [*node.handlers, *node.orelse, *node.finalbody]:
                self.visit(item)

        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name) and node.value.id in aliases and node.attr in POSIX and not (adapter and node.value.id == 'os' and node.attr in {'O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK'}):
                findings.append((node.lineno, 'os.' + node.attr))
            self.generic_visit(node)

        def visit_Import(self, node):
            for alias in node.names:
                if alias.name.split('.')[0] in UNIX_MODULES and not self.optional_import:
                    findings.append((node.lineno, 'import ' + alias.name))

        def visit_ImportFrom(self, node):
            if node.module in UNIX_MODULES and not self.optional_import:
                findings.append((node.lineno, 'from ' + node.module))
            if node.module == 'os':
                for alias in node.names:
                    if alias.name in POSIX:
                        findings.append((node.lineno, 'from os import ' + alias.name))

    Visitor().visit(tree)
    return findings


def test_no_posix_import_time_operations():
    problems = []
    for path in sorted((ROOT / 'astrid').rglob('*.py')):
        relative = path.relative_to(ROOT).as_posix()
        if relative not in IMPORT_ALLOWLIST:
            problems.extend(f'{relative}:{line}: {name}' for line, name in
                            import_time_findings(path.read_text(encoding='utf-8')))
    assert not problems, '\n'.join(problems)


@pytest.mark.parametrize('source', [
    'import os\nx = os.O_DIRECTORY',
    'import os as native\nclass C:\n x = native.getuid()',
    'import os\ndef f(x=os.O_NOFOLLOW): pass',
    'import fcntl', 'from pwd import getpwuid', 'from os import fork as spawn',
])
def test_static_guard_detects_executed_uses(source):
    assert import_time_findings(source)


def reachable_modules():
    """Overapproximate static imports from entrypoints, including lazy branches."""
    files = {}
    for path in (ROOT / 'astrid').rglob('*.py'):
        name = '.'.join(path.relative_to(ROOT).with_suffix('').parts)
        if name.endswith('.__init__'):
            name = name[:-9]
        files[name] = path
    seeds = ['astrid.__main__', 'astrid.installer.cli', 'astrid.runtime_cli',
             'astrid.core.generation.cli', 'astrid.core.execution.generic_host',
             'astrid.core.execution._child_bridge', 'astrid.packs.runpod.engine_snapshot']
    found, pending = set(), list(seeds)
    while pending:
        name = pending.pop()
        # The selected loopback placement never loads the rented-job adapter.
        if name.startswith('astrid.packs.runpod.') and name != 'astrid.packs.runpod.engine_snapshot':
            continue
        if name in found or name not in files:
            continue
        found.add(name)
        path = files[name]
        package = name if path.name == '__init__.py' else name.rpartition('.')[0]
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            imports = []
            if isinstance(node, ast.Import):
                imports = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = importlib.util.resolve_name('.' * node.level + (node.module or ''), package) if node.level else node.module
                imports = [base, *(base + '.' + a.name for a in node.names)] if base else []
            for target in imports:
                pending.extend('.'.join(target.split('.')[:i]) for i in range(1, len(target.split('.')) + 1))
    # Vendored truststore selects one native TLS backend; Darwin is never a
    # Windows import, and loading WinDLL itself needs the real Windows CI lane.
    found.discard('astrid.installer._tls_truststore._macos')
    if sys.platform != 'win32':
        found.discard('astrid.installer._tls_truststore._windows')
    return sorted(found)


def test_windows_reachable_imports_and_parsers():
    script = r'''
import importlib, importlib.abc, json, os, sys
# Initialize host stdlib dependencies before removing POSIX capabilities. Do not
# lie to pathlib/ctypes/subprocess about the kernel executing this simulation.
import asyncio, ctypes, multiprocessing, platform, shutil, ssl, subprocess, tempfile
import numpy  # NumPy initialization consults host os.uname(); load it before simulation.
import runtime_protocol.dirfd as dirfd
# Use the real Windows adapter's import surface; no Win32 call occurs at import.
dirfd.filesystem = dirfd._WindowsFilesystem()
modules, attributes, blocked = json.loads(sys.argv[1])
for name in attributes:
    if hasattr(os, name): delattr(os, name)
shutil._use_fd_functions = False  # Windows selects shutil's path implementation.
os.supports_dir_fd = set()
os.supports_fd = set()
os.supports_follow_symlinks = set()
class BlockUnix(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in blocked:
            raise ModuleNotFoundError('Windows has no ' + fullname, name=fullname)
for name in blocked: sys.modules.pop(name, None)
sys.meta_path.insert(0, BlockUnix())
for name in modules:
    importlib.import_module(name)
from astrid.core.generation.cli import _parser
parser = _parser()
for args in [['scan', '--json'], ['settings', '--help'], ['defaults', '--json'],
             ['fetch-models', 'z-image', 't2i', '--comfy-url', 'http://127.0.0.1:8188', '--json'],
             ['fetch-models', 'z-image', 't2i', '--comfy-url', 'http://127.0.0.1:8188', '--apply', 'a'*64],
             *[[kind, '--placement', 'http://127.0.0.1:8188', '--help'] for kind in ['image', 'audio', 'video']]]:
    try: parser.parse_args(args)
    except SystemExit as e: assert e.code == 0
from astrid.installer.cli import parser as install_parser
install_parser()
from astrid.core.gateway.dispatch import _build_dispatch_parser, _dispatch_doctor, _dispatch_product
_build_dispatch_parser()
for function, args in [(_dispatch_doctor, ['--help']),
                       (_dispatch_product, ['tasks', 'show', '--help']),
                       (_dispatch_product, ['tasks', 'cancel', '--help'])]:
    try: function(args)
    except SystemExit as e: assert e.code == 0
'''
    result = subprocess.run([sys.executable, '-c', script,
                             json.dumps([reachable_modules(), sorted(POSIX), sorted(UNIX_MODULES)])],
                            cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr

