"""In-memory Runtime app-state transport for narrow user-state tests."""

import copy
import json
import re


class FakeApiError(RuntimeError):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


class InMemoryAppState:
    def __init__(self):
        self.documents = {}
        self.receipts = {}
        self.unreachable = False
        self.fail_writes = set()
        self.writes = []

    def get_user_app_state(self, key):
        if self.unreachable:
            raise ConnectionError("Runtime unavailable")
        content, version = self.documents.get(key, (None, 0))
        return {"content": copy.deepcopy(content), "version": version}

    def update_user_app_state(self, key, content, *, expected_version, idempotency_key):
        self.writes.append((key, expected_version))
        if self.unreachable or len(self.writes) in self.fail_writes:
            raise ConnectionError("Runtime unavailable")
        if (not isinstance(key, str) or
                re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", key) is None or
                not isinstance(content, dict)):
            raise FakeApiError(422, "validation")
        size = len(json.dumps(content, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False).encode())
        if size > 16 * 1024:
            raise FakeApiError(422, "validation")
        receipt = (key, idempotency_key)
        if receipt in self.receipts:
            return copy.deepcopy(self.receipts[receipt])
        _, version = self.documents.get(key, (None, 0))
        if expected_version != version:
            raise FakeApiError(409, "version_conflict")
        self.documents[key] = (copy.deepcopy(content), version + 1)
        result = {"content": copy.deepcopy(content), "version": version + 1}
        self.receipts[receipt] = copy.deepcopy(result)
        return result
