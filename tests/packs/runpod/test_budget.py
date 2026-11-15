"""The latency budget observes attempted operations and all submitted launches."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.packs.runpod._budget import RemoteBudget


def test_shell_process_starts_include_nested_groups_but_not_python_bodies():
    command = '''finish_logs() { ( /opt/astrid/bin/python -c 'print("python3 fake")';
/opt/astrid/bin/python -c 'print("astrid generate")' ); }
python3 -c 'print("python ignored")'
if [ "$rc" -ne 0 ]; then /opt/astrid/bin/python -c 'pass'; fi
exec /opt/astrid/bin/astrid generate test
'''
    assert RemoteBudget.process_starts(command) == 5


def test_budget_counts_all_pod_operations_and_failure_attempts(monkeypatch):
    fake = SimpleNamespace(**{name: lambda *a, **kw: None for name in
                             ("exec", "put", "get", "job_start", "job_poll", "job_cancel")})
    budget = RemoteBudget()
    budget.pod(fake, monkeypatch)
    fake.exec("python3 -c 'pass'")
    fake.job_start(command="astrid generate test\npython -c 'pass'")
    for name in ("put", "get", "job_poll", "job_cancel"):
        getattr(fake, name)("fake")
    budget.assert_within({"exec": 1, "put": 1, "get": 1, "job_start": 1,
                          "job_poll": 1, "job_cancel": 1, "process_start": 3})
    with pytest.raises(AssertionError, match="per-kind counts"):
        budget.assert_within({})


def test_https_fake_and_async_api_share_the_counter(monkeypatch):
    async def api():
        raise OSError("fake failure")

    fake = SimpleNamespace(api=api, **{name: lambda *a: None for name in
                           ("execute_command", "put", "get", "detached_start", "detached_poll", "detached_cancel")})
    budget = RemoteBudget()
    budget.https(fake, monkeypatch)
    budget.wrap(monkeypatch, fake, "api", kind="provider.api")
    fake.execute_command("python -c 'pass'")
    fake.detached_start("astrid generate test")
    for name in ("put", "get", "detached_poll", "detached_cancel"):
        getattr(fake, name)("fake")
    with pytest.raises(OSError, match="fake failure"):
        asyncio.run(fake.api())
    assert budget.counts == {"exec": 1, "put": 1, "get": 1, "job_start": 1,
                             "job_poll": 1, "job_cancel": 1, "process_start": 2, "provider.api": 1}


def test_class_transport_wrapper_accepts_keyword_commands(monkeypatch):
    class Transport:
        def execute_command(self, command):
            return command

    budget = RemoteBudget()
    budget.wrap(monkeypatch, Transport, "execute_command", kind="exec", command=True)
    transport = Transport()
    transport.execute_command(command="python3 -c 'pass'")
    transport.execute_command("astrid generate test")
    assert budget.counts == {"exec": 2, "process_start": 2}
