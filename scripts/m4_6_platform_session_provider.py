"""Fixed direct-native M4-6 checkpoint session provider.

The export in this module is selected by ``native_service_adapter``; callers
cannot select another provider or pass an authority token.  It deliberately
does not authenticate a GitHub run or publish a verdict.  Its limited job is
to retain one exact live owner and immutable binding from open through the
product-authorized removal checkpoint, and to refuse every substitution.
"""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path

from . import native_service_adapter as native


class TrustedPlatformContextUnavailable(RuntimeError):
    """Raised when the source-fixed direct checkpoint context is not exact."""


_DIRECT_SLOTS = frozenset({"direct-macos", "direct-linux", "direct-windows"})
_REQUIRED_ARTIFACT_ROLES = frozenset(
    {"product-driver", "service-definition", "service-executable"}
)


def _same(left: object, right: object) -> bool:
    try:
        return native._canonical(left) == native._canonical(right)
    except (TypeError, ValueError):
        return False


def _canonical_file(path_text: object, digest: object, *, label: str) -> Path:
    if not isinstance(path_text, str) or not native._digest(digest):
        raise TrustedPlatformContextUnavailable(f"{label} binding is incomplete")
    path = Path(path_text)
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.resolve(strict=True) != path.absolute()
        or native._sha256_file(path) != digest
    ):
        raise TrustedPlatformContextUnavailable(f"{label} changed, is unsafe, or is absent")
    return path


class DirectCheckpointPlatformSession:
    """One-shot direct checkpoint authority with no serializable success state."""

    def __init__(self, binding: native.PlatformSessionBinding, owner: native.NativeOwner) -> None:
        self._binding = binding
        self._owner = owner
        self._owner_identity = id(owner)
        self._binding_sha256 = native._sha256_bytes(native._canonical(asdict(binding)))
        self._state = "opened"
        self._custody: native.CustodyBinding | None = None
        self._request: native.RemovalRequest | None = None

    def _require_state(self, expected: str) -> None:
        if self._state != expected:
            raise TrustedPlatformContextUnavailable(
                f"direct checkpoint session is {self._state}, not {expected}"
            )

    def _same_owner(self, owner: object) -> native.NativeOwner:
        if owner is not self._owner or id(owner) != self._owner_identity:
            raise TrustedPlatformContextUnavailable("direct checkpoint owner is foreign")
        return self._owner

    def _artifacts(self, *, final: bool = False) -> dict[str, native.PlatformArtifactBinding]:
        rows = self._binding.artifacts
        if not rows or any(type(row) is not native.PlatformArtifactBinding for row in rows):
            raise TrustedPlatformContextUnavailable("immutable artifact binding is malformed")
        by_role = {row.role: row for row in rows}
        if len(by_role) != len(rows) or not _REQUIRED_ARTIFACT_ROLES.issubset(by_role):
            raise TrustedPlatformContextUnavailable("required direct immutable artifact roles are absent")
        if len({row.path for row in rows}) != len(rows):
            raise TrustedPlatformContextUnavailable("immutable artifact paths must be unique")
        plan = self._custody.removal_plan if self._custody is not None else None
        remove_paths = tuple(Path(value) for value in plan.remove_paths) if plan else ()
        for role, row in by_role.items():
            path = Path(row.path)
            removal_owned = any(path == root or path.is_relative_to(root) for root in remove_paths)
            if final and removal_owned:
                if path.exists() or path.is_symlink():
                    raise TrustedPlatformContextUnavailable(
                        f"removed immutable artifact {role} remains after product uninstall"
                    )
                continue
            _canonical_file(row.path, row.sha256, label=f"immutable artifact {role}")
        command = by_role["product-driver"]
        if not self._binding.product_command or self._binding.product_command[0] != command.path:
            raise TrustedPlatformContextUnavailable("product command differs from its immutable artifact")
        return by_role

    def _validate_binding(self) -> None:
        binding = self._binding
        if type(binding) is not native.PlatformSessionBinding:
            raise TrustedPlatformContextUnavailable("exact platform binding type is required")
        contract = native.failure.SLOT_CONTRACTS.get(binding.slot_id)
        if (
            binding.slot_id not in _DIRECT_SLOTS
            or contract is None
            or binding.mode != "direct"
            or binding.owner_kind != "native-service"
            or binding.os_name != contract["os"]
            or binding.os_name != native._platform_name()
            or binding.runner_image != contract["runner"]
            or binding.shell != contract["shell"]
        ):
            raise TrustedPlatformContextUnavailable("direct slot/platform binding differs")
        owner = self._owner
        if (
            type(owner) is not native.NativeOwner
            or type(owner.runner) is not native.SubprocessCommandRunner
            or type(owner.process_probe) is not native.LiveProcessProbe
            or type(owner.data_probe) is not native.DataProbe
            or type(owner.durable_journal_probe) is not native.DurableJournalProbe
            or owner.os_name != binding.os_name
            or owner.manager != native.MANAGERS[binding.os_name]
            or owner.principal != binding.owner_principal
            or owner.target != binding.owner_target
            or not native._nonempty(owner.principal)
            or not native._nonempty(owner.target)
        ):
            raise TrustedPlatformContextUnavailable("fixed live direct collector binding differs")
        expected_target = {
            "linux": native.SYSTEMD_UNIT,
            "windows": native.WINDOWS_TASK_PATH + native.WINDOWS_TASK_NAME,
        }.get(binding.os_name)
        if binding.os_name == "macos":
            if not binding.owner_target.endswith("/" + native.SERVICE_LABEL):
                raise TrustedPlatformContextUnavailable("canonical launchd target differs")
        elif binding.owner_target != expected_target:
            raise TrustedPlatformContextUnavailable("canonical native target differs")
        candidate = dict(binding.candidate)
        if len(candidate) != len(binding.candidate) or native.failure.candidate_errors(candidate):
            raise TrustedPlatformContextUnavailable("full frozen candidate tuple is required")
        checkouts = {name: (sha, path) for name, sha, path in binding.checkouts}
        if len(checkouts) != len(binding.checkouts) or set(checkouts) != {
            "app", "agent", "runtime", "pinokio"
        }:
            raise TrustedPlatformContextUnavailable("full frozen checkout binding is required")
        for name, (sha, path_text) in checkouts.items():
            path = Path(path_text)
            if (
                sha != candidate[name]
                or not path.is_absolute()
                or path.is_symlink()
                or not path.is_dir()
                or path.resolve(strict=True) != path.absolute()
            ):
                raise TrustedPlatformContextUnavailable("checkout differs from the frozen candidate")
        executable = Path(binding.collector_executable)
        if (
            binding.collector_pid != os.getpid()
            or executable != Path(os.sys.executable).resolve(strict=True)
        ):
            raise TrustedPlatformContextUnavailable("collector process identity differs")
        _canonical_file(
            binding.collector_executable,
            binding.collector_executable_sha256,
            label="collector executable",
        )
        if (
            binding.product_protocol != "astrid-m4-6-product-driver/v1"
            or not binding.product_command
            or not Path(binding.product_command_cwd).is_absolute()
            or not Path(binding.product_command_cwd).is_dir()
            or Path(binding.product_command_cwd).is_symlink()
        ):
            raise TrustedPlatformContextUnavailable("fixed product protocol/command binding differs")
        self._artifacts()

    def _validate_custody(self, custody: native.CustodyBinding) -> None:
        if type(custody) is not native.CustodyBinding:
            raise TrustedPlatformContextUnavailable("exact custody binding type is required")
        if (
            custody.slot_id != self._binding.slot_id
            or custody.owner_kind != self._binding.owner_kind
            or custody.principal != self._binding.owner_principal
            or custody.target != self._binding.owner_target
            or custody.removal_plan is None
            or custody.removal_plan.operation != "product-uninstall"
            or custody.removal_plan.keep_data is not True
        ):
            raise TrustedPlatformContextUnavailable("custody binding differs from the opened session")
        artifacts = self._artifacts()
        definition = artifacts["service-definition"]
        action = artifacts["service-executable"]
        if (
            custody.definition_path != definition.path
            or custody.definition_sha256 != definition.sha256
            or not custody.action
            or custody.action[0] != action.path
            or custody.action_sha256 != native._sha256_bytes(native._canonical(list(custody.action)))
            or custody.resolved_action_identity_sha256
            != native._sha256_bytes(native._canonical(list(custody.action)))
            or set(custody.immutable_actions) != {definition, action}
        ):
            raise TrustedPlatformContextUnavailable("custody definition/action binding differs")
        if custody.definition_path not in custody.removal_plan.remove_paths:
            raise TrustedPlatformContextUnavailable("removal plan omits the service definition")

    def bind_exact_custody(self, binding: native.CustodyBinding, owner: object) -> None:
        self._require_state("opened")
        self._same_owner(owner)
        self._validate_binding()
        self._validate_custody(binding)
        self._custody = binding
        self._state = "custody-bound"

    def _validate_request(self, request: native.RemovalRequest) -> None:
        if type(request) is not native.RemovalRequest or self._custody is None:
            raise TrustedPlatformContextUnavailable("exact removal request is required")
        if request.session != self._binding or request.custody != self._custody:
            raise TrustedPlatformContextUnavailable("removal request is bound to a different session")
        plan = self._custody.removal_plan
        assert plan is not None
        if (
            request.operation != plan.operation
            or request.keep_data is not True
            or request.remove_paths != plan.remove_paths
            or request.preserve_paths != plan.preserve_paths
            or request.expected_postconditions != plan.expected_postconditions
        ):
            raise TrustedPlatformContextUnavailable("exact product keep-data removal plan differs")
        self._artifacts()
        registration = self._owner.registration()
        if request.registration_sha256 != native._sha256_bytes(native._canonical(registration)):
            raise TrustedPlatformContextUnavailable("current native registration differs from removal request")
        census = request.live_census
        if (
            type(census) is not native.RemovalCensus
            or census.manager_target != self._binding.owner_target
            or dict(census.registration) != registration
            or dict(census.controller_identity).get("principal") != self._binding.owner_principal
            or dict(census.controller_identity).get("target") != self._binding.owner_target
        ):
            raise TrustedPlatformContextUnavailable("fresh removal census differs from the live owner")
        processes = (*census.processes, *census.prior_incarnations)
        identities = tuple(sorted((row.pid, row.process_birth_id) for row in processes))
        actions = tuple(sorted(row.action for row in processes))
        if (
            not processes
            or tuple(sorted(request.observed_incarnations)) != identities
            or tuple(sorted(request.observed_actions)) != actions
        ):
            raise TrustedPlatformContextUnavailable("removal census omits or changes an observed incarnation")
        data = dict(request.data_identity)
        if len(data) != len(request.data_identity) or native._data_identity(data) != data:
            raise TrustedPlatformContextUnavailable("exact preserved-data identity is required")
        roots = tuple(Path(value) for value in plan.preserve_paths)
        if not all(
            any(Path(str(data[name])) == root or Path(str(data[name])).is_relative_to(root) for root in roots)
            for name in ("data_root", "data_manifest_path")
        ):
            raise TrustedPlatformContextUnavailable("removal plan does not preserve the observed data identity")

    def authorize_exact_removal(self, request: native.RemovalRequest, owner: object) -> None:
        self._require_state("custody-bound")
        self._same_owner(owner)
        self._validate_request(request)
        self._request = request
        self._state = "removal-authorized"

    def finalize_after_removal(
        self, observation: native.FinalRemovalObservation, owner: object
    ) -> None:
        self._require_state("removal-authorized")
        self._same_owner(owner)
        if type(observation) is not native.FinalRemovalObservation or self._request is None:
            raise TrustedPlatformContextUnavailable("exact final removal observation is required")
        if (
            observation.slot_id != self._binding.slot_id
            or observation.owner_kind != self._binding.owner_kind
        ):
            raise TrustedPlatformContextUnavailable("final observation slot/owner differs")
        self._artifacts(final=True)
        authorized = {
            "observed_actions": [list(row) for row in self._request.observed_actions],
            "observed_incarnations": [
                {"pid": pid, "process_birth_id": birth}
                for pid, birth in self._request.observed_incarnations
            ],
            "data_identity": dict(self._request.data_identity),
        }
        fresh = self._owner.observe_absent(authorized)
        if not _same(tuple(sorted(fresh.items())), observation.absence):
            raise TrustedPlatformContextUnavailable("independent final census differs from removal observation")
        if (
            fresh.get("manager_target_absent") is not True
            or fresh.get("all_authorized_incarnations_absent") is not True
            or fresh.get("owned_processes_after") != 0
            or fresh.get("data_identity_unchanged") is not True
            or fresh.get("post_data_identity") != dict(self._request.data_identity)
        ):
            raise TrustedPlatformContextUnavailable("final removal census is incomplete")
        capture = getattr(self._owner, "capture_final_checkpoint_census", None)
        if not callable(capture):
            raise TrustedPlatformContextUnavailable("fixed owner cannot record the final checkpoint census")
        capture(fresh)
        self._state = "finalized"

    def abort(self, reason: str) -> None:
        if self._state in {"finalized", "aborted"}:
            raise TrustedPlatformContextUnavailable("direct checkpoint session is already closed")
        if not isinstance(reason, str) or not reason:
            raise TrustedPlatformContextUnavailable("checkpoint abort reason is required")
        self._state = "aborted"


class _DirectCheckpointProvider:
    """The only source-fixed direct provider; no caller configuration exists."""

    def open_authenticated_session(
        self, binding: native.PlatformSessionBinding, owner: object
    ) -> DirectCheckpointPlatformSession:
        if type(owner) is not native.NativeOwner:
            raise TrustedPlatformContextUnavailable("exact live NativeOwner is required")
        session = DirectCheckpointPlatformSession(binding, owner)
        session._validate_binding()
        # Independent source read after the adapter's initial census and before
        # private scratch creation or any native/product mutation.
        owner.assert_absent()
        return session


PLATFORM_SESSION_PROVIDER = _DirectCheckpointProvider()
