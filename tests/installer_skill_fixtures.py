"""Real installer/skill composition with only external lifecycle edges controlled."""
import sys
import sysconfig
import venv
from pathlib import Path
from types import SimpleNamespace

import astrid
import runtime_protocol

from astrid.installer.backend import Backend
from astrid.installer.choices import resolve_choices
from astrid.installer.core import Version, switch, write_json
from astrid.installer.service import LABEL, venv_python


def skill_installation(tmp_path, monkeypatch, *, manager=None):
    """Select a disposable interpreter; never install into the host environment."""
    home = Path.home()
    for harness in ("claude", "codex", "hermes"):
        monkeypatch.delenv({"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME",
                           "hermes": "HERMES_HOME"}[harness], raising=False)
        (home / f".{harness}").mkdir(exist_ok=True)
    root, data = tmp_path / "app/astrid", tmp_path / "support"
    realm = tmp_path / "workspace"
    realm.mkdir()
    sentinel = realm / "keep.txt"
    sentinel.write_bytes(b"workspace bytes\r\n")
    data.mkdir()
    (data / "keep.txt").write_bytes(b"support bytes\n")
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv", managed_by=manager)
    backend.uv.touch()
    backend.service.choices = resolve_choices(saved={"skill_copies": True}, existing_install=True)
    versions = {}

    def version(name, release):
        folder = root / "versions" / name
        environment = folder / "python"
        venv.EnvBuilder(with_pip=False).create(environment)
        python = venv_python(environment)
        site = (environment / "Lib/site-packages" if sys.platform == "win32" else
                next((environment / "lib").glob("python*/site-packages")))
        # -I remains enabled: explicit fixture paths live in this environment.
        paths = [Path(astrid.__file__).resolve().parent.parent,
                 Path(runtime_protocol.__file__).resolve().parent.parent,
                 Path(sysconfig.get_path("purelib")), Path(sysconfig.get_path("platlib"))]
        (site / "composition.pth").write_text("\n".join(map(str, dict.fromkeys(paths))) + "\n")
        profile = folder / "profile.json"
        profile.write_text("{}\n")
        value = Version(folder, python, profile, {}, release=release, managed_by=manager)
        write_json(folder / "version.json", value.document())
        versions[name] = value
        return value

    old = version("old", 11)
    switch(root, old)
    record = {"owner": LABEL, "program_root": str(root), "data_root": str(data),
              "realm_id": "realm", "realm_root": str(realm), "chat_settings": {},
              "identity": old.document()["identity"], "choices": dict(backend.service.choices)}
    if manager:
        record["managed_by"] = manager
    write_json(root / "install.json", {key: record[key] for key in
               ("owner", "data_root", "managed_by") if key in record})
    write_json(backend.service.selection, record)
    # Workspace identity is Runtime's read-only process edge; skills and file
    # removal execute through their real production implementations.
    monkeypatch.setattr(backend, "workspace", lambda *_args: {
        "realm_id": "realm", "realm_root": str(realm)})
    def inspect_workspace(_version, _data, *args, **_kwargs):
        assert args[:2] == ("workspace", "inspect")
        return {"realm_id": "realm", "realm_root": str(realm)}

    monkeypatch.setattr(backend, "runtime", inspect_workspace)
    seeded = backend.sync_skills(old)
    assert seeded["ok"], backend.log.read_text()
    targets = {name: home / f".{name}/skills/astrid" for name in ("claude", "codex", "hermes")}
    assert all(target.is_dir() and not target.is_symlink() for target in targets.values())
    return SimpleNamespace(**locals())


def edit_and_foreign(env):
    """Retain one edited marked copy and one retargeted, previously owned link."""
    edited = env.targets["claude"] / "SKILL.md"
    edited.write_bytes(edited.read_bytes() + b"\nuser edit\r\n")
    foreign_root = env.tmp_path / "foreign/projects/astrid/notes"
    foreign_root.mkdir(parents=True)
    foreign_file = foreign_root / "keep.txt"
    foreign_file.write_bytes(b"foreign bytes\n")
    foreign = env.targets["hermes"]
    import shutil
    shutil.rmtree(foreign)
    foreign.symlink_to(foreign_root, target_is_directory=True)
    return edited, edited.read_bytes(), foreign, foreign_file
