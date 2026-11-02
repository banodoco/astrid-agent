"""Deterministic authority-lint mutation fixtures (m1 plan step 22 / NSA-3).

Every rule of :mod:`scripts.reshape.authority_lint` is proven by a
representative mutation fixture that must fail exactly that rule, and each
documented exemption is proven by a fixture that must stay clean:

- **imports**: kernel-to-pack imports (every ``astrid/core`` file except the
  single composition exemption), pack-to-pack imports between the m1 schema
  packs, and the documented legacy rendering prefix that stays
  legal in the kernel;
- **writer authority**: ``DatabaseWriter(`` / writable ``sqlite3.connect``
  outside the kernel store fails; ``mode=ro`` probes and the conformance
  kit's scratch writers stay legal;
- **legacy authorities**: supported v10 entry paths may never import the
  legacy file/JSONL/FSA/Supabase authorities, while legacy files *outside*
  those paths remain in-tree legally;
- **schema ownership**: kernel FKs to pack tables, cross-pack FKs,
  undeclared tables/indexes, forbidden schema, closed stream vocabulary,
  and pack convenience alias/default columns all fail;
- **exemptions**: only the gateway serve root may import the standard pack
  composition, and only legacy paths outside the supported v10 entries are
  exempt from the legacy-authority rule.

The m3 reference and shot surfaces get their own adversarial fixtures and
negative controls: kernel imports of ``references``/``shots`` repository and
conformance modules fail, cross-pack implementation imports between the two
new packs fail, a pack-owned ``DatabaseWriter`` or writable ``sqlite3.connect``
inside either pack fails, and kernel FKs to ``project_references``/``shots``
plus cross-pack FKs between the new packs fail. The negative controls prove
that allowed kernel currency stays legal: kernel-module imports from packs,
own-pack imports (the real conformance modules import their own repository),
caller-supplied ``UnitOfWork`` use (``uow.execute`` / ``uow.connection``
without constructing a writer), ``mode=ro`` probes, and pack-to-kernel FKs
(``projects``/``media``/``tasks``) never trip a rule.

All fixtures are pure text under ``tmp_path``; no fixture touches the real
repository tree.
"""

from __future__ import annotations

from pathlib import Path

from scripts.reshape.authority_lint import (
    _installed_rel,
    _lint_installed_source_patterns,
    _rel,
    lint_import_boundaries,
    lint_legacy_authorities,
    lint_removed_authorities,
    lint_schema_ownership,
    lint_writer_authority,
    run_installed_authority_lint,
    run_authority_lint,
)

# The real timeline schema-pack manifest shape (11 snake_case fields), used
# by every fixture that needs a declared pack table/vocabulary.
_TIMELINE_MANIFEST = """\
id: timeline
version: 1
depends_on:
  - core >= 1
migrations:
  - version: 1
    name: initial
    path: migrations/0001_initial.sql
    tables:
      - timelines
stream_types:
  - timeline.timeline
event_kinds:
  - timeline.created
  - timeline.saved
  - timeline.config_replaced
command_kinds:
  - timeline.create
  - timeline.save
  - timeline.replace_config
repositories:
  - TimelineRepository
conformance:
  - replay
cli_mounts:
  timelines: timelines
bridge_mounts:
  - timelines
"""

_SHOTS_MANIFEST = """\
id: shots
version: 1
depends_on:
  - core >= 1
migrations:
  - version: 1
    name: initial
    path: migrations/0001_initial.sql
    tables:
      - shots
      - shot_items
stream_types:
  - shot.shot
event_kinds:
  - shot.created
  - shot.item_added
  - shot.item_removed
  - shot.reordered
command_kinds:
  - shot.create
  - shot.add_item
  - shot.remove_item
  - shot.reorder
repositories:
  - ShotRepository
conformance:
  - replay
  - mismatch_before_mutation
  - same_project
  - vocabulary
  - writer_ownership
  - crash_atomicity
  - hash_chain
cli_mounts:
  shots: timelines shots
bridge_mounts: []
"""

# The real references schema-pack manifest shape: the pack owns the three
# project_references/media_references/reference_links tables, the pack-owned
# ``reference.reference`` aggregate stream, and the receipt-backed lifecycle/
# media/link commands and events (m3).
_REFERENCES_MANIFEST = """\
id: references
version: 1
depends_on:
  - core >= 1
migrations:
  - version: 1
    name: initial
    path: migrations/0001_initial.sql
    tables:
      - project_references
      - media_references
      - reference_links
stream_types:
  - reference.reference
event_kinds:
  - reference.created
  - reference.archived
  - reference.media_associated
  - reference.primary_changed
  - reference.linked
command_kinds:
  - reference.create
  - reference.archive
  - reference.associate
  - reference.set_primary
  - reference.link
repositories:
  - ReferenceRepository
conformance:
  - replay
  - mismatch_before_mutation
  - same_project
  - vocabulary
  - writer_ownership
  - crash_atomicity
  - hash_chain
cli_mounts:
  references: media references
bridge_mounts: []
"""


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _bootstrap(root: Path, *, with_shots: bool = False) -> None:
    """Minimal lint-scan root: core + packs with valid pack manifests.

    The references pack is always present (it is a frozen m3 schema pack);
    its migration FK's inward to kernel tables only (projects/media/tasks),
    mirroring the real pack so the clean baseline proves pack-to-kernel FKs
    are the allowed kernel currency. The shots pack is opt-in because some
    m1 fixtures exercise a timeline-only composition.
    """
    _write(root, "astrid/core/__init__.py", "")
    _write(root, "astrid/packs/__init__.py", "")
    _write(root, "astrid/packs/timeline/__init__.py", "")
    _write(root, "astrid/packs/timeline/schema-pack.yaml", _TIMELINE_MANIFEST)
    _write(
        root,
        "astrid/packs/timeline/migrations/0001_initial.sql",
        "CREATE TABLE timelines (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  project_id TEXT NOT NULL,\n"
        "  event_stream_id TEXT NOT NULL,\n"
        "  name TEXT NOT NULL,\n"
        "  document_json TEXT NOT NULL,\n"
        "  asset_registry_json TEXT NOT NULL,\n"
        "  created_at TEXT NOT NULL,\n"
        "  updated_at TEXT NOT NULL\n"
        ");\n",
    )
    _write(root, "astrid/packs/references/__init__.py", "")
    _write(root, "astrid/packs/references/schema-pack.yaml", _REFERENCES_MANIFEST)
    _write(
        root,
        "astrid/packs/references/migrations/0001_initial.sql",
        "CREATE TABLE project_references (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  project_id TEXT NOT NULL REFERENCES projects (id),\n"
        "  kind TEXT NOT NULL,\n"
        "  name TEXT NOT NULL,\n"
        "  created_at TEXT NOT NULL,\n"
        "  updated_at TEXT NOT NULL,\n"
        "  archived_at TEXT\n"
        ");\n"
        "CREATE TABLE media_references (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  reference_id TEXT NOT NULL REFERENCES project_references (id),\n"
        "  media_id TEXT NOT NULL REFERENCES media (id),\n"
        "  role TEXT NOT NULL,\n"
        "  context_task_id TEXT REFERENCES tasks (id),\n"
        "  ordinal INTEGER NOT NULL DEFAULT 0,\n"
        "  is_primary INTEGER NOT NULL DEFAULT 0,\n"
        "  created_at TEXT NOT NULL\n"
        ");\n"
        "CREATE TABLE reference_links (\n"
        "  from_reference_id TEXT NOT NULL REFERENCES project_references (id),\n"
        "  to_reference_id TEXT NOT NULL REFERENCES project_references (id),\n"
        "  kind TEXT NOT NULL,\n"
        "  created_at TEXT NOT NULL,\n"
        "  PRIMARY KEY (from_reference_id, to_reference_id, kind)\n"
        ");\n",
    )
    if with_shots:
        _write(root, "astrid/packs/shots/__init__.py", "")
        _write(root, "astrid/packs/shots/schema-pack.yaml", _SHOTS_MANIFEST)
        _write(
            root,
            "astrid/packs/shots/migrations/0001_initial.sql",
            "CREATE TABLE shots (\n"
            "  id TEXT PRIMARY KEY,\n"
            "  project_id TEXT NOT NULL REFERENCES projects (id),\n"
            "  name TEXT NOT NULL,\n"
            "  sort_key TEXT NOT NULL,\n"
            "  created_at TEXT NOT NULL,\n"
            "  updated_at TEXT NOT NULL\n"
            ");\n"
            "CREATE TABLE shot_items (\n"
            "  id TEXT PRIMARY KEY,\n"
            "  shot_id TEXT NOT NULL REFERENCES shots (id),\n"
            "  media_id TEXT NOT NULL REFERENCES media (id),\n"
            "  sort_key TEXT NOT NULL,\n"
            "  source_frame INTEGER,\n"
            "  created_at TEXT NOT NULL\n"
            ");\n",
        )


# ---------------------------------------------------------------------------
# Import boundaries
# ---------------------------------------------------------------------------


def test_kernel_to_pack_import_fails(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/core/evil.py",
        "from astrid.packs.timeline.repository import TimelineRepository\n",
    )
    errors = lint_import_boundaries(tmp_path)
    assert any(
        "astrid/core/evil.py: kernel-to-pack import 'astrid.packs.timeline.repository'"
        in error
        for error in errors
    ), errors


def test_composition_exemption_is_the_only_kernel_to_pack_allowed(
    tmp_path: Path,
) -> None:
    _bootstrap(tmp_path)
    # The current generic-host composition edge and the retained legacy
    # gateway-dispatch edge are both explicit; no other core path is exempt.
    _write(
        tmp_path,
        "astrid/core/execution/generic_host.py",
        "from astrid.packs.vibecomfy.production_engine import load_workflow_path\n"
        "from astrid.packs.vibecomfy.shared.bundle_inputs import load_bundle_inputs\n",
    )
    _write(
        tmp_path,
        "astrid/core/gateway/dispatch.py",
        "import astrid.packs.timeline.cli\n",
    )
    # Any other kernel file importing a pack is a violation.
    _write(
        tmp_path,
        "astrid/core/not_exempt.py",
        "import astrid.packs.timeline.cli\n",
    )
    errors = lint_import_boundaries(tmp_path)
    assert not any(
        "astrid/core/execution/generic_host.py" in error for error in errors
    ), errors
    assert not any(
        "astrid/core/gateway/dispatch.py" in error for error in errors
    ), errors
    assert any(
        "astrid/core/not_exempt.py: kernel-to-pack import 'astrid.packs.timeline.cli'"
        in error
        for error in errors
    ), errors


def test_installed_relative_identity_preserves_edges_and_negative_core_import(
    tmp_path: Path,
) -> None:
    """Installed scanning uses the package-root-relative source identity."""
    installed = tmp_path / "site-packages"
    _write(installed, "astrid/__init__.py", "")
    _write(installed, "astrid/core/__init__.py", "")
    _write(installed, "astrid/packs/__init__.py", "")
    _write(installed, "astrid/core/cli/domain_product.py", "")
    generic_host = _write(
        installed,
        "astrid/core/execution/generic_host.py",
        "from astrid.packs.vibecomfy.production_engine import load_workflow_path\n"
        "from astrid.packs.vibecomfy.shared.bundle_inputs import load_bundle_inputs\n",
    )
    dispatch = _write(
        installed,
        "astrid/core/gateway/dispatch.py",
        "import astrid.packs.timeline.cli\n",
    )
    _write(
        installed,
        "astrid/core/not_exempt.py",
        "import astrid.packs.timeline.cli\n",
    )

    assert _rel(generic_host, installed) == "astrid/core/execution/generic_host.py"
    assert _installed_rel(generic_host, installed) == "astrid/core/execution/generic_host.py"
    assert _installed_rel(dispatch, installed) == "astrid/core/gateway/dispatch.py"

    report = run_installed_authority_lint(installed)
    assert any(
        "astrid/core/not_exempt.py: kernel-to-pack import 'astrid.packs.timeline.cli'"
        in error
        for error in report.errors
    ), report.errors
    assert not any("astrid/core/execution/generic_host.py" in error for error in report.errors)
    assert not any("astrid/core/gateway/dispatch.py" in error for error in report.errors)


def test_detached_document_export_dumps_are_narrowly_allowed(
    tmp_path: Path,
) -> None:
    """Only the two checkout metadata dumps avoid semantic-writer errors."""
    root = tmp_path / "site-packages"
    _write(
        root,
        "astrid/sdk/documents.py",
        "import json\n"
        "\n"
        "def _write_metadata(path, value):\n"
        "    json.dump(value, handle)\n"
        "\n"
        "class Family:\n"
        "    def _checkout(self, metadata, handle):\n"
        "        json.dump(metadata, handle)\n"
        "\n"
        "def unrelated_semantic_writer(payload, handle):\n"
        "    json.dump(payload, handle)\n",
    )

    errors = _lint_installed_source_patterns(root)
    assert len(errors) == 1, errors
    assert errors[0].startswith("astrid/sdk/documents.py:")
    assert "semantic JSON writer is not a diagnostic output" in errors[0]


def test_pack_to_pack_import_fails(tmp_path: Path) -> None:
    _bootstrap(tmp_path, with_shots=True)
    _write(
        tmp_path,
        "astrid/packs/timeline/evil.py",
        "from astrid.packs.shots.repository import ShotRepository\n",
    )
    errors = lint_import_boundaries(tmp_path)
    assert any(
        "astrid/packs/timeline/evil.py: pack-to-pack import "
        "'astrid.packs.shots.repository' from pack 'timeline'"
        in error
        for error in errors
    ), errors


def test_kernel_to_pack_import_of_new_repository_surfaces_fails(
    tmp_path: Path,
) -> None:
    """The m3 references/shots repository and conformance modules are pack
    surfaces: a kernel module importing any of them is a kernel-to-pack
    violation (the same rule that guards the m1 timeline pack)."""
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/core/evil_repo_user.py",
        "from astrid.packs.references.repository import ReferenceRepository\n"
        "from astrid.packs.shots.repository import ShotRepository\n"
        "from astrid.packs.references.conformance import reference_create_spec\n"
        "from astrid.packs.shots.conformance import shot_create_spec\n",
    )
    errors = lint_import_boundaries(tmp_path)
    for module in (
        "astrid.packs.references.repository",
        "astrid.packs.shots.repository",
        "astrid.packs.references.conformance",
        "astrid.packs.shots.conformance",
    ):
        assert any(
            f"astrid/core/evil_repo_user.py: kernel-to-pack import {module!r}"
            in error
            for error in errors
        ), (module, errors)


def test_kernel_imports_of_kernel_currencies_stay_clean(tmp_path: Path) -> None:
    """Negative control: kernel modules legitimately import the kernel
    currency the packs receive (UoW, receipts, media/event services); those
    imports are never pack imports and must not be flagged."""
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/core/legit_user.py",
        "from astrid.core.store.uow import UnitOfWork\n"
        "from astrid.core.receipts.service import ReceiptService\n"
        "from astrid.core.repositories.media import MediaRepository\n"
        "from astrid.core.events.service import EventAppendService\n",
    )
    errors = lint_import_boundaries(tmp_path)
    assert errors == [], errors


def test_cross_pack_implementation_imports_between_new_packs_fail(
    tmp_path: Path,
) -> None:
    """The references and shots packs are independent implementations:
    either pack importing the other's repository or conformance modules is a
    pack-to-pack violation."""
    _bootstrap(tmp_path, with_shots=True)
    _write(
        tmp_path,
        "astrid/packs/references/evil.py",
        "from astrid.packs.shots.repository import ShotRepository\n",
    )
    _write(
        tmp_path,
        "astrid/packs/references/evil_conformance.py",
        "from astrid.packs.shots.conformance import shot_create_spec\n",
    )
    _write(
        tmp_path,
        "astrid/packs/shots/evil.py",
        "from astrid.packs.references.repository import ReferenceRepository\n",
    )
    errors = lint_import_boundaries(tmp_path)
    assert any(
        "astrid/packs/references/evil.py: pack-to-pack import "
        "'astrid.packs.shots.repository' from pack 'references'"
        in error
        for error in errors
    ), errors
    assert any(
        "astrid/packs/references/evil_conformance.py: pack-to-pack import "
        "'astrid.packs.shots.conformance' from pack 'references'"
        in error
        for error in errors
    ), errors
    assert any(
        "astrid/packs/shots/evil.py: pack-to-pack import "
        "'astrid.packs.references.repository' from pack 'shots'"
        in error
        for error in errors
    ), errors


def test_pack_import_of_kernel_currency_and_own_pack_stays_clean(
    tmp_path: Path,
) -> None:
    """Negative control mirroring the real conformance modules: a pack may
    import kernel currency (the conformance kit, UoW, receipts) and its own
    pack's repository, but never another pack."""
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/references/conformance.py",
        "from astrid.core.conformance.kit import CommandSpec\n"
        "from astrid.core.store.uow import UnitOfWork\n"
        "from astrid.core.receipts import ReceiptMismatchError\n"
        "from astrid.packs.references.repository import ReferenceRepository\n",
    )
    _write(
        tmp_path,
        "astrid/packs/shots/conformance.py",
        "from astrid.core.conformance.kit import CommandSpec\n"
        "from astrid.core.store.uow import UnitOfWork\n"
        "from astrid.packs.shots.repository import ShotRepository\n",
    )
    errors = lint_import_boundaries(tmp_path)
    assert errors == [], errors


def test_legacy_rendering_pack_imports_stay_legal_in_kernel(
    tmp_path: Path,
) -> None:
    """The rendering capability pack remains an allowed kernel import."""
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/core/legacy_user.py",
        "from astrid.packs.rendering.something import render\n"
    )
    errors = lint_import_boundaries(tmp_path)
    assert errors == [], errors


# ---------------------------------------------------------------------------
# Writer authority
# ---------------------------------------------------------------------------


def test_writer_construction_outside_kernel_store_fails(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/core/evil_writer.py",
        "from astrid.core.store.writer import DatabaseWriter\n"
        "writer = DatabaseWriter('/tmp/x.sqlite3', None)\n",
    )
    _write(
        tmp_path,
        "astrid/packs/timeline/evil_writer.py",
        "import sqlite3\n"
        "conn = sqlite3.connect('/tmp/y.sqlite3')\n",
    )
    errors = lint_writer_authority(tmp_path)
    assert any(
        "astrid/core/evil_writer.py: SQLite writer construction outside the "
        "kernel store" in error
        for error in errors
    ), errors
    assert any(
        "astrid/packs/timeline/evil_writer.py: SQLite writer construction "
        "outside the kernel store" in error
        for error in errors
    ), errors


def test_read_only_probe_is_not_a_writer(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/core/reader.py",
        "import sqlite3\n"
        "conn = sqlite3.connect('file:db.sqlite3?mode=ro', uri=True)\n",
    )
    errors = lint_writer_authority(tmp_path)
    assert errors == [], errors


def test_canonical_read_only_uri_probe_is_not_a_writer(tmp_path: Path) -> None:
    """The shared read-only URI helper must stay outside writer authority."""
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/core/reader.py",
        "import sqlite3\n"
        "conn = sqlite3.connect('file:/tmp/db.sqlite3?mode=ro', uri=True)\n",
    )
    errors = lint_writer_authority(tmp_path)
    assert errors == [], errors


def test_conformance_kit_scratch_writer_is_exempt(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/core/conformance/kit.py",
        "from astrid.core.store.writer import DatabaseWriter\n"
        "writer = DatabaseWriter('/tmp/scratch.sqlite3', None)\n",
    )
    errors = lint_writer_authority(tmp_path)
    assert errors == [], errors


def test_pack_owned_repository_and_conformance_writer_construction_fails(
    tmp_path: Path,
) -> None:
    """A DatabaseWriter or writable sqlite3.connect inside the references or
    shots pack (repository or conformance surface) is a second write
    authority: pack code must run inside the caller's kernel UoW."""
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/references/repository.py",
        "from astrid.core.store.writer import DatabaseWriter\n"
        "writer = DatabaseWriter('/tmp/refs.sqlite3', None)\n",
    )
    _write(
        tmp_path,
        "astrid/packs/shots/repository.py",
        "import sqlite3\n"
        "conn = sqlite3.connect('/tmp/shots.sqlite3')\n",
    )
    _write(
        tmp_path,
        "astrid/packs/references/conformance.py",
        "import sqlite3\n"
        "conn = sqlite3.connect('/tmp/refs_conformance.sqlite3')\n",
    )
    _write(
        tmp_path,
        "astrid/packs/shots/conformance.py",
        "from astrid.core.store.writer import DatabaseWriter\n"
        "writer = DatabaseWriter('/tmp/shots_conformance.sqlite3', None)\n",
    )
    errors = lint_writer_authority(tmp_path)
    for rel in (
        "astrid/packs/references/repository.py",
        "astrid/packs/shots/repository.py",
        "astrid/packs/references/conformance.py",
        "astrid/packs/shots/conformance.py",
    ):
        assert any(
            f"{rel}: SQLite writer construction outside the kernel store"
            in error
            for error in errors
        ), (rel, errors)


def test_caller_supplied_uow_use_is_not_a_writer(tmp_path: Path) -> None:
    """Negative control mirroring the real repositories: importing the
    ``DatabaseWriter``/``sqlite3`` names and running inside the caller's
    ``UnitOfWork`` (``uow.execute`` / ``uow.connection``) never constructs a
    writer and must stay clean; only construction is a violation."""
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/references/repository.py",
        "import sqlite3\n"
        "from astrid.core.store.uow import UnitOfWork\n"
        "from astrid.core.store.writer import DatabaseWriter\n"
        "\n"
        "def create(uow: UnitOfWork) -> None:\n"
        "    uow.execute(\"INSERT INTO project_references (id) VALUES (?)\", ('r1',))\n"
        "\n"
        "def show(uow: UnitOfWork) -> None:\n"
        "    conn = uow.connection\n"
        "    conn.execute(\"SELECT id FROM project_references\")\n",
    )
    errors = lint_writer_authority(tmp_path)
    assert errors == [], errors


# ---------------------------------------------------------------------------
# Legacy authorities
# ---------------------------------------------------------------------------


def test_legacy_authority_in_supported_entry_path_fails(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/__init__.py",
        "from astrid.core.timeline.eventlog import LocalFsBackend\n"
        "backend = LocalFsBackend(timeline_id='x', timeline_home=Path('.'))\n",
    )
    errors = lint_legacy_authorities(tmp_path)
    assert any(
        "astrid/packs/__init__.py: legacy authority marker "
        "'LocalFsBackend' in a supported v10 entry path" in error
        for error in errors
    ), errors


def test_legacy_files_outside_supported_paths_are_allowed(tmp_path: Path) -> None:
    """The m1-m6 legacy files stay in-tree; only the supported v10 entry
    paths are scanned for legacy authority markers."""
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/core/timeline/legacy_thing.py",
        "from astrid.core.timeline.eventlog import LocalFsBackend\n"
        "from astrid.core.timeline.eventlog.backends.remote_client import client\n",
    )
    errors = lint_legacy_authorities(tmp_path)
    assert errors == [], errors


# ---------------------------------------------------------------------------
# Removed authorities
# ---------------------------------------------------------------------------


def test_removed_authority_import_in_product_path_fails(tmp_path: Path) -> None:
    """A product path (SDK module) importing a removed authority fails."""
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/sdk/evil.py",
        "from astrid.core.session import lease\n"
        "token = lease.LeaseError\n",
    )
    errors = lint_removed_authorities(tmp_path)
    assert any(
        "astrid/sdk/evil.py: removed-authority import 'astrid.core.session' "
        "from a product path" in error
        for error in errors
    ), errors


def test_removed_authority_import_in_non_product_path_stays_legal(
    tmp_path: Path,
) -> None:
    """The same removed-authority import outside a product path is legal:
    legacy dead code may stay in-tree as long as no product path imports it."""
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/core/timeline/legacy_thing.py",
        "from astrid.core.session import lease\n"
        "token = lease.LeaseError\n",
    )
    errors = lint_removed_authorities(tmp_path)
    assert errors == [], errors


def test_removed_authority_import_in_dispatch_route_fails(tmp_path: Path) -> None:
    """The eight-family dispatch routes are product paths: a dispatch-route
    import of a deleted CLI module fails."""
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/core/gateway/dispatch.py",
        "from astrid.core.cli.timeline import main\n",
    )
    errors = lint_removed_authorities(tmp_path)
    assert any(
        "astrid/core/gateway/dispatch.py: removed-authority import "
        "'astrid.core.cli.timeline' from a product path" in error
        for error in errors
    ), errors


def test_product_path_importing_kernel_currency_stays_clean(
    tmp_path: Path,
) -> None:
    """Negative control: a product path may import non-removed kernel
    currency (UoW, receipts); only removed authorities are flagged."""
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/sdk/good.py",
        "from astrid.core.store.uow import UnitOfWork\n"
        "from astrid.core.receipts import ReceiptService\n",
    )
    errors = lint_removed_authorities(tmp_path)
    assert errors == [], errors


def test_removed_authority_import_fails_the_whole_authority_lint(
    tmp_path: Path,
) -> None:
    """run_authority_lint registers lint_removed_authorities: a product-path
    removed-authority import is caught by the aggregate lint."""
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/sdk/evil.py",
        "from astrid.core.cli.project import main\n",
    )
    report = run_authority_lint(tmp_path)
    assert not report.ok
    assert any(
        "removed-authority import 'astrid.core.cli.project'" in error
        for error in report.errors
    ), report.errors


# ---------------------------------------------------------------------------
# Schema ownership
# ---------------------------------------------------------------------------


def test_kernel_fk_to_pack_table_fails(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/timeline/migrations/0002_bad.sql",
        "CREATE TABLE events (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  FOREIGN KEY (timeline_id) REFERENCES timelines (id)\n"
        ");\n",
    )
    errors = lint_schema_ownership(tmp_path)
    assert any(
        "astrid/packs/timeline/migrations/0002_bad.sql: kernel FK from "
        "events to pack table 'timelines'" in error
        for error in errors
    ), errors


def test_cross_pack_fk_fails(tmp_path: Path) -> None:
    _bootstrap(tmp_path, with_shots=True)
    _write(
        tmp_path,
        "astrid/packs/timeline/migrations/0002_bad.sql",
        "CREATE TABLE timelines (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  FOREIGN KEY (shot_id) REFERENCES shots (id)\n"
        ");\n",
    )
    errors = lint_schema_ownership(tmp_path)
    assert any(
        "astrid/packs/timeline/migrations/0002_bad.sql: cross-pack FK from "
        "timelines to 'shots' (pack 'shots')" in error
        for error in errors
    ), errors


def test_kernel_fk_to_new_pack_tables_fails(tmp_path: Path) -> None:
    """Kernel FKs may reference kernel tables only: a kernel table may never
    FK to the new pack-owned project_references or shots tables."""
    _bootstrap(tmp_path, with_shots=True)
    _write(
        tmp_path,
        "astrid/packs/timeline/migrations/0002_bad.sql",
        "CREATE TABLE events (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  FOREIGN KEY (reference_id) REFERENCES project_references (id),\n"
        "  FOREIGN KEY (shot_id) REFERENCES shots (id)\n"
        ");\n",
    )
    errors = lint_schema_ownership(tmp_path)
    assert any(
        "astrid/packs/timeline/migrations/0002_bad.sql: kernel FK from "
        "events to pack table 'project_references'" in error
        for error in errors
    ), errors
    assert any(
        "astrid/packs/timeline/migrations/0002_bad.sql: kernel FK from "
        "events to pack table 'shots'" in error
        for error in errors
    ), errors


def test_cross_pack_fk_between_new_packs_fails(tmp_path: Path) -> None:
    """The references and shots packs must never FK to each other: only
    inward (pack-to-kernel) FKs are legal, so references-to-shots and
    shots-to-references FKs are both violations."""
    _bootstrap(tmp_path, with_shots=True)
    _write(
        tmp_path,
        "astrid/packs/references/migrations/0002_bad.sql",
        "CREATE TABLE media_references (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  FOREIGN KEY (shot_id) REFERENCES shots (id)\n"
        ");\n",
    )
    _write(
        tmp_path,
        "astrid/packs/shots/migrations/0002_bad.sql",
        "CREATE TABLE shot_items (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  FOREIGN KEY (reference_id) REFERENCES project_references (id)\n"
        ");\n",
    )
    errors = lint_schema_ownership(tmp_path)
    assert any(
        "astrid/packs/references/migrations/0002_bad.sql: cross-pack FK from "
        "media_references to 'shots' (pack 'shots')" in error
        for error in errors
    ), errors
    assert any(
        "astrid/packs/shots/migrations/0002_bad.sql: cross-pack FK from "
        "shot_items to 'project_references' (pack 'references')" in error
        for error in errors
    ), errors


def test_pack_to_kernel_fks_are_allowed_kernel_currency(tmp_path: Path) -> None:
    """Negative control: the real references/shots migrations FK inward to
    kernel tables only (projects/media/tasks) and to their own pack tables;
    that is the allowed kernel currency and must never be flagged."""
    _bootstrap(tmp_path, with_shots=True)
    errors = lint_schema_ownership(tmp_path)
    assert errors == [], errors


def test_undeclared_table_fails(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/timeline/migrations/0002_bad.sql",
        "CREATE TABLE mystery_table (id TEXT PRIMARY KEY);\n",
    )
    errors = lint_schema_ownership(tmp_path)
    assert any(
        "astrid/packs/timeline/migrations/0002_bad.sql: undeclared table "
        "'mystery_table'" in error
        for error in errors
    ), errors


def test_forbidden_schema_table_fails(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/timeline/migrations/0002_bad.sql",
        "CREATE TABLE sessions (id TEXT PRIMARY KEY);\n",
    )
    errors = lint_schema_ownership(tmp_path)
    assert any(
        "astrid/packs/timeline/migrations/0002_bad.sql: forbidden table "
        "'sessions' violates the no-dormant-platform invariant" in error
        for error in errors
    ), errors


def test_undeclared_kernel_index_fails(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/timeline/migrations/0002_bad.sql",
        "CREATE TABLE projects (id TEXT PRIMARY KEY);\n"
        "CREATE INDEX rogue_idx ON projects (slug);\n",
    )
    errors = lint_schema_ownership(tmp_path)
    assert any(
        "astrid/packs/timeline/migrations/0002_bad.sql: undeclared kernel "
        "index 'rogue_idx'" in error
        for error in errors
    ), errors


def test_undeclared_stream_type_in_sql_fails(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/timeline/migrations/0002_bad.sql",
        "CREATE TABLE events (id TEXT PRIMARY KEY);\n"
        "INSERT INTO event_streams (id, stream_type) "
        "VALUES ('s1', 'core.nonexistent');\n",
    )
    errors = lint_schema_ownership(tmp_path)
    assert any(
        "astrid/packs/timeline/migrations/0002_bad.sql: stream type "
        "'core.nonexistent' is not declared by the composed registry"
        in error
        for error in errors
    ), errors


def test_pack_convenience_alias_default_columns_fail(tmp_path: Path) -> None:
    """Projected alias/default state may never become pack write columns
    (SD1): slug, ULID, default, and hash columns on a pack-owned table."""
    _bootstrap(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/timeline/migrations/0002_bad.sql",
        "CREATE TABLE timelines (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  slug TEXT,\n"
        "  timeline_ulid TEXT,\n"
        "  is_default INTEGER,\n"
        "  event_hash TEXT,\n"
        "  previous_event_hash TEXT\n"
        ");\n",
    )
    errors = lint_schema_ownership(tmp_path)
    for column in (
        "slug",
        "timeline_ulid",
        "is_default",
        "event_hash",
        "previous_event_hash",
    ):
        assert any(
            f"convenience column timelines.{column} projects alias/default "
            "state as write authority" in error
            for error in errors
        ), (column, errors)


# ---------------------------------------------------------------------------
# Whole-lint clean baseline and exemption proof
# ---------------------------------------------------------------------------


def test_clean_fixture_passes_the_whole_authority_lint(tmp_path: Path) -> None:
    """A fixture with only the declared schema pack and no mutations is ok."""
    _bootstrap(tmp_path)
    report = run_authority_lint(tmp_path)
    assert report.ok, report.errors


def test_mutation_fixture_fails_the_whole_authority_lint(
    tmp_path: Path,
) -> None:
    """One representative mutation per rule family, combined, is caught by
    the combined lint (import, writer, legacy, and schema errors together)."""
    _bootstrap(tmp_path, with_shots=True)
    _write(
        tmp_path,
        "astrid/core/evil.py",
        "from astrid.packs.timeline.repository import TimelineRepository\n",
    )
    _write(
        tmp_path,
        "astrid/core/evil_writer.py",
        "from astrid.core.store.writer import DatabaseWriter\n"
        "writer = DatabaseWriter('/tmp/x.sqlite3', None)\n",
    )
    _write(
        tmp_path,
        "astrid/packs/__init__.py",
        "from astrid.core.timeline.eventlog import LocalFsBackend\n",
    )
    _write(
        tmp_path,
        "astrid/packs/timeline/migrations/0002_bad.sql",
        "CREATE TABLE events (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  FOREIGN KEY (timeline_id) REFERENCES timelines (id)\n"
        ");\n"
        "CREATE TABLE sessions (id TEXT PRIMARY KEY);\n",
    )
    report = run_authority_lint(tmp_path)
    assert not report.ok
    joined = "\n".join(report.errors)
    assert "kernel-to-pack import" in joined
    assert "SQLite writer construction outside the kernel store" in joined
    assert "legacy authority marker 'LocalFsBackend'" in joined
    assert "kernel FK from events to pack table 'timelines'" in joined
    assert "forbidden table 'sessions'" in joined


def test_new_surface_mutations_fail_the_whole_authority_lint(
    tmp_path: Path,
) -> None:
    """One representative mutation per new m3 surface family, combined, is
    caught by the combined lint: kernel-to-references import, references-to-
    shots implementation import, a pack-owned writer, a kernel FK to
    project_references, and a references-to-shots cross-pack FK."""
    _bootstrap(tmp_path, with_shots=True)
    _write(
        tmp_path,
        "astrid/core/evil.py",
        "from astrid.packs.references.repository import ReferenceRepository\n",
    )
    _write(
        tmp_path,
        "astrid/packs/references/evil.py",
        "from astrid.packs.shots.repository import ShotRepository\n",
    )
    _write(
        tmp_path,
        "astrid/packs/references/repository.py",
        "from astrid.core.store.writer import DatabaseWriter\n"
        "writer = DatabaseWriter('/tmp/x.sqlite3', None)\n",
    )
    _write(
        tmp_path,
        "astrid/packs/references/migrations/0002_bad.sql",
        "CREATE TABLE media_references (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  FOREIGN KEY (shot_id) REFERENCES shots (id)\n"
        ");\n",
    )
    _write(
        tmp_path,
        "astrid/packs/timeline/migrations/0003_bad.sql",
        "CREATE TABLE events (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  FOREIGN KEY (reference_id) REFERENCES project_references (id)\n"
        ");\n",
    )
    report = run_authority_lint(tmp_path)
    assert not report.ok
    joined = "\n".join(report.errors)
    assert "kernel-to-pack import 'astrid.packs.references.repository'" in joined
    assert (
        "pack-to-pack import 'astrid.packs.shots.repository' from pack "
        "'references'" in joined
    )
    assert (
        "astrid/packs/references/repository.py: SQLite writer construction "
        "outside the kernel store" in joined
    )
    assert (
        "kernel FK from events to pack table 'project_references'" in joined
    )
    assert (
        "cross-pack FK from media_references to 'shots' (pack 'shots')"
        in joined
    )


def test_live_repository_tree_passes_authority_lint() -> None:
    """The committed tree is clean under the full authority lint (home of the live-tree gate)."""
    repo_root = Path(__file__).resolve().parents[2]

    report = run_authority_lint(repo_root)

    assert report.mode == "source"
    assert report.errors == ()
    assert report.ok
