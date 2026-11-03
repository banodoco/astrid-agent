"""Bounded opt-in record of the discovery identity comparison."""

import json
import re
import sys


# Permit only Runtime's Linux birth syntax; never emit arbitrary discovery text.
_BIRTH = re.compile(r"proc-start-ticks:[0-9]{1,20}(?:;boot:[0-9a-fA-F-]{36})?(?:;pidns:[0-9]{1,20})?\Z", re.ASCII)


def _birth(value):
    if type(value) is str and len(value) <= 128 and _BIRTH.fullmatch(value):
        return value
    return None


def _number(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def record(pid, birth, identity, matches, diagnostic):
    """Emit one bounded line; caller isolates all errors from the decision."""
    observed_birth = identity.get("birth_id")
    recorded_safe, observed_safe = _birth(birth), _birth(observed_birth)
    item = {
        "schema": "installer-linux-identity-v1",
        "discovery_pid": _number(pid),
        "discovery_birth": recorded_safe,
        "observed_pid": _number(identity.get("pid")),
        "observed_uid": _number(identity.get("uid")),
        "observed_birth": observed_safe,
        "pid_equal": identity.get("pid") == pid,
        "birth_equal": birth == observed_birth,
        "bridge": diagnostic.get("bridge", "not_called_pid_mismatch"),
        "match": matches,
    }
    sys.stderr.write("installer-linux-identity " + json.dumps(item, sort_keys=True) + "\n")
