"""Agent's independent stdlib measurer uses Runtime's vendored golden vectors."""
from __future__ import annotations

import importlib.machinery
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from astrid.core import source_identity


DEFINITION_PATH = Path(source_identity.__file__).with_name("execution") / "host-source-closure-v1.json"
DEFINITION = json.loads(DEFINITION_PATH.read_text(encoding="utf-8"))


def _materialize(tree, vector):
    for relative, text in vector["files"].items():
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    for relative, target in vector["symlinks"].items():
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path, target_is_directory=(path.parent / target).is_dir())


@pytest.mark.parametrize("vector", DEFINITION["vectors"], ids=lambda vector: vector["name"])
def test_host_source_closure_vector(tmp_path, vector):
    tree = tmp_path / "checkout"
    _materialize(tree, vector)
    if "expect_refusal" in vector:
        with pytest.raises(ValueError, match=vector["expect_refusal"]):
            source_identity.source_checkout_closure(tree)
    else:
        assert source_identity.source_checkout_closure(tree) == {
            "schema_version": 1, **vector["expect"],
        }


def test_vendored_definition_matches_runtime_producer_bytes():
    spec = importlib.machinery.PathFinder.find_spec("runtime_protocol")
    assert spec is not None and spec.origin is not None
    producer = Path(spec.origin).parent / "schemas" / DEFINITION_PATH.name
    assert DEFINITION_PATH.read_bytes() == producer.read_bytes()
    assert DEFINITION["schema_version"] == 1
    assert {vector["name"] for vector in DEFINITION["vectors"]} == {
        "source-checkout", "installed-pair", "pair-missing-one",
        "symlinked-component", "symlink-inside-component",
    }


@pytest.mark.parametrize("package", ["runtime_protocol", "astrid_runtime"])
@pytest.mark.parametrize("kind", ["file", "dangling_symlink"])
def test_installed_pair_requires_real_directories(tmp_path, package, kind):
    tree = tmp_path / "checkout"
    vector = next(vector for vector in DEFINITION["vectors"] if vector["name"] == "source-checkout")
    _materialize(tree, vector)
    path = tree / package
    if kind == "file":
        path.write_bytes(b"not a package")
    else:
        os.symlink("absent-package", path, target_is_directory=True)
    with pytest.raises(ValueError):
        source_identity.source_checkout_closure(tree)


@pytest.mark.parametrize("alias", ["root", "ancestor"])
def test_closure_refuses_root_or_component_ancestor_alias(tmp_path, alias):
    tree = tmp_path / "checkout"
    vector = next(vector for vector in DEFINITION["vectors"] if vector["name"] == "source-checkout")
    _materialize(tree, vector)
    if alias == "root":
        link = tmp_path / "alias"
        link.symlink_to(tree, target_is_directory=True)
        tree = link
    else:
        package = tree / "astrid"
        target = tree / "foreign"
        package.rename(target)
        package.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        source_identity.source_checkout_closure(tree)


def test_measurement_is_stdlib_only_before_runtime_import(tmp_path):
    vector = next(vector for vector in DEFINITION["vectors"] if vector["name"] == "installed-pair")
    tree = tmp_path / "checkout"
    _materialize(tree, vector)
    code = """
import importlib.util
import sys
before = set(sys.modules)
spec = importlib.util.spec_from_file_location("agent_source_identity", sys.argv[1])
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)
assert identity.source_checkout_closure(sys.argv[2])["digest"] == sys.argv[3]
assert not {name for name in sys.modules if name.split(".")[0] in {"astrid", "runtime_protocol", "astrid_runtime"}}
assert not {name for name in set(sys.modules) - before if name.split(".")[0] not in sys.stdlib_module_names}
"""
    subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, source_identity.__file__,
         str(tree), vector["expect"]["digest"]],
        cwd=tmp_path, check=True, timeout=30,
    )
