"""Concrete Runtime and tool operations used by the install transaction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
import webbrowser
from dataclasses import replace
from pathlib import Path

from runtime_protocol import platform
from runtime_protocol.door import DEFAULT_DOOR_PORT

from ..core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION
from .core import (
    BackupSpaceError,
    RuntimeOperationError,
    VerificationError,
    Version,
    absolute,
    current,
    install_identity,
    install_release,
    linked,
    new_version_id,
    persist_install_identity,
    read_json,
    write_json,
)
from .launcher import install_shim, owned_shim, remove_shim
from .service import LABEL, Service, bootstrap_root, platform_target, venv_python
from .support import reveal_bundle
from .tools import binary, install_tools, source


def _last_logged_error(log: Path, *, tail: int = 65536) -> str | None:
    """The last ``error`` field the Runtime wrote to its service log, if any."""
    try:
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - tail))
            text = stream.read().decode("utf-8", "replace")
    except OSError:
        return None
    decoder = json.JSONDecoder()
    found = None
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict) and isinstance(value.get("error"), str) and value["error"]:
            found = value["error"]
        index = text.find("{", end)
    return found


def _last_service_log(log: Path, *, tail: int = 8192) -> str | None:
    try:
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - tail))
            value = stream.read().decode("utf-8", "replace").strip()
        return value or None
    except OSError:
        return None


def _classify_services(services, ready_file: Path) -> tuple[str, dict | None, dict | None]:
    """Classify one health snapshot as ready, crashed, or still starting."""
    if not isinstance(services, list):
        services = []
    pack_host = next((service for service in services
                      if isinstance(service, dict) and service.get("name") == "pack-host"), None)
    failed = next((service for service in services
                   if isinstance(service, dict) and service.get("state") == "failed"), None)
    if failed is not None:
        return "crashed", pack_host, failed
    if pack_host and pack_host.get("state") == "running" and ready_file.is_file():
        return "ready", pack_host, None
    if pack_host and (pack_host.get("restart_count", 0) > 0 or pack_host.get("last_exit") is not None):
        return "crashed", pack_host, None
    return "starting", pack_host, None


def _runtime_json(output: str) -> dict:
    """Runtime's one JSON result, after any stderr notes merged into output."""
    start = next((match.start() for match in re.finditer(r"^\{", output, re.MULTILINE)), 0)
    return json.loads(output[start:])


class Backend:
    def __init__(self, root: Path, log: Path, uv: Path, *, say=print,
                 process_control: str | None = None, managed_by: str | None = None):
        self.root, self.log, self.uv, self.say = root, log, uv, say
        self.managed_by = managed_by
        # None keeps the workspace's current setting (strict on a new one).
        self.process_control = process_control
        if process_control:
            from runtime_protocol import platform

            platform.configure_process_control(process_control)  # this process signals too
        self.service = Service(root, log, self.run, say=say)

    def run(self, argv, *, extra_env=None, cwd=None, timeout=1800, clean_bun_env=False) -> str:
        # Never inherit a developer's Python import path or Runtime connection.
        env = {k: v for k, v in os.environ.items() if k not in {
            "PYTHONPATH", "PYTHONHOME", "ASTRID_WORKSPACE_ENDPOINT",
            "ASTRID_WORKSPACE_CREDENTIAL", "ASTRID_RUNTIME_SOURCE_MANIFEST",
            "ASTRID_RUNTIME_DATA_ROOT", "ASTRID_PACKS_PATH", "ASTRID_INSTALLER_OPERATION_ID"}
               and not (clean_bun_env and (k.startswith("BUN_") or k == "NODE_OPTIONS"))}
        env.update({"PYTHONIOENCODING": "utf-8"})
        env.update(extra_env or {})
        result = subprocess.run([str(s) for s in argv], cwd=cwd, env=env, check=False,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace", timeout=timeout)
        with self.log.open("a", encoding="utf-8") as stream:
            platform.owner_only(stream.fileno())
            # Do not log inherited environments or credentials.
            stream.write(result.stdout + "\n")
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, [str(argv[0])], output=result.stdout)
        return result.stdout.strip()

    def install_remotion_dependencies(self, agent: Path, env: dict[str, str], npm: Path) -> bool:
        """Install the renderer's frozen dependencies, reusing an identical lock."""
        remotion = agent / "remotion"
        lock_hash = hashlib.sha256((remotion / "package-lock.json").read_bytes()).hexdigest()
        modules = remotion / "node_modules"
        marker = modules / ".astrid-package-lock.sha256"
        if not (modules.is_dir() and marker.is_file()
                and marker.read_text(encoding="ascii").strip() == lock_hash):
            # Node's official archives expose npm through this JavaScript entrypoint
            # on every OS; neither shell shims nor an ambient npm are required.
            try:
                self.run([env["ASTRID_NODE_EXECUTABLE"], npm, "ci", "--no-audit", "--no-fund"],
                         cwd=remotion, extra_env=env)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                self.say(f"Rendering unavailable: Remotion npm ci failed ({self._command_failure(exc)}). Check the installer log and retry the install.")
                return False
            marker.write_text(lock_hash + "\n", encoding="ascii")
        # Renders never download: fetch Remotion's browser now, once per Remotion
        # version into the shared cache (a no-op when it is already there).
        try:
            self.run([env["ASTRID_NODE_EXECUTABLE"], remotion / "scripts" / "fetch-shared-browser.mjs"],
                     cwd=remotion, extra_env=env)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            self.say(f"Rendering unavailable: Remotion browser setup failed ({self._command_failure(exc)}). Check network access and retry the install.")
        return True

    @staticmethod
    def _command_failure(exc: Exception) -> str:
        if isinstance(exc, subprocess.CalledProcessError):
            return f"exit code {exc.returncode}"
        if isinstance(exc, subprocess.TimeoutExpired):
            return "timed out"
        return str(exc)

    def runtime(self, version: Version | None, data: Path, *args, profile=False, timeout=1800) -> dict:
        python = version.python if version else Path(sys.executable)
        argv = [python, "-X", "utf8", "-I", "-m", "astrid_runtime", *args, "--data-root", str(data), "--json"]
        if profile and version:
            argv += ["--source-manifest", str(version.profile)]
        launch_env = dict(version.env) if version else {}
        launch_env.pop("ASTRID_INSTALLER_OPERATION_ID", None)
        try:
            output = self.run(argv, extra_env=launch_env or None, timeout=timeout)
        except subprocess.CalledProcessError as exc:
            # Runtime deliberately returns nonzero for a structured refusal.
            # Retain that result instead of losing it in subprocess's summary.
            try:
                result = _runtime_json(exc.output or "")
            except (TypeError, ValueError):
                raise RuntimeOperationError("Runtime command failed without a valid result.") from exc
            self._raise_runtime_error(result)
            raise RuntimeOperationError("Runtime command exited unsuccessfully.") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeOperationError(f"Runtime command failed: {self._command_failure(exc)}") from exc
        try:
            result = _runtime_json(output)
        except (TypeError, ValueError) as exc:
            raise RuntimeOperationError("Runtime command returned an invalid result.") from exc
        self._raise_runtime_error(result)
        return result

    @staticmethod
    def _raise_runtime_error(result: dict) -> None:
        if not isinstance(result, dict):
            raise RuntimeOperationError("Runtime command returned an invalid result.")
        if result.get("ok") is False:
            error = result.get("error")
            if isinstance(error, dict):
                details = error.get("details")
                raise RuntimeOperationError(str(error.get("message") or "Runtime command failed."),
                                            details=details)
            raise RuntimeOperationError(str(error or "Runtime command failed."))

    def existing(self, data: Path) -> Version | None:
        record = self.service.recorded()
        if record.get("uninstalled"):
            return None
        version = current(self.root)
        if version is not None:
            return version if version.python.is_file() else None
        profile = data / "runtime" / "source-profiles" / "astrid.json"
        value = read_json(profile)
        if not value:
            return None
        environment = absolute(value["runtime_environment"])
        python = venv_python(environment)
        if not python.is_file():
            return None  # The bootstrap Runtime can inspect a retained workspace.
        return Version(environment.parent, python, profile,
                       {"PATH": str(python.parent) + os.pathsep + os.defpath},
                       managed_by=self.managed_by)

    def has_workspace(self, data: Path) -> bool:
        # A nonempty selected root is never mistaken for a fresh installation.
        return data.exists() and any(p.name != "logs" for p in data.iterdir())

    def reinstallable(self, data: Path) -> bool:
        record = self.service.recorded()
        return (record.get("owner") == "haus.astrid.runtime"
                and record.get("data_root") == str(data)
                and (data / "runtime" / "catalog.json").is_file())

    @staticmethod
    def _prior_chat_settings(old: Version | None, retained: dict | None = None) -> tuple[Path | None, dict[str, str]]:
        """Carry the selected chat store and supported options across updates."""
        values = retained or {}
        if old is not None:
            profile = read_json(old.profile)
            declaration_path = profile.get("service_declaration")
            if isinstance(declaration_path, str) and Path(declaration_path).is_absolute():
                declaration = read_json(Path(declaration_path))
                chat = next((row for row in declaration.get("services", [])
                             if isinstance(row, dict) and row.get("name") == "chat"), None)
                values = chat.get("env", {}) if isinstance(chat, dict) else values
        if not isinstance(values, dict):
            return None, {}
        cwd = values.get("ASTRID_ACP_CWD")
        chat_cwd = Path(cwd) if isinstance(cwd, str) and Path(cwd).is_absolute() else None
        option_names = {
            "ASTRID_ACP_PROFILE", "ASTRID_ACP_SESSION_DIR", "ASTRID_ACP_SYSTEM_PROMPT_FILE",
            "ASTRID_ACP_COMMAND",
        }
        options = {key: value for key, value in values.items()
                   if key in option_names and isinstance(value, str) and value}
        return chat_cwd, options

    def _selected_chat_settings(self, old: Version | None, data: Path) -> tuple[Path, dict[str, str]]:
        record = self.service.recorded()
        retained = record.get("chat_settings") if record.get("data_root") == str(data) else None
        chat_cwd, chat_options = self._prior_chat_settings(old, retained)
        if chat_cwd is None:
            chat_cwd = Path(os.environ.get("ASTRID_ACP_CWD") or data / "chat")
        return chat_cwd, chat_options

    def _declare_services(self, *, folder: Path, agent: Path, data: Path, python: Path,
                          bun: str, env: dict[str, str], old: Version | None) -> Path:
        """Run the candidate package's declaration API and write its profile input."""
        chat_cwd, chat_options = self._selected_chat_settings(old, data)
        services_path = folder / "services.json"
        script = (
            "import json,sys; from pathlib import Path; "
            "from astrid.services import service_declaration; "
            "value=service_declaration(source_checkout=Path(sys.argv[1]), "
            "data_root=Path(sys.argv[2]), python=sys.argv[3], bun=sys.argv[4], "
            "chat_cwd=Path(sys.argv[5]) if sys.argv[5] else None); "
            "Path(sys.argv[6]).write_text(json.dumps(value, indent=2, sort_keys=True)+'\\n', encoding='utf-8')"
        )
        self.run(
            [python, "-X", "utf8", "-I", "-c", script, str(agent), str(data), str(python), bun,
             str(chat_cwd) if chat_cwd else "", str(services_path)],
            extra_env={**env, **chat_options, "ASTRID_INSTALL_PROGRAM_ROOT": str(self.root)},
        )
        declaration = read_json(services_path)
        for service in declaration.get("services", []):
            if isinstance(service, dict) and service.get("name") == "chat":
                service.setdefault("env", {})["ASTRID_INSTALL_PROGRAM_ROOT"] = str(self.root)
        write_json(services_path, declaration)
        return services_path

    def _installed_product_root(self, python: Path, env: dict[str, str]) -> Path:
        """Return the selected venv's installed Astrid product tree.

        The pinned source checkout is retained for provenance and non-wheel
        assets. Execution identity must instead bind to the noneditable files
        imported by the candidate interpreter.
        """
        probe = """import importlib.util,json,sys,sysconfig
modules={'host':'astrid.core.execution.generic_host','child':'astrid.core.execution.generic_host_worker','action':'astrid.packs.rendering.actions.render.run','client':'astrid_runtime_client','sdk':'astrid.sdk.invocation'}
print(json.dumps({'prefix':sys.prefix,'purelib':sysconfig.get_path('purelib'),'origins':{key:importlib.util.find_spec(module).origin for key,module in modules.items()}}))
"""
        try:
            value = json.loads(self.run(
                [python, "-X", "utf8", "-I", "-B", "-c", probe], extra_env=env,
            ))
            prefix_raw = Path(value["prefix"])
            product_raw = Path(value["purelib"])
            if not prefix_raw.is_absolute() or not product_raw.is_absolute():
                raise ValueError("selected interpreter paths must be absolute")
            prefix = prefix_raw.resolve(strict=True)
            product_root = product_raw.resolve(strict=True)
            selected_venv = python.parent.parent.resolve(strict=True)
            origins = value["origins"]
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError,
                subprocess.CalledProcessError) as exc:
            raise RuntimeError("The selected Python did not report an installed Astrid product tree.") from exc
        if (prefix != selected_venv or not product_root.is_relative_to(prefix)
                or not isinstance(origins, dict) or set(origins) != {
            "host", "child", "action", "client", "sdk",
        }):
            raise RuntimeError("The selected Python's product tree is outside its selected virtual environment.")
        for origin in origins.values():
            if not isinstance(origin, str):
                raise RuntimeError("The selected Python did not resolve every installed Astrid module.")
            path = Path(origin)
            if (not path.is_absolute() or not path.is_file()
                    or not path.resolve(strict=True).is_relative_to(product_root)
                    or any(part.is_symlink() for part in (path, *path.parents))):
                raise RuntimeError("The selected Python resolves Astrid code outside its installed product tree.")
        return product_root

    def workspace(self, version: Version | None, data: Path) -> dict:
        interpreter = version if version is not None and version.python.is_file() else None
        value = self.runtime(interpreter, data, "workspace", "inspect")
        if not value.get("realm_id") or not value.get("realm_root"):
            raise ValueError("The selected workspace has no unambiguous identity.")
        absolute(value["realm_root"])
        return value

    @staticmethod
    def _discovery_owner_state(discovery: dict) -> str:
        """Classify one discovery snapshot without signaling or editing it."""
        if not isinstance(discovery, dict):
            raise RuntimeError("Runtime discovery owner identity is incomplete; refusing to change it.")
        pid = discovery.get("pid")
        birth = discovery.get("process_birth_id")
        if type(pid) is not int or pid <= 0 or not isinstance(birth, str) or not birth.strip():
            raise RuntimeError("Runtime discovery owner identity is incomplete; refusing to change it.")
        try:
            identity = platform.process_identity(pid)
        except OSError as exc:
            raise RuntimeError("Runtime discovery owner identity could not be observed; refusing to change it.") from exc
        if identity is None:
            return "stale"  # Proven absent; Runtime startup owns reconciliation.
        if identity.get("pid") != pid or identity.get("birth_id") != birth:
            raise RuntimeError("Runtime discovery owner identity changed; refusing to change it.")
        return "live"

    def source_checkout_ownership(self, data: Path, installed: Version | None = None) -> bool:
        """Resolve whether the selected workspace belongs to source checkouts.

        A live owner's captured identity is authoritative. When stopped, the
        retained Runtime source profile is admitted through Runtime's normal
        validator and its two paths must be Git checkout roots. A managed
        profile names the venv's installed product tree (``source_checkout``)
        and an unpacked Runtime archive (``runtime_checkout``); neither is a
        Git checkout root, so it cannot pass that check.
        """
        from astrid_runtime.bootstrap import SourceProfile
        from astrid_runtime.identity import validate_live_owner_identity
        from astrid_runtime.paths import RuntimePaths
        from astrid_runtime.runtime_boundary import LocalRuntimeBoundary
        from runtime_protocol.source_identity import validate_checkout_root

        paths = RuntimePaths.current(data_root=data)
        try:
            discovery_bytes = paths.discovery_path.read_bytes()
            discovery = json.loads(discovery_bytes)
        except FileNotFoundError:
            discovery = None
            state = "stopped"
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Runtime discovery owner identity cannot be read; refusing to change it.") from exc
        else:
            state = self._discovery_owner_state(discovery)
        live_identity = None
        if state == "live":
            try:
                instance_id = discovery.get("runtime_instance_id")
                endpoint = discovery.get("endpoint")
                if not isinstance(instance_id, str) or not instance_id or not isinstance(endpoint, str) or not endpoint:
                    raise ValueError("Runtime discovery owner identity is incomplete; refusing to change it.")
                boundary = LocalRuntimeBoundary()
                health = boundary.owner_startup_identity(endpoint=endpoint, instance_id=instance_id)
                live_identity = validate_live_owner_identity(health, instance_id=instance_id)
                # Bind the OS and HTTP observations to the same advertisement.
                if paths.discovery_path.read_bytes() != discovery_bytes:
                    raise ValueError("Runtime discovery owner identity changed; refusing to change it.")
            except Exception as exc:
                raise RuntimeError("Live Runtime startup identity cannot be verified; refusing to change it.") from exc
            if live_identity.get("managed_by") == "source":
                if installed is not None:
                    raise RuntimeError("Live Runtime source identity conflicts with the selected managed version; refusing to change it.")
                return True

        # A selected managed version is explicit evidence that this workspace
        # is an installed release. Its profile names the venv's installed
        # product tree (source_checkout) and an unpacked Runtime archive
        # (runtime_checkout); neither is a Git checkout root.
        if installed is not None:
            return False

        profile_path = paths.source_profiles_dir / "astrid.json"
        if not os.path.lexists(profile_path):
            if state == "stale" and installed is None:
                raise RuntimeError("Stale Runtime discovery has no retained configuration to verify; refusing to change it.")
            return False
        try:
            profile = SourceProfile.load(profile_path, expected_profile="astrid")
            for raw in (profile.runtime_checkout, profile.source_checkout):
                validate_checkout_root(absolute(raw))
        except Exception as exc:
            raise ValueError(f"Retained Runtime source configuration cannot be verified: {exc}") from exc
        if live_identity is not None:
            raise RuntimeError("Live Runtime identity conflicts with retained source checkout configuration; refusing to change it.")
        return True

    @staticmethod
    def _stop_failure(exc: Exception) -> RuntimeOperationError:
        details = getattr(exc, "details", None)
        return RuntimeOperationError(str(exc), details=details)

    @staticmethod
    def _downstream_stop_failure(exc: Exception) -> RuntimeOperationError:
        """Keep post-C1 failures as context, never as a lifecycle refusal."""
        details = getattr(exc, "details", None)
        return RuntimeOperationError(str(exc), details={"cause_details": details})

    def _pending_restore_selection(self, data: Path):
        """Supply Runtime's narrow context for replaying an interrupted restore."""
        journal_path = self.root / "update-transaction.json"
        active = getattr(self, "_update_transaction", None)
        transaction = active[0] if isinstance(active, tuple) and len(active) == 2 else None
        recovering = bool(isinstance(active, tuple) and len(active) == 2 and active[1])
        if not os.path.lexists(journal_path):
            if recovering:
                raise ValueError("The pending restore journal disappeared before stop admission.")
            return None
        if linked(journal_path) or not journal_path.is_file():
            raise ValueError("The pending restore journal must be a regular, unlinked file.")
        try:
            on_disk = read_json(journal_path)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("The pending restore journal is unreadable or malformed.") from exc
        if recovering and on_disk != transaction:
            raise ValueError("The on-disk restore journal changed before stop admission.")
        if not recovering or on_disk.get("step") != "recovering":
            return None
        if (not isinstance(on_disk, dict)
                or on_disk.get("format") != "astrid-install-update/v1"
                or on_disk.get("step") != "recovering"
                or absolute(on_disk.get("data_root", "")) != absolute(data)):
            raise ValueError("The pending restore journal does not match this recovery.")
        if on_disk.get("transaction_kind") not in (None, "migration"):
            return None
        backup = on_disk.get("backup")
        if backup is None:
            return None
        realm = on_disk.get("realm")
        if (not isinstance(backup, dict) or not backup.get("root")
                or not isinstance(realm, dict) or not isinstance(realm.get("realm_root"), str)
                or not isinstance(realm.get("realm_id"), str)
                ):
            raise ValueError("The pending restore journal has incomplete recovery identity.")
        from astrid_runtime.bootstrap import RestoreSelection

        return RestoreSelection(journal_path=journal_path, selection_path=self.service.selection)

    def preflight_stop(self, data: Path) -> None:
        """Read-only C1 admission before installer preparation or writes."""
        try:
            from astrid_runtime.bootstrap import preflight_stop
            from astrid_runtime.paths import RuntimePaths

            paths = RuntimePaths.current(data_root=data)
            restore_selection = self._pending_restore_selection(data)
            if restore_selection is not None and (self.service.recorded() or {}).get("foreground") is not None:
                raise ValueError("Restore recovery refuses a retained foreground process record.")
            if restore_selection is None and not paths.runtime_support.exists():
                return
            preflight_stop(paths, restore_selection=restore_selection)
        except Exception as exc:
            raise self._stop_failure(exc) from exc

    def publish_launch_admission(self, version: Version, data: Path) -> None:
        """Refresh the selected install's launch action before its first C1 stop."""
        if self.managed_by == "pinokio":
            return
        self.service.publish_launch_admission(version, data)

    def stop(self, version: Version | None, data: Path, *, stop_manager=None) -> dict | None:
        """Run Runtime's single fenced stop and validated manager handoff.

        Returns C1's result; callers that ignore it are unaffected.
        """
        try:
            from astrid_runtime.bootstrap import down
            from astrid_runtime.paths import RuntimePaths
            from astrid_runtime.runtime_boundary import LocalRuntimeBoundary
            from runtime_protocol import process_control

            record = self.service.recorded()
            if record and (record.get("owner") != LABEL
                           or record.get("program_root") != str(self.root)
                           or record.get("data_root") != str(data)):
                raise ValueError("The service record does not match the selected installation.")
            paths = RuntimePaths.current(data_root=data)
            prior_control = process_control.read_setting(paths.runtime_support)
            platform.configure_process_control(self.process_control or prior_control)
            boundary = getattr(self, "_stop_boundary", None)
            if boundary is None:
                boundary = self._stop_boundary = LocalRuntimeBoundary()
            restore_selection = self._pending_restore_selection(data)
            foreground = (record or {}).get("foreground")
            if foreground is not None and not isinstance(foreground, dict):
                raise ValueError("The selected foreground process record is malformed.")
            if restore_selection is not None and foreground is not None:
                raise ValueError("Restore recovery refuses a retained foreground process record.")
            # CLI entry already preflighted; down takes fresh C1 admission for
            # every stop, including recovery and rollback.
            discovery = read_json(paths.discovery_path)
            owner_pid = discovery.get("pid") if isinstance(discovery, dict) else None
            if type(owner_pid) is not int or owner_pid <= 0:
                owner_pid = None
            # Read-only OS-manager probe. It stays ahead of C1 on every platform:
            # the in-callback recheck runs after Runtime's owner stop and its
            # refusals are downstream-classified. C1 busy admission runs later in
            # down(), so a probe refusal can be reported before the busy message.
            launchd_identity = None
            if self.managed_by != "pinokio":
                launchd_identity = self.service.validate_stop_target(version, data, owner_pid=owner_pid)
            def stop_selected_manager():
                current_record = self.service.recorded()
                if current_record and (current_record.get("owner") != LABEL
                                       or current_record.get("program_root") != str(self.root)
                                       or current_record.get("data_root") != str(data)):
                    raise ValueError("The service record changed before manager stop.")
                if ((current_record or {}).get("foreground") != foreground
                        or (record and not current_record)):
                    raise ValueError("The selected service owner changed before manager stop.")
                if self.managed_by != "pinokio":
                    if not current_record:
                        if self.service.login_service_exists():
                            raise ValueError("No selected service record authorizes this stop.")
                    else:
                        if (current_record or {}).get("foreground") is None:
                            if self.service.system == "darwin":
                                checked_identity = self.service.validate_stop_target(
                                    version, data, owner_pid=owner_pid,
                                    expected_identity=launchd_identity, require_stopped=True)
                                if launchd_identity is None and checked_identity is not None:
                                    self.service._validated_launchd_stop = None
                                    self.service._validated_launchd_stop_checked = False
                                    raise ValueError("A launchd service appeared after the C1 stop admission.")
                            else:
                                self.service.validate_stop_target(version, data, owner_pid=owner_pid)
                            self.service.disable()
                            self.service.stop()
                if stop_manager is not None:
                    stop_manager()

            def stop_selected_foreground():
                current_record = self.service.recorded()
                if ((current_record or {}).get("owner") != LABEL
                        or (current_record or {}).get("program_root") != str(self.root)
                        or (current_record or {}).get("data_root") != str(data)
                        or (current_record or {}).get("foreground") != foreground):
                    raise ValueError("The foreground selection changed before signal; refusing to stop it.")
                self.service.stop_foreground(expected=foreground)

            result = down(paths, boundary, stop_manager=stop_selected_manager,
                          stop_foreground=(stop_selected_foreground
                                           if restore_selection is None and isinstance(foreground, dict) else None),
                          restore_selection=restore_selection)
        except Exception as exc:
            raise self._stop_failure(exc) from exc

        # The Runtime fence has been released and C1 succeeded. Keep failures
        # from local bookkeeping downstream of the original lifecycle report.
        try:
            if self.process_control:
                self._write_process_control(data)
            # Retire only the unchanged foreground record after C1 completed.
            foreground = (record or {}).get("foreground")
            if isinstance(foreground, dict):
                selected = self.service.recorded()
                if selected.get("foreground") == foreground:
                    selected.pop("foreground")
                    write_json(self.service.selection, selected)
                    self.service._foreground_process = None
        except Exception as exc:
            raise self._downstream_stop_failure(exc) from exc
        return result

    @staticmethod
    def _backup_ignored(source: Path, parent: Path, names, *, support: bool) -> set[str]:
        ignored = set()
        if support and parent == source:
            ignored.add("logs")
        if support and parent == source / "runtime":
            ignored.update({"realms", "discovery.json", "instance.lock", "bootstrap.lock"})
        for name in set(names) - ignored:
            entry = parent / name
            if linked(entry):
                raise ValueError(f"Backup refused a linked entry: {entry}")
        return ignored.intersection(names)

    def _backup_size(self, source: Path, *, support=False) -> int:
        """Measure the same entries admitted by the support/clone copy."""
        def failed(error):
            raise error

        size = 0
        for folder, directories, files in os.walk(source, onerror=failed):
            parent = Path(folder)
            ignored = self._backup_ignored(source, parent, directories + files, support=support)
            directories[:] = [name for name in directories if name not in ignored]
            size += sum((parent / name).stat().st_size for name in files if name not in ignored)
        return size

    def _copy(self, source: Path, destination: Path, *, clone=False, support=False) -> None:
        from runtime_protocol.fsclone import clone_or_copy_file

        def copy_file(src, dst):
            src, dst = Path(src), Path(dst)
            if linked(src):
                raise ValueError(f"Backup refused a linked entry: {src}")
            if clone:
                clone_or_copy_file(src, dst)
            else:
                shutil.copy2(src, dst)
            return str(dst)

        def ignore(folder, names):
            return self._backup_ignored(source, Path(folder), names, support=support)

        shutil.copytree(source, destination, copy_function=copy_file, ignore=ignore)

    def backup(self, version: Version, data: Path, realm: dict) -> dict:
        from runtime_protocol import platform
        destination = self.root / "backups" / uuid.uuid4().hex[:12]
        kind = "clone" if self.service.system == "darwin" else "runtime"
        realm_root = absolute(realm["realm_root"])
        if kind != "clone":
            size = self._backup_size(realm_root) + self._backup_size(data, support=True)
            margin = max(256 * 1024 * 1024, size // 10)
            platform.private_directory(destination.parent)
            if shutil.disk_usage(self.root).free < size + margin:
                raise BackupSpaceError("There is not enough free disk space to back up your workspace.")
        try:
            platform.private_directory(destination)
            if kind == "clone":
                self._copy(realm_root, destination / "realm", clone=True)
            else:
                # The backup CLI uses authenticated RPC. Start only the neutral
                # daemon (no foreground service supervisor and no browser door).
                try:
                    self.runtime(version, data, "up", "--hosted-origin", "off", profile=True)
                    self.runtime(version, data, "backup", "--destination", str(destination / "realm"))
                finally:
                    self.stop(version, data)
            self._copy(data, destination / "support", clone=kind == "clone", support=True)
            result = {"root": str(destination), "kind": kind}
            write_json(destination / "backup.json", result)
            return result
        except BaseException:
            if destination.exists():
                shutil.rmtree(destination)
            raise

    def restore(self, old: Version, data: Path, realm: dict, backup: dict) -> None:
        if platform.sys.platform == "win32":
            platform.private_directory(data)
        destination = Path(backup["root"])
        realm_root = absolute(realm["realm_root"])
        restored = realm_root.with_name(realm_root.name + ".restore-" + uuid.uuid4().hex[:8])
        if backup["kind"] == "clone":
            self._copy(destination / "realm", restored, clone=True)
        else:
            # The offline restore API is the same authenticated implementation
            # behind `astrid-runtime restore`, without starting a damaged schema.
            script = ("from pathlib import Path; from runtime_protocol.backup import restore_backup; import sys; "
                      "key=Path(sys.argv[3]); "
                      "restore_backup(sys.argv[1],sys.argv[2],**({'key_path':key} if key.is_file() else {}))")
            retained_key = destination / "support" / "runtime" / "backup-auth.key"
            self.run([old.python, "-X", "utf8", "-I", "-c", script, destination / "realm", restored, retained_key],
                     extra_env=old.env)
        failed = realm_root.with_name(realm_root.name + ".failed-" + uuid.uuid4().hex[:8])
        # Recovery may replay after a process was killed between the two
        # renames. In that state the original is already absent and restoring
        # the retained backup is the required idempotent continuation.
        if realm_root.exists():
            platform.replace_path(realm_root, failed)
        try:
            platform.replace_path(restored, realm_root)
        except BaseException:
            if failed.exists() and not realm_root.exists():
                platform.replace_path(failed, realm_root)
            raise
        for failed_copy in sorted(path for path in realm_root.parent.glob(realm_root.name + ".failed-*")
                                  if path.is_dir() and not path.is_symlink()):
            self.say(f"The failed update's workspace copy is kept at {failed_copy}. You can delete it once Astrid works.")
        # Restore all support metadata, preserving unselected realms and extras.
        def copy_support(source: Path, target: Path) -> None:
            if source.is_dir():
                # copytree recreates inherited, unprotected Windows DACLs.
                platform.private_directory(target)
                for entry in source.iterdir():
                    copy_support(entry, target / entry.name)
                shutil.copystat(source, target)
            else:
                # A killed support restore must leave each file absent or
                # complete. A truncated catalog would block Runtime admission
                # before the retained realm could be restored on the next run.
                if source.name == "catalog.json" and target.parent.name == "runtime":
                    from astrid_runtime.bootstrap import _read_catalog
                    from astrid_runtime.io import atomic_write_json
                    from astrid_runtime.paths import RuntimePaths

                    support_root = source.parent.parent
                    retained_paths = RuntimePaths.current(data_root=support_root)
                    catalog = _read_catalog(replace(retained_paths, catalog_path=source))
                    realm_id = str(catalog.get("selected_realm_id") or "")
                    selected = next((row for row in catalog.get("realms", [])
                                     if str(row.get("realm_id")) == realm_id), None)
                    expected_realm = str(realm.get("realm_id") or "")
                    expected_root = absolute(realm.get("realm_root", ""))
                    if (selected is None or realm_id != expected_realm
                            or absolute(selected.get("data_root", "")) != expected_root):
                        raise ValueError("The retained Runtime catalog does not match the restore realm.")
                    atomic_write_json(target, catalog)
                    return
                descriptor, temporary_name = platform.private_temporary(target.parent, ".restore-")
                os.close(descriptor)
                temporary = Path(temporary_name)
                try:
                    shutil.copy2(source, temporary)
                    if platform.sys.platform == "win32":
                        platform.owner_only(temporary)
                    with temporary.open("rb") as stream:
                        os.fsync(stream.fileno())
                    platform.durable_replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)

        snapshot = destination / "support"
        for item in snapshot.iterdir():
            if item.name == "logs":
                continue  # Also preserve logs when restoring an older snapshot.
            target = data / item.name
            if item.name == "runtime":
                platform.private_directory(target)
                for entry in target.iterdir():
                    # Keep a readable selected catalog until the retained
                    # snapshot replaces it atomically below. A process kill
                    # during restore must not recreate the legacy gap.
                    if entry.name not in {"realms", "catalog.json"}:
                        if entry.is_dir():
                            shutil.rmtree(entry)
                        else:
                            entry.unlink()
                copy_support(item, target)
            else:
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                copy_support(item, target)

    def stage(self, manifest: dict, root: Path, data: Path) -> Version:
        install_release(manifest)
        # Repair needs only the installed Runtime. Require bootstrap inputs
        # only when dispatch actually prepares a fresh candidate.
        if not self.uv.is_absolute() or not self.uv.is_file():
            raise ValueError("Run the install line to provide the pinned user-local uv.")
        # Older release manifests omit this source. Record the effective pin
        # alongside the other sources without changing the caller's document.
        manifest = {**manifest, "vibecomfy": manifest.get("vibecomfy", {"sha": VIBECOMFY_ENGINE_REVISION})}
        if self.run([self.uv, "--version"]).split()[1] != manifest["tools"]["uv"]["version"]:
            raise ValueError("The installer needs the manifest's pinned uv.")
        folder = root / "versions" / new_version_id(manifest)
        folder.mkdir(parents=True)
        write_json(folder / "manifest.json", manifest)
        agent = source("agent", manifest["agent"]["sha"], folder / "a",
                       **({"repo": manifest["agent"]["repo"]} if "repo" in manifest["agent"] else {}))
        runtime = source("runtime", manifest["runtime"]["sha"], folder / "r",
                         **({"repo": manifest["runtime"]["repo"]} if "repo" in manifest["runtime"] else {}))
        vibecomfy = source("vibecomfy", manifest["vibecomfy"]["sha"], folder / "v",
                          **({"repo": manifest["vibecomfy"]["repo"]} if "repo" in manifest["vibecomfy"] else {}))
        environment = folder / "py"
        self.run([self.uv, "venv", "--python", manifest["tools"]["python"]["version"], environment])
        python = venv_python(environment)
        self.run([self.uv, "pip", "install", "--python", python, "--require-hashes",
                  "-r", agent / "requirements" / "runtime.lock"])
        self.run([self.uv, "pip", "install", "--python", python, "--no-deps", runtime,
                  runtime / "packages" / "python", agent, vibecomfy])
        env = install_tools(manifest, folder / "tools", platform_target(), self.run)
        prior = current(root) or self.existing(data)
        env.update({"ASTRID_RUNTIME_DATA_ROOT": str(data),
                    "ASTRID_RUNTIME_DOOR_PORT": os.environ.get(
                        "ASTRID_RUNTIME_DOOR_PORT",
                        prior.env.get("ASTRID_RUNTIME_DOOR_PORT", str(DEFAULT_DOOR_PORT)) if prior else str(DEFAULT_DOOR_PORT)),
                    "PATH": str(python.parent) + os.pathsep + env["PATH"]})
        profile = folder / "profile.json"
        version = Version(folder, python, profile, env, **install_identity(folder))
        env = version.env
        npm = binary(folder / "tools" / "node", "npm-cli.js")
        npm_installed = self.install_remotion_dependencies(agent, env, npm)
        # The lockfile's transitive @banodoco/timeline-schema package is the
        # source used by host_bootstrap. Install its Python subtree from this
        # candidate's npm tree so the environment follows the same pinned
        # package as the Remotion bundle.
        if npm_installed:
            schema_python = agent / "remotion" / "node_modules" / "@banodoco" / "timeline-schema" / "python"
            self.run([self.uv, "pip", "install", "--python", python, "--no-deps", schema_python])
        product_root = self._installed_product_root(python, env)
        # Python files execute from the selected venv's noneditable install.
        # Keep source-only resources explicit because they are not wheel data.
        env["ASTRID_HOST_EXECUTION_PROFILE"] = "installed"
        env["ASTRID_SOURCE_PROVENANCE_ROOT"] = str(agent)
        remotion_project = agent / "remotion"
        if (remotion_project / "package.json").is_file() and (remotion_project / "node_modules").is_dir():
            env["ASTRID_REMOTION_PROJECT_DIR"] = str(remotion_project.resolve())
        schema_root = remotion_project / "node_modules" / "@banodoco" / "timeline-schema" / "python"
        if (schema_root / "banodoco_timeline_schema" / "__init__.py").is_file():
            env["ASTRID_TIMELINE_SCHEMA_PYTHONPATH"] = str(schema_root.resolve())
        capability_matrix = agent / "config" / "astrid-beta-capabilities.json"
        if capability_matrix.is_file():
            env["ASTRID_CAPABILITY_MATRIX_PATH"] = str(capability_matrix.resolve())
        services_path = self._declare_services(
            folder=folder, agent=product_root, data=data, python=python,
            bun=env["ASTRID_BUN_EXECUTABLE"], env=env, old=prior,
        )
        # Compute the admitted source identity in the installed environment.
        digest = self.run([python, "-X", "utf8", "-I", "-c", "from astrid.core.execution.generic_host import source_checkout_digest; import pathlib,sys; print(source_checkout_digest(pathlib.Path(sys.argv[1])))", product_root], extra_env=env)
        write_json(profile, {"profile": "astrid", "runtime_checkout": str(runtime),
                             "source_checkout": str(product_root),
                             "runtime_environment": str(environment),
                             "source_digest": digest, "service_declaration": str(services_path)})
        persist_install_identity(version, root)
        return version

    def validate_candidate(self, version: Version, data: Path, realm: dict) -> None:
        """Admit the staged package against the live workspace without changing it."""
        from .core import inspect_schema

        folder = absolute(version.root)
        if folder.parent != absolute(self.root / "versions"):
            raise ValueError("The candidate is outside the managed versions directory.")
        # The installed profile and the install identity markers describe the same
        # release; refuse a candidate whose environment disagrees with itself.
        if version.env.get("ASTRID_HOST_EXECUTION_PROFILE") == "installed":
            if (version.env.get("ASTRID_INSTALL_RELEASE") != str(version.release)
                    or version.env.get("ASTRID_INSTALL_MANAGED_BY") != (version.managed_by or "")):
                raise ValueError("The installed profile and install identity markers disagree.")
        # Venv interpreters may themselves be links on POSIX; their directory
        # and the source manifest must remain inside the candidate.
        if (not absolute(version.python.parent).is_relative_to(folder)
                or not version.python.is_file()
                or not absolute(version.profile).is_relative_to(folder)
                or not version.profile.is_file()):
            raise ValueError("The staged candidate is incomplete or outside its version directory.")
        profile = read_json(version.profile)
        for key in ("runtime_checkout", "source_checkout", "runtime_environment", "service_declaration"):
            if not profile.get(key) or not absolute(profile[key]).is_relative_to(folder):
                raise ValueError("The staged source profile is incomplete or outside its version directory.")
        # Admission must use the same Runtime owners that will load this profile
        # during upgrade and startup. Path containment alone admits syntactically
        # invalid profiles and service declarations, allowing the old owner to be
        # stopped before those errors are discovered.
        required_directories = ("runtime_checkout", "source_checkout", "runtime_environment")
        for key in required_directories:
            if not absolute(profile[key]).is_dir():
                raise ValueError(f"The staged source profile {key} is missing or is not a directory.")
        declaration = absolute(profile["service_declaration"])
        if not declaration.is_file():
            raise ValueError("The staged service declaration is missing or is not a file.")
        validate_script = "\n".join((
            "import sys",
            "from pathlib import Path",
            "from astrid_runtime.bootstrap import SourceProfile, _validate_source_profile",
            "from astrid_runtime.runtime_boundary import LocalRuntimeBoundary",
            "from runtime_protocol.services import load_service_configuration",
            "profile = SourceProfile.load(Path(sys.argv[1]), expected_profile='astrid')",
            "_validate_source_profile(profile, expected_profile='astrid')",
            "LocalRuntimeBoundary._validate_source(profile)",
            "services = load_service_configuration(profile.service_declaration, "
            "python=sys.executable, source_checkout=profile.source_checkout)",
            "if services is None or not any(item.name == 'pack-host' for item in services.services):",
            "    raise ValueError('Service declaration must include pack-host')",
        ))
        try:
            self.run([version.python, "-X", "utf8", "-I", "-c", validate_script,
                      str(version.profile)], extra_env=version.env, timeout=120)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            raise ValueError(f"The candidate Runtime rejected its source profile or services: {exc}") from exc
        selected = self.workspace(version, data)
        if (selected["realm_id"] != realm["realm_id"]
                or absolute(selected["realm_root"]) != absolute(realm["realm_root"])):
            raise ValueError("The selected workspace changed while preparing the update.")
        # Use the candidate's installed Runtime to classify the exact schema
        # read-only. Supported older layouts are allowed here: migration runs
        # only after the journal and the lifecycle fence.
        inspect_schema(self, version, realm, require_current=False)

    def create(self, version: Version, data: Path) -> dict:
        realm_id = str(uuid.uuid4())
        return self.runtime(version, data, "workspace", "create", "--realm-root",
                            str(data / "runtime" / "realms" / realm_id), "--realm-id", realm_id,
                            *self._process_control_args(), profile=True)

    def attach_existing(self, version: Version, data: Path) -> dict:
        realm = self.workspace(version, data)
        return self.runtime(version, data, "workspace", "attach", "--realm-root", realm["realm_root"],
                            "--realm-id", realm["realm_id"], *self._process_control_args(), profile=True)

    def _write_process_control(self, data: Path) -> None:
        from runtime_protocol import process_control

        support = data / "runtime"
        if support.is_dir() and process_control.read_setting(support) != self.process_control:
            process_control.write_setting(support, self.process_control)

    def _process_control_args(self) -> tuple[str, ...]:
        return ("--process-control", self.process_control) if self.process_control else ()

    def doctor(self, version: Version, data: Path, bundle: Path) -> Path:
        bundle = absolute(bundle)
        before = bundle.stat() if bundle.exists() else None
        argv = [version.python, "-X", "utf8", "-I", "-m", "astrid", "doctor", "--bundle", bundle]
        try:
            self.run(argv, extra_env={**version.env, "ASTRID_RUNTIME_DATA_ROOT": str(data)})
        except subprocess.CalledProcessError:
            after = bundle.stat() if bundle.exists() else None
            if after is None or (before is not None and after.st_mtime_ns == before.st_mtime_ns
                                 and after.st_ino == before.st_ino):
                raise
        reveal_bundle(bundle)
        return bundle

    def upgrade(self, version: Version, data: Path) -> None:
        self.runtime(version, data, "upgrade", profile=True)
        if self.process_control:
            # Runtime upgrade preserves support metadata. Apply the latest
            # install flag before the candidate is started by the service.
            self._write_process_control(data)

    def register(self, version: Version, data: Path) -> None:
        if version.managed_by != self.managed_by:
            # The current install context is authoritative for hand-built or
            # legacy Version objects that predate the identity field.
            version = replace(version, managed_by=self.managed_by)
        self.persist_identity(version)
        chat_cwd, chat_options = self._prior_chat_settings(version)
        if chat_cwd == data / "chat":
            platform.private_directory(chat_cwd)
        if self.managed_by == "pinokio":
            persist_install_identity(version, self.root, selection=self.service.selection, fields={
                "owner": "haus.astrid.runtime", "program_root": str(self.root),
                "data_root": str(data), "managed_by": "pinokio",
            })
        else:
            self.service.register(version, data)
        realm = self.workspace(version, data)
        record = self.service.recorded()
        settings = {"ASTRID_ACP_CWD": str(chat_cwd), **chat_options} if chat_cwd else {}
        write_json(self.service.selection, {**record,
                                           "realm_id": realm["realm_id"],
                                           "realm_root": realm["realm_root"], "chat_settings": settings})

    def persist_identity(self, version: Version) -> None:
        """Upgrade legacy version and selection records without service changes."""
        persist_install_identity(version, self.root, selection=self.service.selection)

    def start(self, version: Version, data: Path) -> None:
        if self.managed_by == "pinokio":
            raise RuntimeError("Start Astrid in Pinokio.")
        if not self.service.start():
            self.service.start_foreground(version, data)

    def restart_required(self) -> bool:
        return self.service.restart_required()

    def verify(self, version: Version, data: Path, realm_id: str, *, timeout: float = 90,
               hosted: bool = True) -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        port = int(version.env.get("ASTRID_RUNTIME_DOOR_PORT", str(DEFAULT_DOOR_PORT))) if hosted else None
        deadline = time.monotonic() + timeout
        error = None
        identity_ok = False
        while time.monotonic() < deadline:
            try:
                status = self.runtime(version, data, "status", timeout=3)
                discovery = read_json(data / "runtime" / "discovery.json")
                endpoint = discovery.get("door_endpoint", "")
                if hosted and endpoint != f"http://127.0.0.1:{port}":
                    raise ValueError("Astrid's recorded door is not on the configured loopback port.")

                def get(path):
                    if not hosted:
                        return self.runtime(version, data, "status", timeout=3).get("health", {})
                    request = urllib.request.Request(endpoint + path, headers={"Origin": "https://astrid.haus"})
                    with opener.open(request, timeout=2) as response:
                        return json.load(response)

                if hosted:
                    health = get("/api/runtime/v1/health")
                else:
                    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary

                    # The generated status Health model omits startup identity.
                    # Read the existing canonical owner-health handoff directly.
                    health = LocalRuntimeBoundary().owner_startup_identity(
                        endpoint=str(discovery.get("endpoint") or ""),
                        instance_id=str(discovery.get("runtime_instance_id") or ""),
                    )
                    if not isinstance(health, dict):
                        raise ValueError("The local Runtime owner health is unavailable.")
                realm = get("/api/runtime/v1/realm") if hosted else {"realm_id": discovery.get("active_realm")}
                services = health.get("services", [])
                managed_identity = None
                if version.root.parent == self.root / "versions":
                    managed_identity = {"release": version.release, "managed_by": version.managed_by}

                def matches_managed_identity(value):
                    return (isinstance(value, dict) and set(value) == {"release", "managed_by"}
                            and type(value.get("release")) is int
                            and value.get("managed_by") in (None, "pinokio")
                            and value == managed_identity)

                if (health.get("status") != "ok" or realm.get("realm_id") != realm_id
                        or discovery.get("active_realm") != realm_id
                        or health.get("runtime_instance_id") != discovery.get("runtime_instance_id")
                        or (status.get("discovery") or {}).get("runtime_instance_id") != discovery.get("runtime_instance_id")
                        or (managed_identity is not None and
                            (not matches_managed_identity(health.get("identity"))
                             or not matches_managed_identity(discovery.get("identity"))))
                        or status.get("support", {}).get("pid_alive") is not True):
                    raise ValueError("The browser door belongs to a different Runtime, release, or workspace.")
                identity_ok = True
                break
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                error = exc
                time.sleep(0.25)
        if not identity_ok:
            cause = _last_logged_error(self.service.log)
            if cause:
                raise VerificationError(
                    f"Astrid Runtime did not start: {cause} (details: {self.service.log})",
                    safe_message=f"Astrid Runtime didn't start. Its log says why: {self.service.log}",
                )
            raise VerificationError(
                f"Astrid's browser door did not become ready: {error}",
                safe_message=(f"Astrid didn't become reachable from the browser in time. "
                              f"Its log may say why: {self.service.log}"),
            )

        ready_file = data / "runtime" / "generic-host.ready.json"
        state, pack_host, failed = _classify_services(services, ready_file)
        if state == "ready":
            return

        def service_failure(*, crashed: bool) -> VerificationError:
            service_name = failed.get("name") if failed else "pack-host"
            service_log = platform.log_dir() / f"service-{service_name}.log"
            tail = _last_service_log(service_log)
            service_state = pack_host.get("state", "missing") if pack_host else "missing"
            reason = pack_host.get("reason") if pack_host else None
            last_exit = pack_host.get("last_exit") if pack_host else None
            label = (
                "An Astrid service stopped while starting"
                if crashed else "Astrid's task host didn't finish starting in time"
            )
            detail = (f"{label}; see the log for the cause. "
                      f"state={service_state}, reason={reason or 'none'}, "
                      f"last_exit={last_exit!r}; log={service_log}")
            if failed is not None:
                detail += (f"; failed_service={failed.get('name')}, failed_state={failed.get('state')}, "
                           f"failed_reason={failed.get('reason') or 'none'}, "
                           f"failed_last_exit={failed.get('last_exit')!r}")
            safe_detail = detail
            if tail:
                detail += f"\nLast service log lines:\n{tail}"
            return VerificationError(detail, safe_message=safe_detail)

        if state == "crashed":
            raise service_failure(crashed=True)
        profile = read_json(version.profile)
        declaration = read_json(Path(profile["service_declaration"]))
        pack_host_declaration = next(
            service for service in declaration.get("services", [])
            if isinstance(service, dict) and service.get("name") == "pack-host"
        )
        ready_deadline = time.monotonic() + pack_host_declaration["ready"]["timeout_seconds"]
        while state == "starting":
            if time.monotonic() >= ready_deadline:
                raise service_failure(crashed=False)
            try:
                health = get("/api/runtime/v1/health")
                services = health.get("services", [])
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                time.sleep(0.25)
                continue
            time.sleep(0.25)
            state, pack_host, failed = _classify_services(services, ready_file)
            if state == "crashed":
                raise service_failure(crashed=True)

    def sync_skills(self, version: Version) -> None:
        """Best-effort install of Astrid's skill view into detected harnesses."""
        argv = [version.python, "-X", "utf8", "-I", "-m", "astrid.skills", "sync"]
        failed = False
        try:
            self.run(argv, extra_env=version.env, timeout=60)
            outcome = "Astrid skills sync completed successfully.\n"
        except (OSError, subprocess.SubprocessError) as exc:
            failed = True
            output = "" if isinstance(exc, subprocess.CalledProcessError) else (
                getattr(exc, "output", None) or getattr(exc, "stdout", None) or ""
            )
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            outcome = str(output) + f"\nAstrid skills sync failed ({type(exc).__name__}).\n"
        with self.log.open("a", encoding="utf-8") as stream:
            platform.owner_only(stream.fileno())
            stream.write(outcome)
        if failed:
            self.say("Couldn't add Astrid's skill to your agents; see the log.")

    def finish(self, old: Version | None, candidate: Version, backup: dict | None) -> None:
        # Keep the immediately previous program and backup. Failures cleaning an
        # older locked version are residue, not grounds to roll back a good update.
        keep = {candidate.root, old.root if old else candidate.root}
        for path in (self.root / "versions").iterdir():
            if path not in keep:
                try:
                    shutil.rmtree(path)
                except OSError:
                    pass
        if backup:
            for path in (self.root / "backups").iterdir():
                if path != Path(backup["root"]):
                    try:
                        shutil.rmtree(path)
                    except OSError:
                        pass

    def unregister(self) -> None:
        record = self.service.recorded()
        if record:
            kept = dict(record)
            version = current(self.root)
            if "chat_settings" not in kept and version is not None:
                chat_cwd, options = self._selected_chat_settings(version, Path(record["data_root"]))
                kept["chat_settings"] = {"ASTRID_ACP_CWD": str(chat_cwd), **options}
            write_json(self.service.selection, {**kept, "uninstalled": True})
        if self.managed_by != "pinokio":
            self.service.unregister()

    def preflight_remove(self, root: Path, data: Path, *, delete_data: bool) -> Path | None:
        """Prove that removing programs cannot also remove a workspace."""
        root, data = absolute(root), absolute(data)
        trees = [root]
        bootstrap = bootstrap_root(self.service.home, self.service.system)
        if root != bootstrap.parent and self.uv.parent == bootstrap and bootstrap.exists():
            absolute(bootstrap)
            # The Windows parent also removes the installer lock directory.
            trees.append(absolute(bootstrap.parent if self.service.system == "win32" else bootstrap))

        def overlaps(first, second):
            return first == second or first in second.parents or second in first.parents

        if any(overlaps(data, tree) for tree in trees):
            raise ValueError("The workspace support directory overlaps a program removal directory.")
        record = self.service.recorded()
        recorded_realm = absolute(record["realm_root"]) if record.get("realm_root") else None
        catalog = data / "runtime" / "catalog.json"
        version = current(root) or (self.existing(data) if data.exists() else None)
        realm_root = None
        if catalog.exists():
            realm_root = absolute(self.workspace(version, data)["realm_root"])
        elif recorded_realm is not None and recorded_realm.exists():
            # Recover through Runtime's public read-only identity API even if a
            # previous uninstall already removed the version and support files.
            args = ["workspace", "inspect", "--realm-root", str(recorded_realm)]
            if record.get("realm_id"):
                args += ["--expected-realm-id", record["realm_id"]]
            value = self.runtime(None, data, *args)
            if not value.get("realm_id") or not value.get("realm_root"):
                raise ValueError("The selected workspace has no unambiguous identity.")
            realm_root = absolute(value["realm_root"])
        elif recorded_realm is None and version is not None and data.exists():
            # Without a retained selection, incomplete support metadata cannot
            # establish that an attached workspace is outside the removal tree.
            realm_root = absolute(self.workspace(version, data)["realm_root"])
        if realm_root is not None and any(overlaps(realm_root, tree) for tree in trees):
            raise ValueError("The attached workspace overlaps a program removal directory.")
        return realm_root

    def remove(self, root: Path, data: Path, *, delete_data: bool) -> list[Path]:
        residue = []
        # Only directories created and marked by this installer are removable.
        marker = read_json(root / "install.json")
        record = read_json(self.service.selection)
        kept = (record.get("owner") == "haus.astrid.runtime" and record.get("program_root") == str(root)
                and record.get("data_root") == str(data) and record.get("uninstalled") is True)
        bootstrap_only = not root.exists() or all(path.name == "bootstrap" for path in root.iterdir())
        if marker.get("owner") != "haus.astrid.runtime" and not (kept and bootstrap_only):
            raise ValueError("Refusing to remove an unowned installation directory.")
        realm_root = self.preflight_remove(root, data, delete_data=delete_data)
        remove_shim(self.service)
        data_targets = [data] if delete_data else []
        if delete_data and realm_root is not None and realm_root != data and data not in realm_root.parents:
            data_targets.insert(0, realm_root)
        # Keep the ownership/data-root marker until the last owned file is
        # gone, so rerunning the line can finish removal after a Windows lock.
        for path in list(root.iterdir()) if root.exists() else []:
            if path.name == "install.json":
                continue
            if path.name == "astrid.cmd" and not owned_shim(path):
                residue.append(path)
                continue
            try:
                if path.is_dir() and not linked(path):
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError:
                residue.append(path)
        if not residue and root.exists():
            (root / "install.json").unlink(missing_ok=True)
            try:
                root.rmdir()
            except OSError:
                retained_marker = {**marker, "owner": "haus.astrid.runtime", "data_root": str(data)}
                if record.get("managed_by") == "pinokio":
                    retained_marker["managed_by"] = "pinokio"
                write_json(root / "install.json", retained_marker)
                residue.append(root)
        for data_path in data_targets:
            if not data_path.exists():
                continue
            try:
                shutil.rmtree(data_path)
            except OSError:
                residue.append(data_path)
        # The thin line bootstraps under the default root even when the managed
        # program uses --root. Only remove that exact supplied bootstrap.
        bootstrap = bootstrap_root(self.service.home, self.service.system)
        # Windows' PowerShell parent removes its exact bootstrap and lock after
        # uv and this Python process exit, releasing their executable handles.
        if (self.service.system != "win32" and root != bootstrap.parent
                and self.uv.parent == bootstrap and bootstrap.exists()):
            try:
                absolute(bootstrap)
                shutil.rmtree(bootstrap)
            except (OSError, ValueError):
                residue.append(bootstrap)
            else:
                try:
                    bootstrap.parent.rmdir()
                except OSError:
                    pass  # Other files in the default root remain untouched.
        if not residue and getattr(self, "managed_by", None) == "pinokio":
            # Release the one-user claim only after program removal succeeds.
            # Retain the normal uninstalled receipt for workspace recovery,
            # with no manager or program pointer blocking a later install.
            write_json(self.service.selection, {key: value for key, value in record.items()
                                               if key not in {"managed_by", "program_root"}})
        return residue

    def open(self) -> None:
        def report(line):
            self.say(line)
            with self.log.open("a", encoding="utf-8") as stream:
                platform.owner_only(stream.fileno())
                stream.write(line + "\n")

        shim = hint = None
        try:
            shim, hint = install_shim(self.service, say=report)
        except Exception as exc:  # noqa: BLE001 - the command shim is optional; installation must still finish
            report(f"Astrid command shim setup failed ({type(exc).__name__}); use the run-astrid launcher.")
        marker = self.root / "install.json"
        command_shim = shim if self.service.system == "win32" or hint is None else None
        write_json(marker, {**read_json(marker), "command_shim": str(command_shim) if command_shim else None})
        try:
            webbrowser.open("https://astrid.haus/?connect=1")
        except OSError:
            pass  # The printed URL remains usable on headless computers.
        if hint:
            self.say(hint)
