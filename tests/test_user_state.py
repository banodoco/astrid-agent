import re
from types import SimpleNamespace

import pytest

from astrid.core import user_state as state
from tests.user_state_fakes import FakeApiError, InMemoryAppState

KEY = "workspace.settings"


@pytest.fixture(autouse=True)
def isolate_store(monkeypatch):
    state.reset_default_store()
    state.set_store_factory(None)

    def unexpected_open():
        pytest.fail("must not open a real Runtime")

    monkeypatch.setattr(state, "_open_default_store", unexpected_open)
    yield
    state.set_store_factory(None)
    state.reset_default_store()


class CountingStore(InMemoryAppState):
    def __init__(self):
        super().__init__()
        self.reads = 0

    def get_user_app_state(self, key):
        self.reads += 1
        return super().get_user_app_state(key)


@pytest.mark.parametrize("key", ["", "-start", "UPPER", "a" * 65, "ok\n"])
def test_bad_key_refused_before_transport(key):
    store = CountingStore()
    with pytest.raises(state.UserStateError) as error:
        state.read(key, store=store)
    assert (error.value.code, error.value.key) == ("invalid_key", key)
    with pytest.raises(state.UserStateError) as error:
        state.write(key, {}, expected_version=0, idempotency_key="once", store=store)
    assert error.value.code == "invalid_key"
    with pytest.raises(state.UserStateError):
        state.update(key, lambda d: d.update(x=1), store=store)
    assert store.reads == 0
    assert store.writes == []


def test_size_boundary_uses_utf8_and_refuses_before_transport():
    store = CountingStore()
    overhead = state.encoded_size({"x": ""})
    content = {"x": "é" * ((state.APP_STATE_MAX_BYTES - overhead) // 2)}
    content["x"] += "a" * (state.APP_STATE_MAX_BYTES - state.encoded_size(content))
    assert state.encoded_size(content) == 16384
    oversized = {"x": content["x"] + "a"}
    assert state.encoded_size(oversized) == 16385
    with pytest.raises(state.UserStateError) as error:
        state.write(KEY, oversized, expected_version=0, idempotency_key="large", store=store)
    assert error.value.code == "too_large"
    assert all(part in str(error.value) for part in (KEY, "16385", "16384"))
    assert store.writes == []
    assert store.reads == 0
    assert state.write(KEY, content, expected_version=0, idempotency_key="limit",
                       store=store) == state.Document(content, 1)


def test_missing_create_update_and_no_op():
    store = InMemoryAppState()
    with state.using(store):
        assert state.read(KEY) == state.Document(None, 0)
        assert state.update(KEY, lambda d: None) == state.Document(None, 0)
        assert store.writes == []
        assert state.update(KEY, lambda d: d.update(items=[1])) == state.Document({"items": [1]}, 1)
        assert state.update(KEY, lambda d: d["items"].append(2)) == state.Document({"items": [1, 2]}, 2)
        assert state.update(KEY, lambda d: None) == state.Document({"items": [1, 2]}, 2)
    assert store.writes == [(KEY, 0), (KEY, 1)]


def test_conflict_reapplies_change_to_fresh_copy():
    class RacingStore(CountingStore):
        def update_user_app_state(self, key, content, *, expected_version, idempotency_key):
            if not self.writes:
                super().update_user_app_state(key, {"other": [1]}, expected_version=0,
                                              idempotency_key="other-writer")
            return super().update_user_app_state(key, content, expected_version=expected_version,
                                                 idempotency_key=idempotency_key)

    store = RacingStore()
    seen = []

    def change(doc):
        seen.append(dict(doc))
        doc["mine"] = True

    assert state.update(KEY, change, store=store) == state.Document({"other": [1], "mine": True}, 2)
    assert seen == [{}, {"other": [1]}]
    assert store.reads == 2
    assert store.writes == [(KEY, 0), (KEY, 0), (KEY, 1)]


def test_persistent_conflict_is_bounded():
    class ConflictingStore(CountingStore):
        def update_user_app_state(self, key, content, *, expected_version, idempotency_key):
            self.writes.append((key, expected_version))
            raise FakeApiError(409, "version_conflict")

    store = ConflictingStore()
    with pytest.raises(state.UserStateError) as error:
        state.update(KEY, lambda d: d.update(x=1), store=store, attempts=4)
    assert (error.value.code, error.value.status, error.value.key) == ("conflict", 409, KEY)
    assert store.reads == 4
    assert store.writes == [(KEY, 0)] * 4


def test_unreachable_read_write_update():
    store = InMemoryAppState()
    store.unreachable = True
    operations = [
        lambda: state.read(KEY, store=store),
        lambda: state.write(KEY, {}, expected_version=0, idempotency_key="once", store=store),
        lambda: state.update(KEY, lambda d: d.update(x=1), store=store),
    ]
    for operation in operations:
        with pytest.raises(state.UserStateError) as error:
            operation()
        assert (error.value.code, error.value.status) == ("unavailable", None)


@pytest.mark.parametrize("code", ["transport_error", "transport_timeout"])
@pytest.mark.parametrize("operation", ["read", "write", "update"])
def test_wrapped_transport_failure_is_unavailable(code, operation):
    class UnavailableStore(InMemoryAppState):
        def get_user_app_state(self, key):
            if operation == "read":
                raise FakeApiError(0, code)
            return super().get_user_app_state(key)

        def update_user_app_state(self, key, content, *, expected_version, idempotency_key):
            raise FakeApiError(0, code)

    store = UnavailableStore()
    operations = {
        "read": lambda: state.read(KEY, store=store),
        "write": lambda: state.write(
            KEY, {}, expected_version=0, idempotency_key="once", store=store),
        "update": lambda: state.update(KEY, lambda doc: doc.update(x=1), store=store),
    }
    with pytest.raises(state.UserStateError) as error:
        operations[operation]()
    assert (error.value.code, error.value.status, error.value.key) == ("unavailable", 0, KEY)


@pytest.mark.parametrize("status,code", [
    (0, "invalid_response"), (401, "unauthorized"), (422, "validation"),
])
def test_non_transport_api_errors_remain_rejected(status, code):
    class RejectingStore(InMemoryAppState):
        def get_user_app_state(self, key):
            raise FakeApiError(status, code)

    with pytest.raises(state.UserStateError) as error:
        state.read(KEY, store=RejectingStore())
    assert (error.value.code, error.value.status, error.value.key) == ("rejected", status, KEY)


def test_default_open_failure_cached_and_reset(monkeypatch):
    opens = []

    def unavailable():
        opens.append(True)
        raise ConnectionError("no Runtime")

    monkeypatch.setattr(state, "_open_default_store", unavailable)
    for _ in range(2):
        with pytest.raises(state.UserStateError) as error:
            state.read(KEY)
        assert error.value.code == "unavailable"
    assert len(opens) == 1
    state.reset_default_store()
    assert state.current_store() is None
    assert len(opens) == 2


def test_factory_none_is_unavailable_and_does_not_fall_back():
    state.set_store_factory(lambda: None)
    with pytest.raises(state.UserStateError) as error:
        state.read(KEY)
    assert error.value.code == "unavailable"


def test_scope_overrides_factory_and_default_opener(monkeypatch):
    factory_store = InMemoryAppState()
    scoped_store = InMemoryAppState()
    explicit_store = InMemoryAppState()
    with state.using(scoped_store):
        assert state.current_store() is scoped_store
        assert state.read(KEY) == state.Document(None, 0)
        state.set_store_factory(lambda: factory_store)
        assert state.current_store() is scoped_store
        state.update(KEY, lambda d: d.update(x=1), store=explicit_store)
        assert scoped_store.writes == []
    assert state.current_store() is factory_store
    assert explicit_store.writes == [(KEY, 0)]


def test_default_success_cached(monkeypatch):
    store = InMemoryAppState()
    opens = []

    def opener():
        opens.append(True)
        return store

    monkeypatch.setattr(state, "_open_default_store", opener)
    assert state.current_store() is store
    assert state.current_store() is store
    assert opens == [True]


def test_validation_normalizes_or_refuses_before_write():
    store = InMemoryAppState()
    result = state.update(KEY, lambda d: d.update(x="  hi "), store=store,
                          validate=lambda d: {"x": d["x"].strip()})
    assert result == state.Document({"x": "hi"}, 1)

    def invalid(doc):
        raise ValueError("not valid")

    with pytest.raises(ValueError, match="not valid"):
        state.update(KEY, lambda d: d.update(x="bad"), validate=invalid, store=store)
    assert store.writes == [(KEY, 0)]


def test_object_resources_and_rejected_status():
    class ObjectStore(InMemoryAppState):
        def get_user_app_state(self, key):
            return SimpleNamespace(**super().get_user_app_state(key))

        def update_user_app_state(self, key, content, *, expected_version, idempotency_key):
            if content.get("reject"):
                raise FakeApiError(422, "validation")
            return SimpleNamespace(**super().update_user_app_state(
                key, content, expected_version=expected_version, idempotency_key=idempotency_key))

    store = ObjectStore()
    assert state.read(KEY, store=store) == state.Document(None, 0)
    assert state.update(KEY, lambda d: d.update(x=1), store=store) == state.Document({"x": 1}, 1)
    with pytest.raises(state.UserStateError) as error:
        state.update(KEY, lambda d: d.update(reject=True), store=store)
    assert (error.value.code, error.value.status) == ("rejected", 422)


def test_fake_idempotency_deep_copies_and_failed_write():
    store = InMemoryAppState()
    original = {"nested": [1]}
    first = store.update_user_app_state(KEY, original, expected_version=0, idempotency_key="first")
    original["nested"].append(2)
    first["content"]["nested"].append(3)
    assert store.get_user_app_state(KEY) == {"content": {"nested": [1]}, "version": 1}
    store.fail_writes.add(2)
    with pytest.raises(ConnectionError):
        store.update_user_app_state(KEY, {}, expected_version=1, idempotency_key="second")
    assert store.get_user_app_state(KEY)["version"] == 1
    store.update_user_app_state(KEY, {"next": True}, expected_version=1, idempotency_key="second")
    assert store.update_user_app_state(KEY, original, expected_version=0,
                                       idempotency_key="first") == {
        "content": {"nested": [1]}, "version": 1,
    }
    assert store.get_user_app_state(KEY) == {"content": {"next": True}, "version": 2}
    assert store.writes == [(KEY, 0), (KEY, 1), (KEY, 1), (KEY, 0)]


@pytest.mark.parametrize("key,content", [("BAD", {}), (KEY, []), (KEY, {"x": "a" * 16384})])
def test_fake_rejects_invalid_write(key, content):
    store = InMemoryAppState()
    with pytest.raises(FakeApiError) as error:
        store.update_user_app_state(key, content, expected_version=0, idempotency_key="bad")
    assert (error.value.status, error.value.code) == (422, "validation")


def test_digest_and_idempotency_key():
    first = {"z": "é", "a": 1}
    reordered = {"a": 1, "z": "é"}
    digest = state.content_digest(first)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    assert digest == state.content_digest(reordered)
    key = state.idempotency_key_for(KEY, 7, first)
    assert key == f"{KEY}.v7.{digest[7:23]}"
    assert key == state.idempotency_key_for(KEY, 7, reordered)
    assert key != state.idempotency_key_for(KEY, 8, first)
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,255}", key)
