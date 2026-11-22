"""Generation commands share discovery within a command, never between commands."""
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk.generation import GenerationFacade


def test_generate_reuses_registry_and_refreshes_on_next_command(monkeypatch):
    import astrid.sdk as sdk

    first = ModelRegistry.load_default()
    second = ModelRegistry.load_default()
    available = iter((first, second))
    loads = []

    def load(**kwargs):
        result = next(available)
        loads.append(result)
        return result

    monkeypatch.setattr(sdk, "_load_model_registry", load)
    monkeypatch.setattr(GenerationFacade, "_generate_on_target",
                        lambda self, **kwargs: kwargs["registry"])
    facade = GenerationFacade()
    assert facade.generate("z-image", {"prompt": "one"}, placement="auto", dry_run=True) is first
    assert loads == [first]
    assert facade.generate("z-image", {"prompt": "two"}, placement="auto", dry_run=True) is second
    assert loads == [first, second]


def test_typed_entry_loads_default_registry_once(monkeypatch):
    registry = ModelRegistry.load_default()
    loads = []

    def load(cls, **kwargs):
        loads.append(kwargs)
        return registry

    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(load))
    monkeypatch.setattr(GenerationFacade, "_generate_on_target",
                        lambda self, **kwargs: kwargs["registry"])
    result = GenerationFacade().image(entry="z-image", prompt="one", placement="auto", dry_run=True)
    assert result is registry
    assert len(loads) == 1


def test_cli_entry_reuses_registry_through_facade(monkeypatch, capsys):
    from astrid.core.generation import cli

    registry = ModelRegistry.load_default()
    loads = []

    def load(cls, **kwargs):
        loads.append(kwargs)
        return registry

    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(load))
    monkeypatch.setattr(GenerationFacade, "_generate_on_target",
                        lambda self, **kwargs: {"selected": kwargs["model"]})
    assert cli.main(["image", "hello", "--entry", "z-image", "--placement", "auto", "--dry-run"]) == 0
    assert '"z-image"' in capsys.readouterr().out
    assert len(loads) == 1


def test_prepared_job_resolves_inputs_once_per_command(monkeypatch, tmp_path):
    import pytest
    from astrid.core.generation import entries, preflight
    from astrid.packs.vibecomfy import local_job

    resolved = []
    looked_up = []

    def resolve(name, inputs, **kwargs):
        resolved.append((name, dict(inputs)))
        return {"mode": "t2i"}, dict(inputs)

    def find(name, **kwargs):
        looked_up.append(name)
        return {"id": name, "bundle": "unused.py"}

    class StopBeforeExecution(Exception):
        pass

    monkeypatch.setattr(entries, "resolve_entry_inputs", resolve)
    monkeypatch.setattr(entries, "find_entry", find)
    monkeypatch.setattr(entries, "entry_hooks", lambda entry: {"build": object()})
    monkeypatch.setattr(preflight, "require_generation_target_readiness",
                        lambda *args: (_ for _ in ()).throw(StopBeforeExecution()))
    for value in ("first", "second"):
        with pytest.raises(StopBeforeExecution):
            local_job.run_prepared_job("example", {"prompt": value}, {"python": "unused.py"}, {},
                                       tmp_path, task="task", attempt="attempt", mode="t2i", align="check")
    assert resolved == [("example", {"prompt": "first"}), ("example", {"prompt": "second"})]
    assert looked_up == ["example", "example"]
