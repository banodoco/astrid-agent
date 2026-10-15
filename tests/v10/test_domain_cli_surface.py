"""Product CLI surface tests (m4 plan step 24, task T25).

Proves the product-family registry contract: exactly five families,
runtime-declared nested mounts (shots under timelines, references under
media), rejection of missing/duplicate/unexpected/dynamic mounts, the
exclusion of operational/legacy/singular-run commands from product
dispatch, and the ``AstridClient``-passing handler boundary. Task T26
extends this file with the shared output-layer (exact JSON envelope
renderer, concise human output, stable exit codes) and the product help.
"""

from __future__ import annotations

import argparse
import json
import types

import pytest

from astrid.core.cli.domain_output import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_USAGE,
    envelope_dict,
    print_result,
    render_envelope_json,
    render_human,
)
from astrid.core.cli.domain_product import (
    EXCLUDED_FROM_PRODUCT_CENSUS,
    PRODUCT_FAMILIES,
    ProductRegistryError,
    RuntimeMount,
    _validate_mounts,
    build_product_mounts,
    family_mount,
    is_product_family,
    is_registered_family,
    product_top_level_commands,
    read_runtime_cli_mounts,
    run_product_family,
)
from astrid.core.cli.registration import CommandSpec, register_product_commands
from astrid.core.gateway.help import _print_product_help, _product_help_text
from astrid.sdk.contracts import DomainResult, ErrorObject

# ---------------------------------------------------------------------------
# Census
# ---------------------------------------------------------------------------


def test_product_census_is_exactly_seven_families() -> None:
    assert tuple(PRODUCT_FAMILIES) == (
        "projects",
        "documents",
        "preferences",
        "media",
        "tasks",
        "runs",
        "timelines",
    )
    assert product_top_level_commands() == frozenset(PRODUCT_FAMILIES)
    assert len(PRODUCT_FAMILIES) == 7


def test_operational_families_are_excluded_from_product_census() -> None:
    """serve/doctor/backup are operational families outside the census."""
    assert EXCLUDED_FROM_PRODUCT_CENSUS == frozenset(
        {"serve", "doctor", "backup"}
    )
    for excluded in EXCLUDED_FROM_PRODUCT_CENSUS:
        assert not is_product_family(excluded)
        assert excluded not in product_top_level_commands()


def test_is_product_family_validation() -> None:
    assert all(is_product_family(family) for family in PRODUCT_FAMILIES)
    assert not is_product_family("shots")
    assert not is_product_family("references")
    assert not is_product_family("")
    assert not is_product_family(None)


def test_is_registered_family_includes_nested_mounts() -> None:
    assert all(is_registered_family(family) for family in PRODUCT_FAMILIES)
    assert is_registered_family("shots")
    assert is_registered_family("references")
    assert not is_registered_family("serve")
    assert not is_registered_family("run")


# ---------------------------------------------------------------------------
# Mounts
# ---------------------------------------------------------------------------


def test_mounts_include_core_and_the_two_nested_mounts() -> None:
    mounts = build_product_mounts()
    by_family = {mount.family: mount for mount in mounts}
    assert set(by_family) == {
        "projects",
        "documents",
        "preferences",
        "media",
        "tasks",
        "runs",
        "timelines",
        "shots",
        "references",
    }
    # Core families mount at their own top-level token.
    for family in ("projects", "media", "tasks", "runs"):
        assert by_family[family].mount_path == (family,)
        assert by_family[family].declared_by == "core"
    # Runtime-owned timelines.
    assert by_family["timelines"].mount_path == ("timelines",)
    assert by_family["timelines"].declared_by == "runtime:runtime"
    # Exactly the two declared nested mounts.
    assert by_family["shots"].mount_path == ("timelines", "shots")
    assert by_family["shots"].declared_by == "runtime:runtime"
    assert by_family["references"].mount_path == ("media", "references")
    assert by_family["references"].declared_by == "runtime:runtime"


def test_runtime_mounts_come_from_reviewed_declarations() -> None:
    """The loader reads only the three fixed runtime mount declarations."""
    declared = {mount.family: mount.token for mount in read_runtime_cli_mounts()}
    assert declared == {
        "timelines": "timelines",
        "shots": "timelines shots",
        "references": "media references",
    }


def test_family_mount_resolves_and_rejects() -> None:
    assert family_mount("shots").mount_path == ("timelines", "shots")
    assert family_mount("timelines").declared_by == "runtime:runtime"
    with pytest.raises(ProductRegistryError):
        family_mount("serve")


# ---------------------------------------------------------------------------
# Mount validation: missing / duplicate / unexpected / dynamic
# ---------------------------------------------------------------------------


def test_missing_required_runtime_mount_is_rejected() -> None:
    """A runtime declaration that omits a required mount fails closed."""
    with pytest.raises(ProductRegistryError, match="missing mount"):
        _validate_mounts(
            PRODUCT_FAMILIES,
            (
                RuntimeMount("timelines", "timelines", "runtime"),
                RuntimeMount("shots", "timelines shots", "runtime"),
                # references declaration is missing entirely.
            ),
        )


def test_empty_runtime_mount_is_rejected() -> None:
    with pytest.raises(ProductRegistryError, match="missing mount"):
        _validate_mounts(
            PRODUCT_FAMILIES,
            (
                RuntimeMount("timelines", "timelines", "runtime"),
                RuntimeMount("shots", "", "runtime"),
                RuntimeMount("references", "media references", "runtime"),
            ),
        )


def test_duplicate_runtime_family_is_rejected() -> None:
    """Two runtime declarations for one family are a duplicate mount."""
    with pytest.raises(ProductRegistryError, match="duplicate mount"):
        _validate_mounts(
            PRODUCT_FAMILIES,
            (
                RuntimeMount("timelines", "timelines", "runtime"),
                RuntimeMount("shots", "timelines shots", "runtime"),
                RuntimeMount("references", "media references", "runtime"),
                RuntimeMount("shots", "timelines shots", "runtime-other"),
            ),
        )


def test_unexpected_runtime_family_is_rejected() -> None:
    """A runtime declaration outside the frozen registry is rejected."""
    with pytest.raises(ProductRegistryError, match="unexpected mount"):
        _validate_mounts(
            PRODUCT_FAMILIES,
            (
                RuntimeMount("timelines", "timelines", "runtime"),
                RuntimeMount("shots", "timelines shots", "runtime"),
                RuntimeMount("references", "media references", "runtime"),
                RuntimeMount("sixth-family", "projects sixth-family", "runtime-extra"),
            ),
        )


def test_unexpected_mount_path_is_rejected() -> None:
    """A declared family at a different mount path is rejected."""
    with pytest.raises(ProductRegistryError, match="unexpected mount"):
        _validate_mounts(
            PRODUCT_FAMILIES,
            (
                RuntimeMount("timelines", "timelines", "runtime"),
                RuntimeMount("shots", "media references", "runtime"),
                RuntimeMount("references", "media references", "runtime"),
            ),
        )


def test_core_family_re_mounted_at_other_path_is_rejected() -> None:
    with pytest.raises(ProductRegistryError, match="unexpected mount"):
        _validate_mounts(
            PRODUCT_FAMILIES,
            (
                RuntimeMount("timelines", "media timelines", "runtime"),
                RuntimeMount("shots", "timelines shots", "runtime"),
                RuntimeMount("references", "media references", "runtime"),
            ),
        )


def test_no_dynamic_mount_source_exists() -> None:
    """Mounts can only come from the fixed runtime declarations.

    ``build_product_mounts`` has no injection point and the loader accepts
    no root argument: a mount from any other directory is impossible by
    construction, so the registry can never discover a dynamic mount.
    """
    import inspect

    from astrid.core.cli import domain_product

    loader = inspect.signature(domain_product.read_runtime_cli_mounts)
    assert loader.parameters == {}
    builder = inspect.signature(domain_product.build_product_mounts)
    assert builder.parameters == {}


# ---------------------------------------------------------------------------
# Handler boundary: AstridClient is passed to rule-free handlers
# ---------------------------------------------------------------------------


def test_register_product_commands_stamps_client_and_family() -> None:
    parser = argparse.ArgumentParser(prog="astrid projects")
    sub = parser.add_subparsers(dest="command", required=True)
    client = object()

    def _configure(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--json", action="store_true")
        subparser.set_defaults(
            handler=lambda parsed: (parsed.client, parsed.family, parsed.json)
        )

    register_product_commands(
        sub,
        [CommandSpec("ls", help="List projects", configure=_configure)],
        family="projects",
        client=client,
    )

    parsed = parser.parse_args(["ls", "--json"])
    assert parsed.client is client
    assert parsed.family == "projects"
    assert parsed.handler(parsed) == (client, "projects", True)


def test_register_product_commands_accepts_nested_families() -> None:
    parser = argparse.ArgumentParser(prog="astrid timelines shots")
    sub = parser.add_subparsers(dest="command", required=True)
    register_product_commands(
        sub,
        [
            CommandSpec(
                "list",
                help="List shots",
                configure=lambda p: p.set_defaults(handler=lambda parsed: 0),
            )
        ],
        family="shots",
        client=object(),
    )
    parsed = parser.parse_args(["list"])
    assert parsed.family == "shots"


def test_register_product_commands_rejects_unknown_family() -> None:
    parser = argparse.ArgumentParser(prog="astrid")
    sub = parser.add_subparsers(dest="command", required=True)
    with pytest.raises(ValueError, match="not a registered product family"):
        register_product_commands(
            sub,
            [
                CommandSpec(
                    "ls",
                    help="List",
                    configure=lambda p: p.set_defaults(handler=lambda parsed: 0),
                )
            ],
            family="serve",
            client=object(),
        )


class _StubParser:
    def __init__(self, client: object) -> None:
        self._client = client

    def parse_args(self, args: list[str]) -> argparse.Namespace:
        return argparse.Namespace(
            handler=lambda parsed: parsed.client is not None and 3,
            client=self._client,
            args=args,
        )


def _stub_family_module() -> types.ModuleType:
    module = types.ModuleType("stub_family")
    module.build_parser = lambda client: _StubParser(client)
    return module


def test_run_product_family_passes_client_to_handler() -> None:
    client = object()
    stub = _stub_family_module()
    result = run_product_family(
        "projects",
        ["ls"],
        client=client,
        _parser_modules={"projects": stub},
    )
    assert result == 3


def test_run_product_family_rejects_non_product_family() -> None:
    stub = _stub_family_module()
    with pytest.raises(ProductRegistryError, match="not a product family"):
        run_product_family(
            "serve",
            [],
            client=object(),
            _parser_modules={"serve": stub},
        )
    with pytest.raises(ProductRegistryError, match="not a product family"):
        run_product_family(
            "shots",
            [],
            client=object(),
            _parser_modules={"shots": stub},
        )


def test_run_product_family_rejects_missing_builder() -> None:
    with pytest.raises(ProductRegistryError, match="no in-tree parser builder"):
        run_product_family(
            "projects",
            [],
            client=object(),
            _parser_modules={},
        )


# ---------------------------------------------------------------------------
# Dispatch boundary
# ---------------------------------------------------------------------------


def _call_dispatch_product(args: list[str], monkeypatch: pytest.MonkeyPatch):
    from astrid.core.gateway import dispatch

    return dispatch._dispatch_product(args)


class _FakeClient:
    closed = False

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        type(self).closed = True


def test_dispatch_product_routes_family_and_closes_client(monkeypatch) -> None:
    import astrid.core.cli.domain_product as domain_product
    import astrid.sdk.client as sdk_client

    seen: dict[str, object] = {}

    def _fake_open(cls, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return _FakeClient()

    def _fake_run(family, args, *, client, **kwargs):  # noqa: ANN001, ANN002, ANN003
        seen["family"] = family
        seen["args"] = list(args)
        seen["client"] = client
        return 7

    monkeypatch.setattr(sdk_client.AstridClient, "open_from_launcher", classmethod(_fake_open))
    monkeypatch.setattr(domain_product, "run_product_family", _fake_run)

    from astrid.core.gateway import dispatch

    assert dispatch._dispatch_product(["projects", "ls", "--json"]) == 7
    assert seen["family"] == "projects"
    assert seen["args"] == ["ls", "--json"]
    assert isinstance(seen["client"], _FakeClient)
    assert _FakeClient.closed


def test_dispatch_product_skips_pack_host_for_runtime_only_reads(monkeypatch) -> None:
    import astrid.core.cli.domain_product as domain_product
    import astrid.sdk.client as sdk_client

    seen: dict[str, object] = {}

    def _fake_open(cls, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        seen["start_pack_host"] = kwargs.get("start_pack_host")
        return _FakeClient()

    monkeypatch.setattr(sdk_client.AstridClient, "open_from_launcher", classmethod(_fake_open))
    monkeypatch.setattr(domain_product, "run_product_family", lambda *args, **kwargs: 0)

    from astrid.core.gateway import dispatch

    assert dispatch._dispatch_product(["runs", "open", "--project", "demo"]) == 0
    assert seen["start_pack_host"] is False
    assert dispatch._dispatch_product(["runs", "retry", "RUN1"]) == 0
    assert seen["start_pack_host"] is False


def test_tasks_list_uses_existing_runtime_observer_only(monkeypatch) -> None:
    import astrid.core.cli.domain_product as domain_product
    import astrid.sdk.client as sdk_client

    seen: list[str] = []

    def _fake_existing(cls, **kwargs):  # noqa: ANN001, ANN002
        seen.append("existing")
        return _FakeClient()

    def _fake_launcher(cls, **kwargs):  # noqa: ANN001, ANN002
        seen.append("launcher")
        return _FakeClient()

    monkeypatch.setattr(
        sdk_client.AstridClient,
        "open_existing_from_launcher",
        classmethod(_fake_existing),
    )
    monkeypatch.setattr(
        sdk_client.AstridClient,
        "open_from_launcher",
        classmethod(_fake_launcher),
    )
    monkeypatch.setattr(domain_product, "run_product_family", lambda *args, **kwargs: 0)

    from astrid.core.gateway import dispatch

    assert dispatch._dispatch_product(["tasks", "list", "--project", "demo"]) == 0
    assert seen == ["existing"]
    # Other task verbs preserve the established explicit launcher boundary.
    assert dispatch._dispatch_product(["tasks", "show", "TASK1"]) == 0
    assert seen == ["existing", "launcher"]


def test_native_timeline_visualize_skips_optional_pack_host(monkeypatch) -> None:
    import astrid.core.cli.domain_product as domain_product
    import astrid.sdk.client as sdk_client

    seen: dict[str, object] = {}

    def _fake_open(cls, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        seen["start_pack_host"] = kwargs.get("start_pack_host")
        return _FakeClient()

    monkeypatch.setattr(sdk_client.AstridClient, "open_from_launcher", classmethod(_fake_open))
    monkeypatch.setattr(domain_product, "run_product_family", lambda *args, **kwargs: 0)

    from astrid.core.gateway import dispatch

    assert dispatch._dispatch_product(
        [
            "timelines",
            "visualize",
            "TIMELINE",
            "--project",
            "PROJECT",
            "--show",
            "inputs",
            "--hide",
            "output",
            "--format",
            "md,png",
        ]
    ) == 0
    assert seen["start_pack_host"] is True


def test_pack_host_requirement_is_declared_by_command_not_read_allowlist() -> None:
    from astrid.core.cli.domain_product import command_requires_pack_host

    assert command_requires_pack_host("projects", ["list"]) is False
    assert command_requires_pack_host("runs", ["retry"]) is False
    assert command_requires_pack_host("timelines", ["visualize"]) is True
    assert command_requires_pack_host("timelines", ["render"]) is True
    assert command_requires_pack_host("timelines", ["shots", "list"]) is False
    assert command_requires_pack_host("media", ["references", "show"]) is False
    assert command_requires_pack_host("timelines", ["future-command"]) is True


def test_dispatch_product_help_does_not_open_client(monkeypatch, capsys) -> None:
    """Family/verb help remains available without touching the store."""
    import astrid.sdk.client as sdk_client

    def _fail_open(cls, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("help must not compose an AstridClient")

    monkeypatch.setattr(sdk_client.AstridClient, "open", classmethod(_fail_open))

    from astrid.core.gateway import dispatch

    with pytest.raises(SystemExit) as excinfo:
        dispatch._dispatch_product(["timelines", "show", "--help"])
    assert excinfo.value.code == 0
    assert "astrid timelines show" in capsys.readouterr().out


def test_timeline_check_listing_does_not_open_runtime(monkeypatch) -> None:
    import astrid.core.cli.domain_product as domain_product
    import astrid.sdk.client as sdk_client

    seen: dict[str, object] = {}

    def _fail_open(cls, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("check metadata listing must not open Runtime")

    def _fake_run(family, args, *, client, **kwargs):  # noqa: ANN001, ANN002, ANN003
        seen["family"] = family
        seen["args"] = list(args)
        seen["client"] = client
        return 0

    monkeypatch.setattr(sdk_client.AstridClient, "open_from_launcher", classmethod(_fail_open))
    monkeypatch.setattr(domain_product, "run_product_family", _fake_run)

    from astrid.core.gateway import dispatch

    assert dispatch._dispatch_product(["timelines", "lint", "--list-checks"]) == 0
    assert seen == {"family": "timelines", "args": ["lint", "--list-checks"], "client": None}


def test_dispatch_product_rejects_excluded_commands_before_opening(
    monkeypatch,
) -> None:
    import astrid.sdk.client as sdk_client
    from astrid.core.contracts.errors import AstridError

    def _fail_open(cls, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("client must not be opened for excluded commands")

    monkeypatch.setattr(sdk_client.AstridClient, "open", classmethod(_fail_open))

    from astrid.core.gateway import dispatch

    for excluded in ("serve", "doctor", "backup", "sessions", "renderers"):
        with pytest.raises(AstridError, match="unknown product command"):
            dispatch._dispatch_product([excluded])


def test_dispatch_product_requires_a_family(monkeypatch) -> None:
    import astrid.sdk.client as sdk_client
    from astrid.core.contracts.errors import AstridError

    monkeypatch.setattr(
        sdk_client.AstridClient,
        "open",
        classmethod(lambda cls, *args, **kwargs: _FakeClient()),
    )

    from astrid.core.gateway import dispatch

    with pytest.raises(AstridError, match="product family is required"):
        dispatch._dispatch_product([])


def test_product_census_hook_matches_domain_registry() -> None:
    from astrid.core.gateway import dispatch

    assert dispatch._product_top_level_commands() == product_top_level_commands()


def test_top_level_commands_include_product_and_operator_families() -> None:
    from astrid.core.gateway import dispatch

    assert dispatch._top_level_commands() == frozenset(
        {
            "projects",
            "preferences",
            "documents",
            "timelines",
            "media",
            "tasks",
            "runs",
            "doctor",
            "backup",
            "dev",
        }
    )
    assert len(dispatch._top_level_commands()) == 10


def test_all_five_product_families_route_through_product_dispatch(
    monkeypatch,
) -> None:
    """Every product-family handler prepends its token to _dispatch_product."""
    from astrid.core.gateway import dispatch

    seen: dict[str, object] = {}

    def _fake_product(args):  # noqa: ANN001
        seen["args"] = list(args)
        return 7

    monkeypatch.setattr(dispatch, "_dispatch_product", _fake_product)
    for family in ("projects", "timelines", "media", "tasks", "runs"):
        handler = dispatch._TOP_LEVEL_HANDLERS[family]
        assert handler(["list", "--json"]) == 7
        assert seen["args"] == [family, "list", "--json"]


# ---------------------------------------------------------------------------
# Shared output layer (plan step 24 renderer, task T26)
# ---------------------------------------------------------------------------


def test_renderer_emits_exact_envelope_for_success() -> None:
    result = DomainResult.success(
        {"slug": "demo", "name": "Demo"}, idempotency_key="key-1"
    )
    envelope = json.loads(render_envelope_json(result))
    assert set(envelope) == {"ok", "data", "error", "receipt", "idempotency_key"}
    assert envelope["ok"] is True
    assert envelope["data"] == {"slug": "demo", "name": "Demo"}
    assert envelope["error"] is None
    assert envelope["receipt"] is None
    assert envelope["idempotency_key"] == "key-1"


def test_renderer_emits_exact_envelope_for_failure() -> None:
    error = ErrorObject(
        code="not_found", message="missing", details={"kind": "project"}
    )
    result = DomainResult.failure(error, idempotency_key="key-2")
    envelope = json.loads(render_envelope_json(result))
    assert set(envelope) == {"ok", "data", "error", "receipt", "idempotency_key"}
    assert envelope["ok"] is False
    assert envelope["data"] is None
    assert envelope["error"] == {
        "code": "not_found",
        "message": "missing",
        "details": {"kind": "project"},
    }
    assert envelope["receipt"] is None


def test_renderer_envelope_keys_match_sdk_contract() -> None:
    from astrid.sdk.contracts import ENVELOPE_KEYS as SDK_ENVELOPE_KEYS

    envelope = json.loads(
        render_envelope_json(DomainResult.success({"slug": "demo"}))
    )
    assert tuple(sorted(envelope)) == tuple(sorted(SDK_ENVELOPE_KEYS))


def test_renderer_accepts_plain_envelope_dict_and_rejects_drift() -> None:
    good = {
        "ok": True,
        "data": {"slug": "demo"},
        "error": None,
        "receipt": None,
        "idempotency_key": "k",
    }
    assert envelope_dict(good) == good
    for bad in (
        # missing key
        {"ok": True, "data": None, "error": None, "receipt": None},
        # extra key
        {
            "ok": True,
            "data": None,
            "error": None,
            "receipt": None,
            "idempotency_key": "k",
            "extra": 1,
        },
        # non-boolean ok
        {
            "ok": "yes",
            "data": None,
            "error": None,
            "receipt": None,
            "idempotency_key": "k",
        },
    ):
        with pytest.raises(ValueError):
            envelope_dict(bad)
    with pytest.raises(ValueError):
        envelope_dict("not an envelope")


def test_renderer_json_round_trips_through_domain_result() -> None:
    result = DomainResult.success(
        {"media_id": "M1", "content_hash": "abc"}, idempotency_key="key-3"
    )
    assert DomainResult.from_json(render_envelope_json(result)) == result


def test_human_output_is_concise() -> None:
    assert render_human(DomainResult.success({"slug": "demo"})) == "slug: demo"
    assert render_human(DomainResult.success({"media_id": "M1"})) == "media_id: M1"
    rendered_rows = render_human(DomainResult.success([{"id": "a"}, {"id": "b"}]))
    assert "2 result(s)" in rendered_rows
    assert "a" in rendered_rows and "b" in rendered_rows
    assert render_human(DomainResult.success(None)) == "ok"
    failure = DomainResult.failure(
        ErrorObject(code="conflict", message="stale version", details={})
    )
    assert render_human(failure) == "error conflict: stale version"


def test_human_output_summarizes_every_list_row_and_nested_report() -> None:
    rendered = render_human(DomainResult.success([
        {"task_id": "T-1", "state": "succeeded", "capability": "rendering.render"},
        {"task_id": "T-2", "state": "failed", "capability": "pixel.snap"},
    ]))
    assert "2 result(s)" in rendered
    assert "T-1" in rendered and "T-2" in rendered
    report = DomainResult.success({"kind": "report", "preview": {"text": '{"ok":true,"count":2}'}})
    assert "report:" in render_human(report)
    assert '"ok": true' in render_human(report)


def test_print_result_returns_stable_exit_codes(capsys) -> None:
    ok = DomainResult.success({"slug": "demo"}, idempotency_key="k")
    failure = DomainResult.failure(
        ErrorObject(code="not_found", message="missing", details={})
    )
    assert EXIT_OK == 0
    assert EXIT_FAILURE == 1
    assert EXIT_USAGE == 2

    # Human success -> stdout, exit 0.
    assert print_result(ok) == EXIT_OK
    out, err = capsys.readouterr()
    assert out == "slug: demo\n"
    assert err == ""

    # Human failure -> stderr, exit 1.
    assert print_result(failure) == EXIT_FAILURE
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "error not_found: missing\n"

    # JSON mode always prints the exact envelope on stdout; the exit code
    # still carries the outcome.
    assert print_result(ok, as_json=True) == EXIT_OK
    out, err = capsys.readouterr()
    envelope = json.loads(out)
    assert set(envelope) == {"ok", "data", "error", "receipt", "idempotency_key"}
    assert envelope["ok"] is True
    assert err == ""

    assert print_result(failure, as_json=True) == EXIT_FAILURE
    out, err = capsys.readouterr()
    envelope = json.loads(out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "not_found"
    assert err == ""


# ---------------------------------------------------------------------------
# Product help (plan step 24 renderer/help, task T26)
# ---------------------------------------------------------------------------


def test_product_help_text_declares_exact_census_and_mounts() -> None:
    text = _product_help_text()
    assert (
        "Family census (exactly nine families): "
        "projects timelines media tasks runs preferences documents doctor backup" in text
    )
    for family in PRODUCT_FAMILIES:
        assert family in text
    for operational in ("doctor", "backup"):
        assert operational in text
    assert "timelines shots" in text
    assert "media references" in text
    # ``serve`` remains a rejected legacy token but is no longer an advertised
    # operational family after the Stage1 runtime cutover.
    for excluded in EXCLUDED_FROM_PRODUCT_CENSUS - {"serve"}:
        assert excluded in text


def test_product_help_census_matches_explicit_registry() -> None:
    text = _product_help_text()
    census_line = next(
        line for line in text.splitlines() if line.startswith("Family census")
    )
    census = census_line.split(":", 1)[1].split()
    assert tuple(census[7:]) == ("doctor", "backup")
    assert set(census[:7]) == set(PRODUCT_FAMILIES)
    # Every advertised product family is a real registered product family.
    for family in census[:7]:
        assert is_product_family(family)
    # Only the two current operational families are advertised; ``serve`` is
    # a retired rejected token, not a public family.
    assert set(census[7:]) == {"doctor", "backup"}


def test_product_help_documents_stable_exit_codes() -> None:
    text = _product_help_text()
    assert "Exit codes:" in text
    assert "0  success (envelope ok=true)" in text
    assert "1  typed SDK error (envelope ok=false)" in text
    assert "2  usage/parse error" in text


def test_product_help_documents_json_envelope_convention() -> None:
    text = _product_help_text()
    assert "--json" in text
    assert "ok/data/error/receipt/idempotency_key" in text
    assert "doctor emits" in text
    assert "backup emits its runtime result object" in text


def test_product_help_lists_current_timeline_visualize_and_render_verbs() -> None:
    text = _product_help_text()
    assert "projects    [kernel] project create/list/show/update/select/current" in text
    assert "runs        [kernel] run list/show/cancel/retry/events/open" in text
    assert "[pack: timeline] timelines list/show/replace-parent-media/archive/recover/history/diff/visualize/render" in text


def test_print_product_help_prints_to_stdout(capsys) -> None:
    _print_product_help()
    captured = capsys.readouterr()
    assert captured.out.startswith("Astrid product commands")
    assert "Family census (exactly nine families)" in captured.out
