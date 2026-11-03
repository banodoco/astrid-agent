from __future__ import annotations

import pytest

from astrid.pack_validation import validate_pack

PIN = {"commit": "a" * 40, "digest": "b" * 64}
ENV = {"implementation_name": "cpython", "implementation_version": "3.13.0", "os_name": "posix", "platform_machine": "x86_64", "platform_release": "test", "platform_system": "Linux", "platform_version": "test", "python_full_version": "3.13.0", "platform_python_implementation": "CPython", "python_version": "3.13", "sys_platform": "linux"}


def check(pack_id="demo", root=None, api="1.0", requirements=(), packages=None, pins=None, **policy):
    manifest = {"id": pack_id, "schema_version": 3, "pack_api": api,
                "dependencies": {"packs": [], "python_requirements": list(requirements)}}
    candidate = {"id": pack_id, "canonical_root": root or f"/packs/{pack_id}",
                 "commit": PIN["commit"], "installed_digest": PIN["digest"]}
    return validate_pack(mode="inventory", manifest=manifest, candidate=candidate, selected={},
                         core_api=(1, 0), release_pins=pins or {}, python_packages=packages or {},
                         import_violations=[], **policy)


def test_personal_requires_the_configured_canonical_identity():
    assert check("personal", root="/home/a/Astrid/packs/personal", canonical_personal_root="/home/a/Astrid/packs/personal")["ok"]
    assert check("personal", root="/elsewhere/personal", canonical_personal_root="/home/a/Astrid/packs/personal")["code"] == "reserved_id"
    assert check("personal")["code"] == "reserved_id"


@pytest.mark.parametrize("pack_id", ["h3_av", "runpod"])
def test_internal_requires_named_policy_and_complete_matching_pin(pack_id):
    policy = {"internal_pack_ids": frozenset({"h3_av", "runpod"})}
    assert check(pack_id, api="internal", pins={pack_id: PIN}, **policy)["ok"]
    assert not check(pack_id, api="internal", pins={pack_id: {**PIN, "commit": "c" * 40}}, **policy)["ok"]
    assert not check(pack_id, api="internal", pins={pack_id: {**PIN, "digest": "c" * 64}}, **policy)["ok"]
    assert not check(pack_id, api="internal", pins={pack_id: {"commit": PIN["commit"]}}, **policy)["ok"]
    assert not check(pack_id, api="internal", pins={pack_id: PIN})["ok"]


def test_internal_does_not_extend_to_another_pinned_pack():
    result = check("demo", api="internal", pins={"demo": PIN}, internal_pack_ids=frozenset({"h3_av", "runpod"}))
    assert not result["ok"]


@pytest.mark.parametrize(("requirement", "packages", "ok"), [
    ("Foo_Bar>=1,<2", {"foo-bar": "1.5"}, True),
    ("foo>=9", {"foo": "1"}, False),
    ("foo>=9; python_version < '3.0'", {}, True),
    ("foo>=9; python_version >= '3.0'", {"foo": "1"}, False),
    ("foo => 1", {"foo": "1"}, False),
    ("foo; python_version ??? '3'", {"foo": "1"}, False),
    ("foo @ https://example.invalid/foo.whl", {"foo": "1"}, False),
])
def test_pep508_preflight(requirement, packages, ok):
    result = check(requirements=[requirement], packages=packages, python_environment=ENV)
    assert result["ok"] is ok
    if not ok:
        assert requirement in result["reason"]


def test_marker_needs_explicit_interpreter_snapshot():
    result = check(requirements=["foo; python_version > '3'"])
    assert not result["ok"] and "python_version" in result["reason"]


def test_canonical_personal_still_checks_dependencies_and_import_results():
    manifest = {"id": "personal", "schema_version": 3, "pack_api": "1.0", "dependencies": {"packs": ["missing"]}}
    result = validate_pack(mode="inventory", manifest=manifest,
                           candidate={"id": "personal", "canonical_root": "/home/a/personal"},
                           selected={}, core_api=(1, 0), release_pins={}, python_packages={},
                           import_violations=[], canonical_personal_root="/home/a/personal")
    assert result["code"] == "missing_dep"
