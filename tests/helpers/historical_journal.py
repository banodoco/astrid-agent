"""Load pinned historical journal writers from Git, with source-byte receipts.

Only test subprocesses use these modules. Dependency and service boundaries
remain those of the recovery fixture; no historical installer is installed.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from types import ModuleType

AGENT_COMMIT = "710e9c0aae306adf3113cd239b1bb8883093b275"
RUNTIME_COMMIT = "11443b0b5b34bd5b36725504fb083146192a759d"
AGENT_HASH = "dd448a39cc760105a1d97cadd8f0b7d93f2f1c922641cfb759e422ae26ef3ffa"
RUNTIME_HASH = "a918931016baef24f2a896d5b666eb0b2bd1240eb4aa2624777e853015efb818"


def load(repo, commit, path, digest, name):
    source = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{path}"],
                            capture_output=True, check=True, timeout=15).stdout
    assert hashlib.sha256(source).hexdigest() == digest
    module = ModuleType(name)
    module.__file__ = f"{commit}:{path}"
    module.__package__ = name.rpartition(".")[0]
    sys.modules[name] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module
