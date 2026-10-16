"""Pack V3 timeline Check discovery and invocation use the canonical admission route."""
from __future__ import annotations

import pytest
import yaml

from astrid.core.pack.canonical import CanonicalPackValidationError, validate_canonical_pack
from astrid.core.pack.checks import CheckHostContext, UnsupportedCheck, invoke_check, resolve_check
from astrid.packs.rendering.actions.timeline_visualize.layers.base import CheckContext
from astrid.sdk.timeline.commands import CommandError

IDENTITY = "timeline.check/demo.damaged-art"


@pytest.fixture
def check_pack(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    packs = tmp_path / "packs"
    root = packs / "demo"
    descriptor_path = "extensions/timeline.check/damaged_art.yaml"
    implementation_path = "extensions/timeline.check/damaged_art.py"
    descriptor = {
        "point": "timeline.check",
        "id": "demo.damaged-art",
        "label": "Damaged pixel art",
        "description": "Report selected sprite measurements.",
        "scope": "timeline",
        "reads": ["timeline.document", "timeline.selected_assets"],
        "finding": {"code": "DAMAGED-ART", "severity": "warn", "location": "message"},
        "thresholds": {"damaged_art_below": 0.75},
        "implementation": {"path": implementation_path, "function": "run"},
    }
    descriptor_file = root / descriptor_path
    implementation_file = root / implementation_path
    descriptor_file.parent.mkdir(parents=True, exist_ok=True)
    implementation_file.parent.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "implementation-ran"
    marker.unlink(missing_ok=True)
    implementation_file.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran')\n"
        "def run(ctx):\n"
        "    ctx.document\n"
        "    return [f\"{row['address']}: {row['asset']} {row['outline_integrity']:.2f}\" "
        "for row in ctx.selected_assets()]\n",
        encoding="utf-8",
    )
    descriptor_file.write_text(yaml.safe_dump(descriptor), encoding="utf-8")
    (root / "pack.yaml").write_text(yaml.safe_dump({
        "schema_version": 3,
        "id": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "extensions": {"timeline": {"checks": [descriptor_path]}},
        "resources": [
            {"kind": "support", "path": descriptor_path},
            {"kind": "implementation", "path": implementation_path},
        ],
    }), encoding="utf-8")
    kwargs = {"project_root": tmp_path / "empty-project", "extra_pack_roots": (str(packs),)}
    return resolve_check(IDENTITY, **kwargs), kwargs, marker


def test_discovery_reads_metadata_without_running_contribution(check_pack):
    check, _kwargs, marker = check_pack
    assert check.identity == IDENTITY
    assert check.eligible and check.source_kind == "extra"
    assert check.descriptor["scope"] == "timeline"
    assert check.descriptor["reads"] == ["timeline.document", "timeline.selected_assets"]
    assert check.finding == {"code": "DAMAGED-ART", "severity": "warn", "location": "message"}
    assert not marker.exists()


def test_existing_check_runner_adapts_descriptor_metadata_once(check_pack):
    check, _kwargs, marker = check_pack
    from astrid.core.pack.checks import registered_checks

    adapted = next(
        item for item in registered_checks(
            project_root=check.pack_root.parent.parent,
            extra_pack_roots=(str(check.pack_root.parent),),
        )
        if item.name == "demo-damaged-art"
    )
    host = CheckHostContext("sdk", {"timeline_id": "t"},
                            lambda: [{"address": "c1.rocket", "asset": "R", "outline_integrity": 0.70}])
    context = CheckContext(cut=None, cuts=[], fps=30, elements=[], all_elements=[], words=[], beats={}, sfx=[],
                           tracks=[], params={}, extension_context=host)
    findings = adapted.run(context)
    assert [(finding.code, finding.severity, finding.message) for finding in findings] == [
        ("DAMAGED-ART", "warn", "c1.rocket: R 0.70")
    ]
    assert findings[0].line() == "DAMAGED-ART c1.rocket: R 0.70"
    assert marker.exists()


@pytest.mark.parametrize("host_context,host", [
    (None, "sdk"),
    (CheckHostContext("app", {}, lambda: []), "sdk"),
    (CheckHostContext("sdk", {}, None), "sdk"),
    (CheckHostContext("sdk", {}, lambda: []), "app"),
])
def test_unavailable_check_host_refuses_before_contribution_execution(check_pack, host_context, host):
    check, _kwargs, marker = check_pack
    with pytest.raises(UnsupportedCheck, match="unsupported|unavailable"):
        invoke_check(check, host_context, host=host)
    assert not marker.exists()


def test_unknown_identity_and_changed_resource_are_refused(check_pack):
    check, kwargs, marker = check_pack
    for identity in ("damaged-art", "timeline.view/demo.damaged-art", "timeline.check/missing.check"):
        with pytest.raises(UnsupportedCheck, match="unsupported"):
            resolve_check(identity, **kwargs)
    implementation = check.pack_root / check.descriptor["implementation"]["path"]
    implementation.write_text("def run(ctx):\n    return ['changed']\n", encoding="utf-8")
    with pytest.raises(CommandError, match="implementation changed"):
        invoke_check(check, CheckHostContext("sdk", {}, lambda: []))
    assert not marker.exists()


def test_checkout_reports_rediscover_changed_check_implementations(check_pack, monkeypatch):
    import json
    from pathlib import Path

    from astrid.core.pack import checks as declarations
    from astrid.sdk.timeline import check as sdk_check
    from astrid.sdk.timeline.checkout import Checkout

    check, kwargs, _marker = check_pack
    discover = declarations.discover_checks
    monkeypatch.setattr(declarations, "discover_checks", lambda **_ignored: discover(**kwargs))
    monkeypatch.setattr(sdk_check, "timeline_check_host_context", lambda checkout: CheckHostContext(
        "sdk", checkout.bundle,
        lambda: [{"address": "c1.sprite", "asset": "R", "outline_integrity": 0.70}],
    ))
    fixture = Path(__file__).parents[2] / "fixtures/timeline_editing/tiny_publishable.json"
    checkout = Checkout(json.loads(fixture.read_text()))
    checkout._edit_local_only = True
    implementation = check.pack_root / check.descriptor["implementation"]["path"]
    implementation.write_text("def run(ctx):\n    return ['first implementation']\n")
    first = checkout.check()
    implementation.write_text("def run(ctx):\n    return ['second implementation']\n")
    second = checkout.check()
    assert any("first implementation" in line for line in first.lint)
    assert any("second implementation" in line for line in second.lint)
    assert not any("first implementation" in line for line in second.lint)


def test_descriptor_and_implementation_must_be_admitted_resources(check_pack):
    check, _kwargs, _marker = check_pack
    manifest = check.pack_root / "pack.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["resources"] = [row for row in data["resources"] if row["path"] != check.descriptor_path]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(CanonicalPackValidationError, match="declared file resource"):
        validate_canonical_pack(check.pack_root)


def test_unadmitted_implementation_identity_is_rejected(check_pack):
    check, _kwargs, _marker = check_pack
    descriptor_path = check.pack_root / check.descriptor_path
    descriptor = yaml.safe_load(descriptor_path.read_text(encoding="utf-8"))
    descriptor["implementation"]["path"] = "extensions/timeline.check/neighbour.py"
    (check.pack_root / descriptor["implementation"]["path"]).write_text(
        "def run(ctx): return ['must not run']\n", encoding="utf-8")
    descriptor_path.write_text(yaml.safe_dump(descriptor), encoding="utf-8")
    with pytest.raises(CanonicalPackValidationError, match="declared Python file resource"):
        validate_canonical_pack(check.pack_root)


@pytest.fixture
def transaction_checks(check_pack, monkeypatch):
    """Real Check execution on detached documents, with counted trust boundaries."""
    import inspect
    import json
    from pathlib import Path
    from astrid.core.pack import canonical, checks
    from astrid.sdk.timeline import check as sdk_check
    from astrid.sdk.timeline.checkout import Checkout

    check, kwargs, _marker = check_pack
    counts = dict(discovery=0, admission=0, invocation=0, readmission=0, source_hash=0)
    contexts = []
    discover, admit = checks.discover_checks, checks.CheckAdmissionContext.admit
    invoke, validate, read_bytes = checks.invoke_check, canonical.validate_canonical_pack, Path.read_bytes

    def discover_counted(**options):
        counts["discovery"] += 1
        return discover(**{**options, **kwargs})

    def admit_counted(context, root):
        if Path(root).resolve() not in context._entries:
            counts["admission"] += 1
        if context not in contexts:
            contexts.append(context)
        return admit(context, root)

    def invoke_counted(*args, **options):
        counts["invocation"] += 1
        return invoke(*args, **options)

    def validate_counted(*args, **options):
        if inspect.currentframe().f_back.f_code.co_name == "invoke_check":
            counts["readmission"] += 1
        return validate(*args, **options)

    def read_counted(path):
        # This is the final fresh read immediately followed by the SHA comparison,
        # distinct from resource hashing inside canonical re-admission.
        if inspect.currentframe().f_back.f_code.co_name == "invoke_check":
            counts["source_hash"] += 1
        return read_bytes(path)

    monkeypatch.setattr(checks, "discover_checks", discover_counted)
    monkeypatch.setattr(checks.CheckAdmissionContext, "admit", admit_counted)
    monkeypatch.setattr(checks, "invoke_check", invoke_counted)
    monkeypatch.setattr(canonical, "validate_canonical_pack", validate_counted)
    monkeypatch.setattr(Path, "read_bytes", read_counted)
    monkeypatch.setattr(sdk_check, "timeline_check_host_context", lambda checkout: CheckHostContext(
        "sdk", checkout.document(),
        lambda: [{"address": "c1.sprite", "asset": "R", "outline_integrity": 0.70}],
    ))
    fixture = Path(__file__).parents[2] / "fixtures/timeline_editing/tiny_publishable.json"
    checkout = Checkout(json.loads(fixture.read_text()))
    checkout._edit_local_only = True
    return checkout, counts, contexts


def test_outer_transaction_shares_one_admission_across_nested_prefix_and_base_reports(transaction_checks):
    checkout, counts, contexts = transaction_checks
    seen = []
    from astrid.sdk.timeline.checkout import Checkout
    from unittest.mock import patch
    original = Checkout._check

    def report(probe, **options):
        seen.append(options.get("_admission_context"))
        return original(probe, **options)

    with patch.object(Checkout, "_check", report):
        with checkout.edit("several prefixes", quiet=True) as outer:
            assert outer._check_admission is None
            checkout.clip("c1.rocket").set(x=90)
            with checkout.edit("nested") as nested:
                assert nested is outer
                checkout.clip("c1.rocket").set(y=44)
            checkout.clip("c1.rocket").set(opacity=0.8)
    assert len(seen) == 4  # final + three semantic prefixes; each also lints its base
    assert len(contexts) == 1 and all(context is contexts[0] for context in seen)
    # Bundled sprite integrity plus this fixture's demo check, on both revisions.
    assert counts == dict(discovery=1, admission=2, invocation=16, readmission=16, source_hash=16)
    assert outer._check_admission is None
    with pytest.raises(RuntimeError, match="closed"):
        outer._check_context()
    assert contexts[0]._closed and contexts[0]._entries == {} and contexts[0]._registry is None
    with pytest.raises(RuntimeError, match="closed"):
        contexts[0].registry()
    print("transaction trust-boundary counts:", counts)

    assert checkout._edit_depth == 0 and checkout._open_edit is None
    with checkout.edit("next", quiet=True) as later:
        checkout.clip("c1.rocket").set(x=96)  # sprite positions snap to six-pixel units
    assert later.changed, (checkout.clip("c1.rocket").get("x"), counts)
    checkout.check()
    checkout.lint()
    assert counts["discovery"] == 4  # next outer transaction, standalone check, standalone lint
    assert len(contexts) == 2 and contexts[0] is not contexts[1]


@pytest.mark.parametrize("exit_kind", ["dry-run", "no-change", "exception", "rollback", "unchecked"])
def test_transaction_context_is_lazy_and_disposed_on_every_exit(transaction_checks, monkeypatch, exit_kind):
    checkout, counts, contexts = transaction_checks
    from astrid.sdk.timeline import commands

    if exit_kind == "rollback":
        original = commands.finish_recording

        def refuse(*args):
            original(*args)
            raise CommandError("after report")

        monkeypatch.setattr(commands, "finish_recording", refuse)
    try:
        with checkout.edit("lifecycle", quiet=True, dry_run=exit_kind == "dry-run",
                           check=exit_kind != "unchecked") as tx:
            if exit_kind != "no-change":
                checkout.clip("c1.rocket").set(x=90)
            if exit_kind == "exception":
                raise RuntimeError("body failed")
    except (RuntimeError, CommandError) as exc:
        assert str(exc) in {"body failed", "after report"}
    assert tx._check_admission is None
    with pytest.raises(RuntimeError, match="closed"):
        tx._check_context()
    expected = int(exit_kind in {"dry-run", "rollback"})
    assert counts["discovery"] == expected
    assert all(context._closed and not context._entries and context._registry is None for context in contexts)
    if exit_kind == "rollback":
        monkeypatch.setattr(commands, "finish_recording", original)
    with checkout.edit("fresh after exit", quiet=True):
        checkout.clip("c1.rocket").set(x=95)
    assert counts["discovery"] == expected + 1
    assert len(contexts) == expected + 1


@pytest.mark.parametrize("changed", ["descriptor", "manifest", "implementation"])
def test_retained_transaction_admission_refuses_changed_closure(check_pack, changed):
    from astrid.core.pack.checks import CheckAdmissionContext, discover_checks

    check, kwargs, marker = check_pack
    context = CheckAdmissionContext()
    try:
        admitted = next(item for item in discover_checks(**kwargs, _admission_context=context)
                        if item.identity == IDENTITY)
        host = CheckHostContext("sdk", {}, lambda: [])
        invoke_check(admitted, host)
        marker.unlink()
        if changed == "implementation":
            path = check.pack_root / check.descriptor["implementation"]["path"]
            path.write_text("def run(ctx): return ['changed']\n")
        else:
            path = check.pack_root / (check.descriptor_path if changed == "descriptor" else "pack.yaml")
            payload = yaml.safe_load(path.read_text())
            if changed == "descriptor":
                payload["thresholds"]["damaged_art_below"] = 0.9
            else:
                payload["resources"] = [row for row in payload["resources"] if row["kind"] != "implementation"]
            path.write_text(yaml.safe_dump(payload))
        with pytest.raises((CommandError, CanonicalPackValidationError)):
            invoke_check(admitted, host)
        assert not marker.exists()
    finally:
        context.close()


def test_invocation_refuses_source_change_after_readmission(check_pack, monkeypatch):
    from astrid.core.pack import canonical

    check, _kwargs, marker = check_pack
    validate = canonical.validate_canonical_pack

    def change_after_admission(root):
        entry = validate(root)
        (check.pack_root / check.descriptor["implementation"]["path"]).write_text(
            "def run(ctx): return ['raced']\n")
        return entry

    monkeypatch.setattr(canonical, "validate_canonical_pack", change_after_admission)
    with pytest.raises(CommandError, match="changed during invocation"):
        invoke_check(check, CheckHostContext("sdk", {}, lambda: []))
    assert not marker.exists()
