"""Entrypoint help rendering for the Astrid gateway.

Extracted from ``astrid/gateway.py`` to keep the
gateway facade focused while preserving the help-printing entrypoint that
callers rely on via ``astrid.core.gateway._print_entrypoint_help``.

``_product_help_text`` / ``_print_product_help`` are the executable help. They document the nine-family core surface
plus discovered external pack routes: the
seven product families from the explicit registry
(``astrid/core/cli/domain_product.py``) with their kernel/pack ownership,
the two manifest-declared nested mounts, the ``--json`` envelope
convention, the stable exit codes, and the two operational families
(``doctor``, ``backup``).
"""

from __future__ import annotations


def _print_entrypoint_help() -> None:
    print(
        """Astrid command gateway — Python SDK + CLI

The canonical Python boundary is ``import astrid`` (see docs/reference/sdk.md).
This gateway is the CLI entry point for the seven product families,
workspace status, operational families (doctor, backup), and external tools.

Usage:
  python3 -m astrid <family> <command> [options]
  python3 -m astrid help

Product families:
  python3 -m astrid projects ...
  python3 -m astrid timelines ...
  python3 -m astrid media ...
  python3 -m astrid tasks ...
  python3 -m astrid runs ...

Timeline evidence:
  python3 -m astrid timelines visualize --project PROJECT [--timeline-slug REF]
      [--format FORMAT[,FORMAT...]] [--json]
      (paired rendered filmstrip; use --range/--at/--shot to focus)

Latest project render:
  python3 -m astrid runs open [RUN_ID] [--project PROJECT] [--json]

Operational families:
  python3 -m astrid status [--json]
  python3 -m astrid doctor [--json]
  python3 -m astrid backup {create,restore,export,tombstone,recover,purge} [--json]
  python3 -m astrid dev {status,promote,reclaim} [--ref REF] [--dry-run] [--json]
  python3 -m astrid auth {login,status,logout,revoke}

External tools:
  python3 -m astrid hivemind search QUERY [--limit N] [--json]

Nested mounts (manifest-owned):
  python3 -m astrid timelines shots ...
  python3 -m astrid media references ...

Options:
  --json      product commands print the five-key SDK envelope
              (ok/data/error/receipt/idempotency_key); doctor emits its
              diagnostic object; backup emits its runtime result object
  -h, --help  show help

Notes:
  python3 -m astrid is the package entry point.
  Use ``python3 -m astrid help`` for the full family census, kernel/pack
  ownership, nested mounts, and stable exit codes.

Ownership handoff:
  Product commands connect to the selected runtime through the generated
  client. Follow the typed next_action: it distinguishes a missing runtime
  install from a runtime that only needs configuration or startup.
"""
    )


def _product_help_text() -> str:
    """Return the executable help for the gateway surface.

    The text is generated from the explicit product registry plus the two
    operational families, so the advertised census can never drift from
    ``astrid/core/cli/domain_product.py``: the seven product families (with
    their kernel/pack ownership), the two manifest-declared nested mounts,
    the ``--json`` envelope convention, the stable exit codes, and the
    two operational families (``doctor``, ``backup``).
    """
    return """Astrid product commands — runtime families and external tools

The gateway owns seven product families, two operational families, the reserved
workspace status command, and the external Hivemind tool. ``shots`` mounts beneath ``timelines`` and
``references`` mounts beneath ``media``.

Usage:
  python3 -m astrid <family> <command> [options]
  python3 -m astrid <family> --help

Family census (exactly nine families): projects timelines media tasks runs preferences documents doctor backup

Reserved workspace commands (outside the family census): status

Product families:
  projects    [kernel] project create/list/show/update/select/current
  media       [kernel] media import/list/show/verify/relate
  tasks       [kernel] task create/list/show/cancel/retry/events
  runs        [kernel] run list/show/cancel/retry/events/open
  timelines   [pack: timeline] timelines list/show/replace-parent-media/archive/recover/history/diff/visualize/render

Operational families:
  status      [runtime] read-only workspace/Runtime/readiness status
  preferences [runtime] user/project Markdown defaults; checkout/checkin/edit
  documents   [runtime] reusable project documents; list/create/show/checkout/checkin
  doctor      [runtime] read-only runtime health diagnostics
  backup      [runtime] create/restore/export/tombstone/recover/purge
  dev         [operator] status/promote the served checkout; reclaim: report and expire registered disposable storage roots

Contributor authentication (reserved):
  auth        [hivemind] login/status/logout/revoke; contribution-only access

External tools:
  hivemind    [managed pack] search current messages and resources
  installed pack routes are discovered from pack manifests; packs without a
  declared CLI command remain available through ``astrid agent``

Nested mounts (manifest-owned):
  timelines shots       [pack: shots] project-level reusable shot list/create/show/add/remove/reorder
  media references      [pack: references] reference create/update/archive/associate/link/set-primary/list/show

Options:
  --json      product commands print the five-key SDK envelope
              (ok/data/error/receipt/idempotency_key); doctor emits its
              diagnostic object; backup emits its runtime result object
  -h, --help  show help

Exit codes:
  0  success (envelope ok=true)
  1  typed SDK error (envelope ok=false)
  2  usage/parse error

Ownership handoff:
  Product commands connect to the selected runtime through the generated
  client. Follow the typed next_action: it distinguishes a missing runtime
  install from a runtime that only needs configuration or startup.
"""


def _print_product_help() -> None:
    """Print the product-focused executable help to stdout."""
    print(_product_help_text(), end="")
