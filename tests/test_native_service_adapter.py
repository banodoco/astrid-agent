from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import journey_failure_modes as failure
from scripts import native_service_adapter as native
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE, valid_observation

FIXTURE = Path(__file__).parent / "fixtures" / "release0"


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def task_xml(
    action: tuple[str, ...],
    principal: str,
    working_directory: str,
    *,
    namespace: bool = True,
    crlf: bool = False,
    multiple_actions: bool = False,
    extra_action: bool = False,
    multiple_instances_policy: str = "IgnoreNew",
) -> str:
    xmlns = ' xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"' if namespace else ""
    arguments = subprocess.list2cmdline(list(action[1:]))
    actions = (
        f"<Exec><Command>{action[0]}</Command><Arguments>{arguments}</Arguments>"
        f"<WorkingDirectory>{working_directory}</WorkingDirectory></Exec>"
    )
    if multiple_actions:
        actions += actions
    if extra_action:
        actions += (
            "<ComHandler><ClassId>{00000000-0000-0000-0000-000000000000}</ClassId></ComHandler>"
        )
    xml = (
        f'<?xml version="1.0"?><Task{xmlns}>\n'
        f'  <Principals><Principal id="Author"><UserId>{principal}</UserId>'
        "</Principal></Principals>\n"
        f"  <Settings><MultipleInstancesPolicy>{multiple_instances_policy}</MultipleInstancesPolicy>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries></Settings>\n"
        f'  <Actions Context="Author">{actions}</Actions>\n'
        "</Task>\n"
    )
    return xml.replace("\n", "\r\n") if crlf else xml


class FakeProcessProbe:
    def __init__(self) -> None:
        self.active: dict[int, dict] = {}
        self.wrong_birth = False
        self.deny_absence = False
        self.data_root: Path | None = None
        self.data_identity_sha256 = ""
        self.workspace_identity = ""
        self.semantic_data_sha256 = ""
        self.semantic_data: dict = {}
        self.data_manifest_path: Path | None = None
        self.data_manifest_sha256 = ""
        self.data_root_identity_sha256 = ""
        self.durable_journal_path: Path | None = None
        self.durable_journal_sha256 = ""

    def configure_data(self, data_root: Path, semantic_data: dict) -> None:
        self.data_root = data_root
        self.workspace_identity = "workspace-primary"
        entities = {
            name: semantic_data["entities"][name]["after_sha256"] for name in failure.DATA_ENTITIES
        }
        self.data_identity_sha256 = hashlib.sha256(
            json.dumps(entities, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.semantic_data = {
            "before_sha256": self.data_identity_sha256,
            "after_sha256": self.data_identity_sha256,
            "identical": True,
            "entities": {
                name: {"before_sha256": digest, "after_sha256": digest}
                for name, digest in entities.items()
            },
        }
        self.semantic_data_sha256 = hashlib.sha256(
            json.dumps(self.semantic_data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        manifest = {
            "schema": native.DATA_IDENTITY_SCHEMA,
            "workspace_identity": self.workspace_identity,
            "entities": entities,
        }
        self.data_manifest_path = data_root / "native-data-identity.json"
        self.data_manifest_path.write_text(
            json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.data_manifest_sha256 = file_digest(self.data_manifest_path)
        self.data_root_identity_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "data_root": str(data_root.resolve()),
                    "manifest_sha256": self.data_manifest_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def configure_journal(self, operation_id: str, phase: str) -> None:
        assert self.data_root is not None
        self.durable_journal_path = self.data_root.parent / "durable-operation-journal.json"
        self.durable_journal_path.write_text(
            json.dumps(
                {
                    "schema": native.DURABLE_JOURNAL_SCHEMA,
                    "operation_id": operation_id,
                    "phase": phase,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.durable_journal_sha256 = file_digest(self.durable_journal_path)

    def seed(self, pid: int, executable: Path, *, replace: bool = True) -> dict:
        assert self.data_root is not None
        assert self.data_manifest_path is not None
        if replace:
            self.active.clear()
        row = {
            "pid": pid,
            "process_birth_id": f"birth-{pid}",
            "runtime_instance_id": f"runtime-{pid}",
            "executable_path": str(executable.resolve()),
            "executable_sha256": file_digest(executable),
            "data_root": str(self.data_root.resolve()),
            "data_identity_sha256": self.data_identity_sha256,
            "workspace_identity": self.workspace_identity,
            "semantic_data_sha256": self.semantic_data_sha256,
            "data_manifest_path": str(self.data_manifest_path.resolve()),
            "data_manifest_sha256": self.data_manifest_sha256,
            "data_root_identity_sha256": self.data_root_identity_sha256,
        }
        self.active[pid] = row
        return dict(row)

    def seed_controller(
        self,
        pid: int,
        executable: Path,
        *,
        operation_id: str,
        phase: str,
    ) -> dict:
        assert self.durable_journal_path is not None
        row = {
            "pid": pid,
            "process_birth_id": f"controller-birth-{pid}",
            "controller_instance_id": f"controller-instance-{pid}",
            "executable_path": str(executable.resolve()),
            "executable_sha256": file_digest(executable),
            "runtime_operation_id": operation_id,
            "durable_phase": phase,
            "durable_journal_path": str(self.durable_journal_path.resolve()),
            "durable_journal_sha256": self.durable_journal_sha256,
        }
        self.active[pid] = row
        return dict(row)

    def terminate(self, pid: int) -> None:
        self.active.pop(pid, None)

    def observe(self, pid: int, *, os_name: str, expected_executable: Path) -> dict:
        if pid not in self.active:
            raise RuntimeError("fake process is absent")
        row = self.active[pid]
        assert Path(row["executable_path"]) == expected_executable.resolve()
        return {
            "pid": pid,
            "process_birth_id": "foreign-birth" if self.wrong_birth else row["process_birth_id"],
            "executable_path": row["executable_path"],
            "executable_sha256": row["executable_sha256"],
        }

    def incarnation_absent(self, pid: int, birth_id: str, *, os_name: str) -> bool:
        if self.deny_absence:
            raise RuntimeError("fake process absence query denied")
        return pid not in self.active or self.active[pid]["process_birth_id"] != birth_id

    def owned_action_processes(self, action, *, os_name: str):
        executable = str(Path(action[0]).resolve())
        return [
            {"pid": pid, "process_birth_id": row["process_birth_id"]}
            for pid, row in self.active.items()
            if row["executable_path"] == executable
        ]


class StatefulRunner:
    def __init__(
        self,
        os_name: str,
        heads: dict[str, str],
        process: FakeProcessProbe,
        *,
        action_count: int = 1,
        instance_count: int = 1,
        zero_pid: bool = False,
        adversarial_argv: bool = False,
        removal_sticks: bool = False,
        respawn_after_removal: bool = False,
        respawn_candidate_after_removal: bool = False,
        delete_data_manifest_on_uninstall: bool = False,
        tamper_data_manifest_on_uninstall: bool = False,
        exported_xml_variant: str = "equivalent",
    ) -> None:
        self.os_name = os_name
        self.heads = heads
        self.process = process
        self.action_count = action_count
        self.instance_count = instance_count
        self.zero_pid = zero_pid
        self.adversarial_argv = adversarial_argv
        self.removal_sticks = removal_sticks
        self.respawn_after_removal = respawn_after_removal
        self.respawn_candidate_after_removal = respawn_candidate_after_removal
        self.delete_data_manifest_on_uninstall = delete_data_manifest_on_uninstall
        self.tamper_data_manifest_on_uninstall = tamper_data_manifest_on_uninstall
        self.candidate_executable: Path | None = None
        self.exported_xml_variant = exported_xml_variant
        self.registered = False
        self.action: tuple[str, ...] = ()
        self.definition_path: Path | None = None
        self.working_directory = ""
        self.principal = "S-1-5-21-123" if os_name == "windows" else "runner"
        self.exported_xml = ""
        self.calls: list[list[str]] = []

    def configure(self, definition: Path, action: tuple[str, ...], working_directory: str) -> None:
        self.definition_path = definition
        self.action = action
        self.working_directory = working_directory
        if self.os_name != "windows":
            self.exported_xml = definition.read_text(encoding="utf-8")
            return
        exported_action = action
        exported_principal = self.principal
        multiple_actions = False
        multiple_instances_policy = "IgnoreNew"
        if self.exported_xml_variant == "changed-action":
            exported_action = (str(Path(action[0]).with_name("foreign-runtime.exe")), *action[1:])
        elif self.exported_xml_variant == "changed-principal":
            exported_principal = "S-1-5-21-FOREIGN"
        elif self.exported_xml_variant == "multiple-actions":
            multiple_actions = True
        elif self.exported_xml_variant == "changed-settings":
            multiple_instances_policy = "Parallel"
        self.exported_xml = task_xml(
            exported_action,
            exported_principal,
            working_directory,
            namespace=False,
            crlf=True,
            multiple_actions=multiple_actions,
            multiple_instances_policy=multiple_instances_policy,
        )

    def _pid(self) -> int:
        if self.zero_pid:
            return 0
        return next(iter(self.process.active), 0)

    def _launchd_query(self) -> native.CommandResult:
        if not self.registered:
            return native.CommandResult(3, "", "Could not find service")
        lines = [
            f"path = {self.definition_path}",
            f"program = {self.action[0]}",
            "arguments = {",
            *[f"  {index} = {argument}" for index, argument in enumerate(self.action)],
            "}",
            "state = running",
            f"pid = {self._pid()}",
        ]
        return native.CommandResult(
            0,
            "\n".join(lines) + "\n",
            "SECRET_TOKEN=do-not-publish\n",
        )

    def _systemd_query(self) -> native.CommandResult:
        if not self.registered:
            return native.CommandResult(0, "LoadState=not-found\n", "")
        action = list(self.action)
        if self.adversarial_argv:
            action.append("--foreign")
        pid = self._pid()
        fields = {
            "Id": native.SYSTEMD_UNIT,
            "Names": native.SYSTEMD_UNIT,
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "running",
            "FragmentPath": str(self.definition_path),
            "MainPID": str(pid),
            "InvocationID": f"{pid:032x}",
            "NeedDaemonReload": "no",
            "ExecStart": f"{{ path={action[0]} ; argv[]={shlex.join(action)} ; ignore_errors=no ; }}",
        }
        return native.CommandResult(0, "".join(f"{key}={value}\n" for key, value in fields.items()))

    def _windows_query(self) -> native.CommandResult:
        if not self.registered:
            return native.CommandResult(0, json.dumps({"TaskCount": 0, "ComPresent": False}))
        active = list(self.process.active)
        instances = [
            {
                "InstanceGuid": f"00000000-0000-0000-0000-{pid:012d}",
                "EnginePID": pid,
            }
            for index, pid in enumerate(active[: self.instance_count])
        ]
        if self.instance_count > len(instances):
            instances.extend(
                {"InstanceGuid": f"foreign-{index}", "EnginePID": 900 + index}
                for index in range(self.instance_count - len(instances))
            )
        return native.CommandResult(
            0,
            json.dumps(
                {
                    "TaskCount": 1,
                    "ComPresent": True,
                    "ActionCount": self.action_count,
                    "Execute": self.action[0],
                    "Arguments": subprocess.list2cmdline(list(self.action[1:])),
                    "WorkingDirectory": self.working_directory,
                    "Principal": self.principal,
                    "State": "Running" if instances else "Ready",
                    "ExportedXml": self.exported_xml,
                    "Instances": instances,
                }
            ),
        )

    def product_uninstall(self, definition_path: Path) -> None:
        if self.removal_sticks:
            return
        prior = list(self.process.active.values())
        self.registered = False
        self.process.active.clear()
        definition_path.unlink()
        if self.respawn_after_removal and prior:
            executable = Path(prior[-1]["executable_path"])
            self.process.seed(999, executable)
        if self.respawn_candidate_after_removal:
            assert self.candidate_executable is not None
            self.process.seed(999, self.candidate_executable)
        if self.delete_data_manifest_on_uninstall:
            assert self.process.data_manifest_path is not None
            self.process.data_manifest_path.unlink()
        if self.tamper_data_manifest_on_uninstall:
            assert self.process.data_manifest_path is not None
            manifest = json.loads(self.process.data_manifest_path.read_text(encoding="utf-8"))
            manifest["entities"]["document"] = "f" * 64
            self.process.data_manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
            )

    def swap_action(self) -> None:
        self.action = (str(Path(self.action[0]).with_name("foreign-runtime")), *self.action[1:])

    def run(self, argv, *, timeout=30) -> native.CommandResult:
        argv = [str(value) for value in argv]
        self.calls.append(argv)
        if argv[:1] == ["git"]:
            return native.CommandResult(0, self.heads[str(Path(argv[2]))] + "\n", "")
        if argv == ["id", "-u"]:
            return native.CommandResult(0, "501\n")
        if argv == ["id", "-un"]:
            return native.CommandResult(0, "runner\n")
        if argv == ["uname", "-r"]:
            return native.CommandResult(0, "6.11.0-hosted\n")
        if argv == ["sw_vers", "-buildVersion"]:
            return native.CommandResult(0, "23G93\n")
        if argv[:2] == ["launchctl", "version"]:
            return native.CommandResult(0, "launchd 1069.140.2\n")
        if argv == ["launchctl", "asuser", "501", "/usr/bin/true"]:
            return native.CommandResult(0)
        if argv[:2] == ["launchctl", "print"]:
            return self._launchd_query()
        if argv[:2] == ["launchctl", "bootstrap"]:
            self.registered = True
            return native.CommandResult(0)
        if argv[:2] in (["launchctl", "kickstart"], ["launchctl", "bootout"]):
            if argv[1] == "bootout":
                self.registered = False
                self.process.active.clear()
            return native.CommandResult(0)
        if argv[:2] == ["kill", "-TERM"]:
            self.process.terminate(int(argv[2]))
            return native.CommandResult(0)
        if argv[:3] == ["systemctl", "--user", "--version"]:
            return native.CommandResult(0, "systemd 255 (255.4-1)\n")
        if argv[:3] == ["systemctl", "--user", "is-system-running"]:
            return native.CommandResult(0, "running\n")
        if argv[:3] == ["systemctl", "--user", "show"]:
            return self._systemd_query()
        if argv[:3] in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable"],
            ["systemctl", "--user", "start"],
            ["systemctl", "--user", "restart"],
        ):
            if argv[2] in {"enable", "start"}:
                self.registered = True
            return native.CommandResult(0)
        if argv[:3] == ["systemctl", "--user", "disable"]:
            self.registered = False
            self.process.active.clear()
            return native.CommandResult(0)
        if self.os_name == "windows" and argv[0].endswith("powershell.exe"):
            script = argv[-1]
            if "Win32_OperatingSystem" in script:
                return native.CommandResult(0, "20348\n")
            if "Get-Command Get-ScheduledTask" in script:
                return native.CommandResult(
                    0,
                    json.dumps(
                        {
                            "PSVersion": "5.1.20348.2849",
                            "Module": "ScheduledTasks",
                            "SID": self.principal,
                            "Com": True,
                        }
                    ),
                )
            if "$tasks=@(Get-ScheduledTask" in script:
                return self._windows_query()
            if "Register-ScheduledTask" in script:
                assert "-Force" not in script
                self.registered = True
                return native.CommandResult(0)
            if "Unregister-ScheduledTask" in script:
                self.registered = False
                self.process.active.clear()
                return native.CommandResult(0)
            if "Stop-Process" in script:
                if self.process.active:
                    self.process.terminate(next(iter(self.process.active)))
                return native.CommandResult(0)
            if "Start-ScheduledTask" in script or "Stop-ScheduledTask" in script:
                return native.CommandResult(0)
        raise AssertionError(f"unexpected command: {argv}")


def hosted_environment(tmp_path: Path, os_name: str, powershell: Path | None) -> dict[str, str]:
    runner_os, image_os = {
        "macos": ("macOS", "macos14"),
        "linux": ("Linux", "ubuntu24"),
        "windows": ("Windows", "win22"),
    }[os_name]
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": runner_os,
        "ImageOS": image_os,
        "ImageVersion": "20261030.1",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "123456",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "m4-6-native",
        "GITHUB_REPOSITORY": "haus/astrid",
        "GITHUB_WORKFLOW": "journey",
        "RUNNER_TEMP": str(tmp_path),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    if powershell is not None:
        env["SystemRoot"] = str(powershell.parents[3])
    return env


def make_repos(tmp_path: Path) -> tuple[dict[str, Path], dict[str, str]]:
    repos: dict[str, Path] = {}
    heads: dict[str, str] = {}
    for name in ("app", "agent", "runtime", "pinokio"):
        path = tmp_path / f"repo-{name}"
        path.mkdir(parents=True)
        repos[name] = path
        heads[str(path)] = PROVISIONAL_CANDIDATE[name]
    return repos, heads


class FakeJourneyDriver:
    def __init__(
        self,
        candidate: dict,
        process: FakeProcessProbe,
        runner: StatefulRunner,
        *,
        wrong_definition_path: bool = False,
        extra_source_action: bool = False,
        fail_at: str | None = None,
    ) -> None:
        self.candidate = candidate
        self.process = process
        self.runner = runner
        self.wrong_definition_path = wrong_definition_path
        self.extra_source_action = extra_source_action
        self.fail_at = fail_at
        self.definition_path: Path | None = None
        self.release0: Path | None = None
        self.candidate_executable: Path | None = None
        self.controller_executable: Path | None = None

    def service_spec(self, context) -> native.ServiceSpec:
        product = context["work_root"] / "product"
        product.mkdir(parents=True, exist_ok=True)
        self.release0 = product / "release0-runtime.exe"
        self.candidate_executable = product / "candidate-runtime.exe"
        self.controller_executable = product / "failure-controller.exe"
        self.release0.write_bytes(b"real release zero executable")
        self.candidate_executable.write_bytes(b"candidate executable")
        self.runner.candidate_executable = self.candidate_executable
        self.controller_executable.write_bytes(b"external failure controller")
        data_root = context["work_root"] / "data"
        data_root.mkdir()
        semantic_data = valid_observation(context["slot_id"], self.candidate)["scenarios"][
            "interruption_recovery"
        ]["semantic_data"]
        self.process.configure_data(data_root, semantic_data)
        canonical = context["canonical_definition_path"]
        path = (
            Path(canonical)
            if canonical is not None
            else context["work_root"] / "installed-product" / "Astrid-Runtime.xml"
        )
        if self.wrong_definition_path:
            path = context["work_root"] / "foreign" / path.name
        path.parent.mkdir(parents=True, exist_ok=True)
        action = (str(self.release0), "--service")
        working = str(product) if self.runner.os_name == "windows" else ""
        definition = (
            task_xml(
                action,
                context["principal"],
                working,
                extra_action=self.extra_source_action,
            )
            if self.runner.os_name == "windows"
            else json.dumps({"action": action, "principal": context["principal"]})
        )
        path.write_text(definition, encoding="utf-8")
        self.definition_path = path
        self.runner.configure(path, action, working)
        self.process.seed(100, self.release0)
        return native.ServiceSpec(
            path,
            file_digest(path),
            action,
            context["principal"],
            "product-installer",
            working,
        )

    def _identity(self, selected: str, executable: Path, process_char: str) -> dict:
        return {
            "selected_release": selected,
            "executable_path": str(executable.resolve()),
            "executable_sha256": file_digest(executable),
            "version_manifest_sha256": (
                "2" * 64 if selected == "release0" else self.candidate["manifest_sha256"]
            ),
            "process_identity_sha256": process_char * 64,
            "source_refs": (
                dict(failure.RELEASE0_REFS)
                if selected == "release0"
                else {"agent": self.candidate["agent"], "runtime": self.candidate["runtime"]}
            ),
            "running": True,
            "health": "pass",
        }

    def run(self, context, session: native.NativeSession) -> dict:
        assert self.release0 is not None
        assert self.candidate_executable is not None
        assert self.controller_executable is not None
        assert self.definition_path is not None
        if self.fail_at == "before-observe":
            raise RuntimeError("controlled failure before exact instance")
        values = copy.deepcopy(valid_observation(context["slot_id"], self.candidate)["scenarios"])
        values["interruption_recovery"]["semantic_data"] = copy.deepcopy(self.process.semantic_data)
        values["failed_candidate_rollback"]["semantic_data"] = copy.deepcopy(
            self.process.semantic_data
        )
        values["keep_data_uninstall"]["semantic_data"] = copy.deepcopy(self.process.semantic_data)
        for outcome in failure.STOP_OUTCOMES:
            values["stop_safety"]["cases"][outcome]["semantic_data"] = copy.deepcopy(
                self.process.semantic_data
            )
        values["interruption_recovery"]["terminal_identity"] = self._identity(
            "candidate", self.candidate_executable, "6"
        )
        pair = {
            "before": self._identity("release0", self.release0, "3"),
            "after": self._identity("release0", self.release0, "4"),
            "match": True,
        }
        values["failed_candidate_rollback"]["executable_identity"] = copy.deepcopy(pair)
        for outcome in failure.STOP_OUTCOMES:
            values["stop_safety"]["cases"][outcome]["executable_identity"] = copy.deepcopy(pair)
            values["stop_safety"]["cases"][outcome]["mutation_inventory"] = {
                "before": "candidate-selection-journal-current-bytes",
                "after": "candidate-selection-journal-current-bytes",
                "unchanged": True,
            }

        first_discovery = dict(self.process.active[100])
        if self.fail_at == "tamper-data-manifest":
            assert self.process.data_manifest_path is not None
            manifest = json.loads(self.process.data_manifest_path.read_text(encoding="utf-8"))
            manifest["entities"]["project"] = "f" * 64
            self.process.data_manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
            )
        first = session.observe("interruption_recovery", "before", first_discovery)
        if self.fail_at == "after-observe":
            raise RuntimeError("controlled failure after exact instance")
        if self.fail_at == "swapped-restart":
            self.runner.swap_action()
            session.restart(first, first_discovery)
        operation_id = values["interruption_recovery"]["operation_id"]
        phase = values["interruption_recovery"]["phase"]
        self.process.configure_journal(operation_id, phase)
        interrupted_controller = self.process.seed_controller(
            700,
            self.controller_executable,
            operation_id=operation_id,
            phase=phase,
        )
        if self.fail_at == "tamper-durable-journal":
            assert self.process.durable_journal_path is not None
            journal = json.loads(self.process.durable_journal_path.read_text(encoding="utf-8"))
            journal["phase"] = "foreign-phase"
            self.process.durable_journal_path.write_text(
                json.dumps(journal, sort_keys=True) + "\n", encoding="utf-8"
            )
        interrupted_authorization = session.before_external_controller_interruption(
            interrupted_controller
        )
        self.process.terminate(700)
        session.observe_external_controller_interrupted(interrupted_authorization)
        session.terminate_owned_runtime(first, first_discovery)
        after_interrupt = self.process.seed(101, self.candidate_executable)
        session.observe("interruption_recovery", "after", after_interrupt)
        recovery_controller = self.process.seed_controller(
            701,
            self.controller_executable,
            operation_id=operation_id,
            phase=phase,
        )
        recovery_authorization = session.before_external_recovery_controller(recovery_controller)
        self.process.terminate(701)
        session.observe_external_recovery_controller_complete(recovery_authorization)
        rollback = self.process.seed(102, self.release0)
        session.observe("failed_candidate_rollback", "after", rollback)
        current_discovery = self.process.seed(103, self.release0)
        current = session.observe("keep_data_uninstall", "before", current_discovery)
        for outcome in failure.STOP_OUTCOMES:
            session.observe(f"stop_safety.{outcome}", "before", current_discovery)
            session.observe(f"stop_safety.{outcome}", "after", current_discovery)
        if self.fail_at == "return-before-removal":
            return values
        session.before_product_removal(current, current_discovery)
        self.runner.product_uninstall(self.definition_path)
        session.observe_product_removal()
        return values


def collect(
    tmp_path: Path,
    slot_id: str,
    *,
    action_count: int = 1,
    instance_count: int = 1,
    zero_pid: bool = False,
    adversarial_argv: bool = False,
    removal_sticks: bool = False,
    respawn_after_removal: bool = False,
    respawn_candidate_after_removal: bool = False,
    delete_data_manifest_on_uninstall: bool = False,
    tamper_data_manifest_on_uninstall: bool = False,
    exported_xml_variant: str = "equivalent",
    deny_absence: bool = False,
    wrong_definition_path: bool = False,
    extra_source_action: bool = False,
    fail_at: str | None = None,
) -> tuple[dict, StatefulRunner, FakeProcessProbe]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    os_name = failure.RUNNER_PROVENANCE[failure.SLOT_CONTRACTS[slot_id]["runner"]][0]
    repos, heads = make_repos(tmp_path)
    process = FakeProcessProbe()
    process.deny_absence = deny_absence
    powershell = None
    if os_name == "windows":
        powershell = (
            tmp_path / "Windows" / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        )
        powershell.parent.mkdir(parents=True)
        powershell.write_bytes(b"PowerShell 5.1 fixture")
    runner = StatefulRunner(
        os_name,
        heads,
        process,
        action_count=action_count,
        instance_count=instance_count,
        zero_pid=zero_pid,
        adversarial_argv=adversarial_argv,
        removal_sticks=removal_sticks,
        respawn_after_removal=respawn_after_removal,
        respawn_candidate_after_removal=respawn_candidate_after_removal,
        delete_data_manifest_on_uninstall=delete_data_manifest_on_uninstall,
        tamper_data_manifest_on_uninstall=tamper_data_manifest_on_uninstall,
        exported_xml_variant=exported_xml_variant,
    )
    driver = FakeJourneyDriver(
        PROVISIONAL_CANDIDATE,
        process,
        runner,
        wrong_definition_path=wrong_definition_path,
        extra_source_action=extra_source_action,
        fail_at=fail_at,
    )
    observation = native.execute(
        slot_id,
        PROVISIONAL_CANDIDATE,
        FIXTURE,
        tmp_path / "run",
        app_repo=repos["app"],
        agent_repo=repos["agent"],
        runtime_repo=repos["runtime"],
        pinokio_repo=repos["pinokio"],
        environment=hosted_environment(tmp_path, os_name, powershell),
        _runner=runner,
        _process_probe=process,
        _journey_driver=driver,
        _fixture_powershell=powershell,
        _allow_platform_fixture=True,
    )
    return observation, runner, process


@pytest.mark.parametrize("slot_id", ["direct-macos", "direct-linux", "direct-windows"])
def test_product_canonical_native_lifecycle_is_collected_but_fixture_is_inadmissible(
    tmp_path: Path, slot_id: str
) -> None:
    observation, runner, _process = collect(tmp_path, slot_id)
    registration = observation["native_service"]["registration"]
    if slot_id == "direct-macos":
        assert registration["definition_path"].endswith(
            "/home/Library/LaunchAgents/haus.astrid.runtime.plist"
        )
    elif slot_id == "direct-linux":
        assert registration["definition_path"].endswith("/.config/systemd/user/astrid.service")
    else:
        assert registration["definition_path"].endswith("/run/installed-product/Astrid-Runtime.xml")
        assert all(
            Path(call[0]).is_absolute()
            for call in runner.calls
            if call[0].endswith("powershell.exe")
        )
        instances = observation["native_service"]["instances"]
        assert all(row["manager_instance_id"] for row in instances if row["state"] == "running")
        assert registration["source_xml_sha256"] != registration["exported_xml_sha256"]
    assert observation["native_service"]["removal"]["performed_by"] == "product-uninstall"
    assert observation["native_service"]["removal"]["owned_processes_after"] == 0
    assert observation["native_service"]["removal"]["observed_action_count"] == 2
    assert observation["native_service"]["removal"]["observed_incarnation_count"] == 4
    before_data = next(
        row
        for row in observation["native_service"]["instances"]
        if row["scenario"] == "keep_data_uninstall" and row["phase"] == "before"
    )
    cleanup_data = next(
        row
        for row in observation["native_service"]["instances"]
        if row["scenario"] == "keep_data_uninstall" and row["phase"] == "cleanup"
    )
    assert all(cleanup_data[name] == before_data[name] for name in native.DATA_IDENTITY_FIELDS)
    assert observation["collector_provenance"]["kind"] == "injected-platform-fixture"
    assert observation["collector_provenance"]["journey_driver"] == "injected-platform-fixture"
    assert not (tmp_path / "run" / "evidence").exists()
    assert native._native_structural_errors(observation) == []
    errors = native.native_observation_errors(observation)
    assert any("platform_session" in error for error in errors)
    assert runner.registered is False


def test_fixture_cannot_be_promoted_by_editing_scope_and_authentication(tmp_path: Path) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    collector = observation["collector_provenance"]
    collector.update(
        {
            "kind": "live-platform",
            "hosted_admissible": True,
            "environment": "live-process",
            "command_runner": "SubprocessCommandRunner",
            "process_probe": "LiveProcessProbe",
            "journey_driver": "registered-shared-driver",
        }
    )
    observation["execution_scope"] = native.HOSTED_SCOPE
    observation["collector_structural_sha256"] = hashlib.sha256(
        json.dumps(collector, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for item in observation["evidence_files"]:
        item["collector_kind"] = "live-platform"
        item["collector_structural_sha256"] = observation["collector_structural_sha256"]
        item["execution_scope"] = native.HOSTED_SCOPE
    errors = native.native_observation_errors(observation)
    assert any("platform_session" in error for error in errors)


def test_injected_collection_never_loads_fixed_platform_session_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_import = native.importlib.import_module

    def guarded_import(name):
        if name == native.FIXED_PLATFORM_SESSION_MODULE:
            raise AssertionError("fixture loaded hosted platform-session provider")
        return original_import(name)

    monkeypatch.setattr(native.importlib, "import_module", guarded_import)
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    collector = observation["collector_provenance"]
    collector.update(
        {
            "kind": "live-platform",
            "hosted_admissible": True,
            "environment": "live-process",
            "command_runner": "SubprocessCommandRunner",
            "process_probe": "LiveProcessProbe",
            "journey_driver": "registered-shared-driver",
        }
    )
    observation["execution_scope"] = native.HOSTED_SCOPE
    observation["collector_structural_sha256"] = hashlib.sha256(
        json.dumps(collector, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for item in observation["evidence_files"]:
        item["collector_kind"] = "live-platform"
        item["collector_structural_sha256"] = observation["collector_structural_sha256"]
        item["execution_scope"] = native.HOSTED_SCOPE
    errors = native.native_observation_errors(observation)
    assert any("platform_session" in error for error in errors)


def test_no_public_post_hoc_verifier_or_registrar_exists() -> None:
    assert not hasattr(native, "HOSTED_ADMISSION_VERIFIER")
    assert not hasattr(native, "register_hosted_admission_verifier")
    assert not hasattr(native, "HostedAdmissionVerifier")
    assert native.FIXED_PLATFORM_SESSION_MODULE == "scripts.m4_6_platform_session_provider"
    assert native.FIXED_PINOKIO_CUSTODY_MODULE == "scripts.m4_6_pinokio_custody_provider"


def _session_binding() -> native.PlatformSessionBinding:
    return native.PlatformSessionBinding(
        slot_id="direct-linux",
        mode="direct",
        owner_kind="native-service",
        os_name="linux",
        runner_image="ubuntu-24.04",
        shell="sh",
        runner_os="Linux",
        image_os="ubuntu24",
        os_build="Linux-test",
        repository_claim="haus/astrid",
        workflow_claim="journey",
        job_claim="m4-6-native",
        run_id_claim="1",
        run_attempt_claim="1",
        candidate=tuple(sorted(PROVISIONAL_CANDIDATE.items())),
        checkouts=(),
        collector_pid=123,
        collector_executable="/usr/bin/python3",
        collector_executable_sha256="a" * 64,
        product_protocol="astrid-m4-6-product-driver/v1",
        product_command=("/product-driver",),
        product_command_cwd="/work",
        artifacts=(),
        owner_principal="runner",
        owner_target="astrid.service",
    )


def _custody_binding() -> native.CustodyBinding:
    return native.CustodyBinding(
        slot_id="direct-linux",
        owner_kind="native-service",
        principal="runner",
        target="astrid.service",
        definition_path="/astrid.service",
        definition_sha256="6" * 64,
        action=("/runtime", "--service"),
        action_sha256="7" * 64,
        immutable_actions=(),
        resolved_action_identity_sha256="8" * 64,
        removal_plan=native.RemovalPlan(
            operation="product-uninstall",
            keep_data=True,
            remove_paths=("/astrid.service", "/runtime"),
            preserve_paths=("/data",),
            expected_postconditions=(
                "owner-target-absent",
                "all-authorized-incarnations-absent",
                "no-owned-action-processes",
                "preserved-data-identity-unchanged",
            ),
        ),
    )


@pytest.mark.parametrize(
    "remove_paths,preserve_paths,error",
    [
        (("/program",), ("/program/data",), "overlap by ancestry"),
        (("/program/../program",), ("/data",), "canonical and normalized"),
    ],
)
def test_removal_plan_rejects_ancestry_overlap_and_lexical_aliases(
    remove_paths: tuple[str, ...], preserve_paths: tuple[str, ...], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        native.RemovalPlan(
            operation="product-uninstall",
            keep_data=True,
            remove_paths=remove_paths,
            preserve_paths=preserve_paths,
            expected_postconditions=(
                "owner-target-absent",
                "all-authorized-incarnations-absent",
                "no-owned-action-processes",
                "preserved-data-identity-unchanged",
            ),
        )


def test_pinokio_removal_census_preserves_controller_descendant_relationships() -> None:
    targets = ("pinokio-controller", "/pinokio/install")
    controller = native.RemovalProcessBinding(
        role="pinokio-controller",
        target="pinokio-controller",
        pid=41,
        process_birth_id="controller-birth",
        parent_role="pinokio-direct-custody",
        parent_pid=None,
        parent_process_birth_id=None,
        executable_path="/usr/bin/node",
        executable_sha256="a" * 64,
        action=("/usr/bin/node", "/pinokio/start.js"),
        discovery_identity=(("controller_instance_id", "controller-1"),),
        removal_targets=targets,
    )
    runtime = native.RemovalProcessBinding(
        role="runtime-descendant",
        target="pinokio-controller",
        pid=42,
        process_birth_id="runtime-birth",
        parent_role="pinokio-controller",
        parent_pid=41,
        parent_process_birth_id="controller-birth",
        executable_path="/pinokio/install/runtime",
        executable_sha256="b" * 64,
        action=("/pinokio/install/runtime", "--service"),
        discovery_identity=(("runtime_instance_id", "runtime-1"),),
        removal_targets=targets,
    )
    census = native.RemovalCensus(
        manager_target="pinokio-controller",
        manager_instance_id="pinokio-session-1",
        registration=(("checkout_sha256", "c" * 64),),
        controller_identity=(
            ("pid", 41),
            ("process_birth_id", "controller-birth"),
            ("executable_sha256", "a" * 64),
        ),
        processes=(controller, runtime),
        prior_incarnations=(),
        removal_targets=targets,
    )

    payload = native._removal_census_payload(census)
    assert [row["role"] for row in payload["processes"]] == [
        "pinokio-controller",
        "runtime-descendant",
    ]
    assert payload["processes"][1]["parent_pid"] == 41
    assert payload["processes"][1]["parent_process_birth_id"] == "controller-birth"
    assert payload["processes"][1]["executable_sha256"] == "b" * 64


def test_pinokio_structural_validation_accepts_multi_argv_census_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native, "_scenario_instance_errors", lambda _observation: [])
    checkout = "/candidate/pinokio"
    action_files = {
        relative: {
            "path": str(Path(checkout) / relative),
            "sha256": digest * 64,
        }
        for relative, digest in (
            ("pinokio/start.js", "a"),
            ("pinokio/stop.js", "b"),
            ("pinokio/update.js", "c"),
        )
    }
    controller_action = ["/usr/bin/node", action_files["pinokio/start.js"]["path"], "--headless"]
    runtime_action = ["/pinokio/install/runtime", "--service"]
    registration = {
        "principal": "runner",
        "target": "pinokio-controller",
        "checkout_path": checkout,
        "checkout_sha": "d" * 40,
        "definition_owner": "pinokio",
        "definition_path": action_files["pinokio/start.js"]["path"],
        "definition_sha256": "a" * 64,
        "manager_definition_sha256": "a" * 64,
        "resolved_action_identity_sha256": "e" * 64,
        "action": controller_action,
        "action_sha256": native._sha256_bytes(native._canonical(controller_action)),
        "action_files": action_files,
    }
    current = _removal_current()
    keep_before = {
        "scenario": "keep_data_uninstall",
        "phase": "before",
        **current,
    }
    action_rows = [controller_action, runtime_action]
    incarnation_rows = [
        {"pid": 41, "process_birth_id": "controller-birth"},
        {"pid": 42, "process_birth_id": "runtime-birth"},
    ]
    data_identity = native._data_identity(current)
    raw = [
        {
            "label": "pinokio-controller-query",
            "argv_sha256": "1" * 64,
            "stdout_sha256": "2" * 64,
            "stderr_sha256": "3" * 64,
        },
        {
            "label": "pinokio-controller-query",
            "argv_sha256": "4" * 64,
            "stdout_sha256": "5" * 64,
            "stderr_sha256": "6" * 64,
        },
    ]
    collector = {"kind": "live-platform"}
    collector_sha = "7" * 64
    observation = {
        "candidate_checkouts": {"pinokio": {"path": checkout, "sha": registration["checkout_sha"]}},
        "execution_scope": native.HOSTED_SCOPE,
        "collector_provenance": collector,
        "collector_structural_sha256": collector_sha,
        "evidence_files": [
            {
                "path": f"evidence/{scenario}.json",
                "scenario": scenario,
                "kind": "machine-observation",
                "sha256": "8" * 64,
                "collector_kind": "live-platform",
                "collector_structural_sha256": collector_sha,
                "execution_scope": native.HOSTED_SCOPE,
            }
            for scenario in failure.SCENARIOS
        ],
        "native_service": {
            "manager": "pinokio",
            "capability": "pass",
            "manager_version": "test",
            "target": "pinokio-controller",
            "principal": "runner",
            "registration": registration,
            "instances": [keep_before],
            "removal": {
                "definition_absent": False,
                "manager_target_absent": True,
                "owned_processes_after": 0,
                "all_authorized_incarnations_absent": True,
                "observed_action_count": 2,
                "observed_incarnation_count": 2,
                "post_data_identity": data_identity,
                "post_data_identity_sha256": native._sha256_bytes(native._canonical(data_identity)),
                "data_identity_unchanged": True,
                "observed": True,
                "requested": True,
                "owned_target_revalidated": True,
                "performed_by": "product-uninstall",
                "authorized_identity_sha256": native._sha256_bytes(
                    native._canonical(native._instance_identity(keep_before))
                ),
                "authorized_registration_sha256": native._sha256_bytes(
                    native._canonical(registration)
                ),
                "authorized_live_census_sha256": "9" * 64,
                "authorized_actions_sha256": native._sha256_bytes(native._canonical(action_rows)),
                "authorized_incarnations_sha256": native._sha256_bytes(
                    native._canonical(incarnation_rows)
                ),
                "authorized_data_identity_sha256": native._sha256_bytes(
                    native._canonical(data_identity)
                ),
                "authorization_raw_index": 0,
                "absence_raw_index": 1,
            },
            "raw_observations": raw,
            "raw_observations_sha256": native._sha256_bytes(native._canonical(raw)),
        },
    }

    singleton_rows = [[row[0]] for row in action_rows]
    assert observation["native_service"]["removal"]["authorized_actions_sha256"] != (
        native._sha256_bytes(native._canonical(singleton_rows))
    )
    errors = native._pinokio_custody_structural_errors(observation)
    assert errors == []

    invalid_counts = copy.deepcopy(observation)
    invalid_counts["native_service"]["removal"]["observed_action_count"] = 3
    assert any(
        "authorization/absence binding differs" in error
        for error in native._pinokio_custody_structural_errors(invalid_counts)
    )


class _TwoPhasePlatformSession:
    def __init__(self, events: list[str], *, fail_final: bool = False) -> None:
        self.events = events
        self.fail_final = fail_final
        self.request = None

    def bind_exact_custody(self, binding, owner):
        del binding, owner
        self.events.append("bind")

    def authorize_exact_removal(self, request, owner):
        del owner
        assert request.session.slot_id == "direct-linux"
        assert request.session.owner_kind == "native-service"
        assert request.operation == "product-uninstall"
        assert request.keep_data is True
        assert request.remove_paths == ("/astrid.service", "/runtime")
        assert request.preserve_paths == ("/data",)
        assert request.custody.removal_plan is not None
        assert request.custody.removal_plan.remove_paths == request.remove_paths
        assert request.live_census.manager_target == "astrid.service"
        assert request.live_census.processes[0].executable_sha256 == "1" * 64
        self.request = request
        self.events.append("authorize")

    def finalize_after_removal(self, observation, owner):
        assert observation.slot_id == "direct-linux"
        assert self.request is not None
        authorized = {
            "observed_actions": [list(row) for row in self.request.observed_actions],
            "observed_incarnations": [
                {"pid": pid, "process_birth_id": birth_id}
                for pid, birth_id in self.request.observed_incarnations
            ],
            "data_identity": dict(self.request.data_identity),
        }
        self.events.append("platform-live-reobserve")
        second_absence = owner.observe_absent(authorized)
        assert tuple(sorted(second_absence.items())) == observation.absence
        self.events.append("finalize")
        if self.fail_final:
            raise RuntimeError("controlled final re-observation mismatch")

    def abort(self, reason):
        assert reason
        self.events.append("abort")


def test_authenticated_session_opens_before_private_scratch_creation(tmp_path: Path) -> None:
    events: list[str] = []
    session = _TwoPhasePlatformSession(events)
    work_root = tmp_path / "private" / "run"
    validated = native._validated_work_root(
        work_root,
        {"RUNNER_TEMP": str(tmp_path)},
    )
    native.DataProbe(validated)
    native.DurableJournalProbe(validated)

    class Provider:
        @staticmethod
        def open_authenticated_session(binding, owner):
            assert binding == _session_binding()
            assert owner is sentinel_owner
            assert not work_root.exists()
            events.append("open")
            return session

    sentinel_owner = object()
    opened = native._open_platform_session_and_create_work_root(
        Provider(),
        _session_binding(),
        sentinel_owner,
        validated,
    )

    assert opened is session
    assert events == ["open"]
    assert work_root.is_dir()


class _RemovalOwner:
    os_name = "linux"

    def __init__(self, current: dict, events: list[str]) -> None:
        self.current = current
        self.events = events
        self.raw: list[dict] = []

    def registration(self):
        return {
            "action": ["/runtime", "--service"],
            "target": "astrid.service",
            "principal": "runner",
            "definition_path": "/astrid.service",
        }

    def revalidate(self, expected, discovery):
        assert expected == self.current and discovery == self.current
        self.raw.append({"label": "systemd-query"})
        return dict(self.current)

    @staticmethod
    def _same_instance(left, right):
        return native._instance_identity(left) == native._instance_identity(right)

    def removal_census(self, current, instances, custody):
        del instances
        assert custody.removal_plan is not None
        targets = ("astrid.service", *custody.removal_plan.remove_paths)
        return native.RemovalCensus(
            manager_target="astrid.service",
            manager_instance_id=current["manager_instance_id"],
            registration=tuple(sorted(self.registration().items())),
            controller_identity=(
                ("owner_kind", "native-service"),
                ("manager", "systemd-user"),
                ("target", "astrid.service"),
                ("manager_instance_id", current["manager_instance_id"]),
            ),
            processes=(
                native.RemovalProcessBinding(
                    role="native-runtime-service",
                    target="astrid.service",
                    pid=current["pid"],
                    process_birth_id=current["process_birth_id"],
                    parent_role="systemd-user-owned-service",
                    parent_pid=None,
                    parent_process_birth_id=None,
                    executable_path=current["executable_path"],
                    executable_sha256=current["executable_sha256"],
                    action=("/runtime", "--service"),
                    discovery_identity=tuple(
                        (name, current.get(name)) for name in native.INSTANCE_IDENTITY_FIELDS
                    ),
                    removal_targets=targets,
                ),
            ),
            prior_incarnations=(),
            removal_targets=targets,
        )

    def observe_absent(self, authorized):
        assert authorized["observed_actions"]
        self.events.append("live-absence")
        self.raw.append({"label": "systemd-query"})
        return {
            "definition_absent": True,
            "manager_target_absent": True,
            "owned_processes_after": 0,
            "all_authorized_incarnations_absent": True,
            "observed_action_count": len(authorized["observed_actions"]),
            "observed_incarnation_count": len(authorized["observed_incarnations"]),
            "post_data_identity": authorized["data_identity"],
            "post_data_identity_sha256": hashlib.sha256(
                json.dumps(
                    authorized["data_identity"], sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
            "data_identity_unchanged": True,
            "observed": True,
        }


def _removal_current() -> dict:
    return {
        "state": "running",
        "manager_instance_id": "manager",
        "pid": 321,
        "process_birth_id": "birth",
        "runtime_instance_id": "runtime",
        "executable_path": "/runtime",
        "executable_sha256": "1" * 64,
        "data_root": "/data",
        "data_identity_sha256": "2" * 64,
        "workspace_identity": "workspace",
        "semantic_data_sha256": "3" * 64,
        "data_manifest_path": "/data/native-data-identity.json",
        "data_manifest_sha256": "4" * 64,
        "data_root_identity_sha256": "5" * 64,
    }


def test_two_phase_session_authorizes_before_removal_and_finalizes_before_return() -> None:
    events: list[str] = []
    current = _removal_current()
    owner = _RemovalOwner(current, events)
    platform = _TwoPhasePlatformSession(events)
    session = native.NativeSession(
        owner,
        platform_session=platform,
        platform_binding=_session_binding(),
        custody_binding=_custody_binding(),
    )
    session.instances.append({"scenario": "keep_data_uninstall", "phase": "before", **current})

    session.before_product_removal(current, current)
    events.append("product-remove")
    result = session.observe_product_removal()

    assert events == [
        "authorize",
        "product-remove",
        "live-absence",
        "platform-live-reobserve",
        "live-absence",
        "finalize",
    ]
    assert result["observed"] is True
    assert "authorization_sha256" not in result
    with pytest.raises(RuntimeError, match="already consumed"):
        session.observe_product_removal()


def test_removal_census_accounts_for_every_recorded_process_incarnation() -> None:
    events: list[str] = []
    current = _removal_current()
    prior = {
        **current,
        "pid": 320,
        "process_birth_id": "prior-birth",
        "runtime_instance_id": "prior-runtime",
    }
    owner = _RemovalOwner(current, events)
    platform = _TwoPhasePlatformSession(events)
    session = native.NativeSession(
        owner,
        platform_session=platform,
        platform_binding=_session_binding(),
        custody_binding=_custody_binding(),
    )
    session.instances.extend(
        [
            {"scenario": "interruption_recovery", "phase": "before", **prior},
            {"scenario": "keep_data_uninstall", "phase": "before", **current},
        ]
    )

    with pytest.raises(
        RuntimeError,
        match="does not account for every recorded process incarnation",
    ):
        session.before_product_removal(current, current)
    assert events == []


def test_two_phase_session_returns_no_removal_when_final_reobservation_fails() -> None:
    events: list[str] = []
    current = _removal_current()
    owner = _RemovalOwner(current, events)
    platform = _TwoPhasePlatformSession(events, fail_final=True)
    session = native.NativeSession(
        owner,
        platform_session=platform,
        platform_binding=_session_binding(),
        custody_binding=_custody_binding(),
    )
    session.instances.append({"scenario": "keep_data_uninstall", "phase": "before", **current})
    session.before_product_removal(current, current)

    with pytest.raises(RuntimeError, match="final re-observation mismatch"):
        session.observe_product_removal()
    assert session.removal is None
    assert events == [
        "authorize",
        "live-absence",
        "platform-live-reobserve",
        "live-absence",
        "finalize",
    ]
    with pytest.raises(RuntimeError, match="no exact platform authorization"):
        session.safe_finalize()
    native._abort_platform_session(platform, RuntimeError("controlled"))
    assert events[-1] == "abort"


def test_two_phase_session_rejects_boolean_or_handle_shaped_authorization() -> None:
    class BooleanAuthorizationSession(_TwoPhasePlatformSession):
        def authorize_exact_removal(self, request, owner):
            super().authorize_exact_removal(request, owner)
            return True

    events: list[str] = []
    current = _removal_current()
    owner = _RemovalOwner(current, events)
    platform = BooleanAuthorizationSession(events)
    session = native.NativeSession(
        owner,
        platform_session=platform,
        platform_binding=_session_binding(),
        custody_binding=_custody_binding(),
    )
    session.instances.append({"scenario": "keep_data_uninstall", "phase": "before", **current})

    with pytest.raises(RuntimeError, match="must return no handle or verdict"):
        session.before_product_removal(current, current)
    assert session.removal is None


def assign_path(value: dict | list, path: tuple[object, ...], replacement: object) -> None:
    cursor = value
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = replacement


@pytest.mark.parametrize(
    "path,replacement,error_fragment",
    [
        (("native_service", "manager"), "launchd", "manager"),
        (("native_service", "capability"), "unknown", "capability"),
        (("native_service", "manager_version"), "", "manager_version"),
        (("native_service", "target"), "foreign.service", "target"),
        (("native_service", "principal"), "foreign", "principal"),
        (
            ("native_service", "registration", "definition_path"),
            "/tmp/foreign.service",
            "definition_path",
        ),
        (("owner", "slot_id"), "direct-macos", "owner.slot_id"),
        (("owner", "mode"), "pinokio-backend", "owner.mode"),
        (("owner", "managed_by"), "foreground", "managed_by"),
        (("owner", "foreground_fallback"), True, "foreground_fallback"),
        (("owner", "checkout_sha256"), "0" * 64, "checkout_sha256"),
        (("hosted_authentication", "provider"), "local", "provider"),
        (("hosted_authentication", "CI"), "false", "CI"),
        (("hosted_authentication", "GITHUB_ACTIONS"), "false", "GITHUB_ACTIONS"),
        (
            ("hosted_authentication", "RUNNER_ENVIRONMENT"),
            "self-hosted",
            "RUNNER_ENVIRONMENT",
        ),
        (
            ("hosted_authentication", "GITHUB_SERVER_URL"),
            "https://example.invalid",
            "GITHUB_SERVER_URL",
        ),
        (("hosted_authentication", "GITHUB_RUN_ID"), "", "GITHUB_RUN_ID"),
        (
            ("hosted_authentication", "GITHUB_RUN_ATTEMPT"),
            "",
            "GITHUB_RUN_ATTEMPT",
        ),
        (("hosted_authentication", "GITHUB_JOB"), "", "GITHUB_JOB"),
        (("hosted_authentication", "GITHUB_REPOSITORY"), "", "GITHUB_REPOSITORY"),
        (("hosted_authentication", "ImageVersion"), "fixture", "ImageVersion"),
        (("hosted_authentication", "shell"), "powershell.exe", "shell"),
        (("hosted_authentication", "RUNNER_OS"), "Windows", "RUNNER_OS"),
        (("hosted_authentication", "ImageOS"), "win22", "ImageOS"),
        (("hosted_authentication", "os_build"), "", "os_build"),
        (("os_build",), "foreign-build", "os_build"),
        (("runner_provenance", "RUNNER_OS"), "macOS", "runner_provenance"),
        (("runner_provenance", "ImageOS"), "macos14", "runner_provenance"),
        (("runner_provenance", "ImageVersion"), "foreign", "runner_provenance"),
        (("manifest_sha256",), "0" * 64, "manifest_sha256"),
        (
            ("release0_fixture", "fixture_sums_sha256"),
            "0" * 64,
            "release0_fixture",
        ),
        (
            ("candidate_checkouts", "agent", "sha"),
            "0" * 40,
            "checkout_sha256",
        ),
        (
            ("native_service", "registration", "action_sha256"),
            "0" * 64,
            "registration",
        ),
        (
            ("native_service", "registration", "definition_sha256"),
            "0" * 64,
            "definition",
        ),
        (
            ("native_service", "registration", "manager_definition_sha256"),
            "0" * 64,
            "manager definition",
        ),
        (
            ("native_service", "removal", "authorized_registration_sha256"),
            "0" * 64,
            "authorization",
        ),
        (
            ("native_service", "removal", "authorized_actions_sha256"),
            "0" * 64,
            "authorization",
        ),
        (
            ("native_service", "removal", "observed_action_count"),
            1,
            "authorization",
        ),
        (
            (
                "native_service",
                "removal",
                "post_data_identity",
                "data_identity_sha256",
            ),
            "0" * 64,
            "authorization",
        ),
        (
            ("native_service", "removal", "authorized_identity_sha256"),
            "0" * 64,
            "authorization",
        ),
        (
            ("native_service", "instances", 0, "manager_instance_id"),
            "foreign-instance",
            "InvocationID",
        ),
        (
            ("native_service", "removal", "authorization_raw_index"),
            0,
            "raw authorization",
        ),
        (
            ("native_service", "removal", "owned_processes_after"),
            1,
            "absence",
        ),
        (
            ("native_service", "removal", "manager_target_absent"),
            False,
            "manager_target_absent",
        ),
        (
            (
                "scenarios",
                "interruption_recovery",
                "terminal_identity",
                "executable_sha256",
            ),
            "0" * 64,
            "executable binding",
        ),
        (
            ("native_service", "instances", 0, "data_identity_sha256"),
            "0" * 64,
            "data identity",
        ),
        (("evidence_files", 0, "collector_kind"), "foreign", "evidence_files"),
        (("evidence_files", 0, "execution_scope"), "local", "evidence_files"),
        (("collector_structural_sha256",), "0" * 64, "collector_structural"),
        (
            ("native_service", "raw_observations", 0, "command"),
            "foreign-command",
            "raw_observations_sha256",
        ),
        (
            ("native_service", "raw_observations", 0, "argc"),
            99,
            "raw_observations_sha256",
        ),
        (
            ("native_service", "raw_observations", 0, "returncode"),
            99,
            "raw_observations_sha256",
        ),
        (
            ("native_service", "raw_observations", 0, "stdout_bytes"),
            99,
            "raw_observations_sha256",
        ),
        (
            ("native_service", "raw_observations", 0, "stderr_bytes"),
            99,
            "raw_observations_sha256",
        ),
        (
            ("native_service", "raw_observations", 0, "argv_sha256"),
            "0" * 64,
            "raw_observations_sha256",
        ),
        (
            ("native_service", "raw_observations", 0, "stdout_sha256"),
            "0" * 64,
            "raw_observations_sha256",
        ),
        (
            ("native_service", "raw_observations", 0, "stderr_sha256"),
            "0" * 64,
            "raw_observations_sha256",
        ),
        (
            ("native_service", "raw_observations_sha256"),
            "0" * 64,
            "raw_observations_sha256",
        ),
    ],
)
def test_structural_validator_rejects_mutated_native_fields(
    tmp_path: Path,
    path: tuple[object, ...],
    replacement: object,
    error_fragment: str,
) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    assign_path(observation, path, replacement)
    errors = native._native_structural_errors(observation)
    assert error_fragment in "; ".join(errors)


def test_structural_validator_pins_exact_slot_tuple_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    monkeypatch.setattr(
        failure,
        "REQUIRED_SLOT_IDS",
        tuple(reversed(failure.REQUIRED_SLOT_IDS)),
    )
    assert any(
        "exact nine slot tuple/order" in error
        for error in native._native_structural_errors(observation)
    )


def test_structural_validator_requires_raw_manager_operations(tmp_path: Path) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    raw = observation["native_service"]["raw_observations"]
    next(row for row in raw if row["label"] == "owned-runtime-termination")["label"] = (
        "foreign-operation"
    )
    assert any(
        "required manager operations" in error
        for error in native._native_structural_errors(observation)
    )


@pytest.mark.parametrize(
    "field,replacement",
    [("command", "foreign"), ("argc", 99), ("returncode", 99)],
)
def test_raw_command_result_contract_survives_recomputed_structural_hash(
    tmp_path: Path, field: str, replacement: object
) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    raw = observation["native_service"]["raw_observations"]
    raw[0][field] = replacement
    observation["native_service"]["raw_observations_sha256"] = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    errors = native._native_structural_errors(observation)
    assert any("result metadata differs" in error for error in errors)


def test_closed_world_rejects_scenario_token_smuggling(tmp_path: Path) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    observation["scenarios"]["interruption_recovery"]["TOKEN"] = "do-not-serialize"
    errors = native._native_structural_errors(observation)
    assert any("scenarios.interruption_recovery: unknown fields" in error for error in errors)


def test_closed_world_rejects_raw_parsed_token_with_recomputed_hash(tmp_path: Path) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    raw = observation["native_service"]["raw_observations"]
    query = next(row for row in raw if row["label"] == "systemd-query" and "parsed" in row)
    query["parsed"]["TOKEN"] = "do-not-serialize"
    observation["native_service"]["raw_observations_sha256"] = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    errors = native._native_structural_errors(observation)
    assert any("parsed: unknown fields" in error for error in errors)


def test_closed_world_rejects_unknown_environment_field(tmp_path: Path) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    observation["unexpected_environment"] = {"TOKEN": "do-not-serialize"}
    errors = native._native_structural_errors(observation)
    assert any("observation: unknown fields" in error for error in errors)


def test_closed_world_rejects_mapping_in_scenario_scalar_leaf(tmp_path: Path) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    observation["scenarios"]["interruption_recovery"]["first_request"]["journal_state"] = {
        "TOKEN": "do-not-serialize"
    }
    errors = native._native_structural_errors(observation)
    assert any("first_request.journal_state: exact str leaf" in error for error in errors)


def test_closed_world_rejects_mapping_in_registration_scalar_after_rehash(
    tmp_path: Path,
) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    registration = observation["native_service"]["registration"]
    registration["working_directory"] = {"TOKEN": "do-not-serialize"}
    observation["native_service"]["removal"]["authorized_registration_sha256"] = hashlib.sha256(
        json.dumps(registration, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    errors = native._native_structural_errors(observation)
    assert any("registration.working_directory: exact str leaf" in error for error in errors)


def test_closed_world_rejects_mapping_in_windows_instance_pid_after_rehash(
    tmp_path: Path,
) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-windows")
    raw = observation["native_service"]["raw_observations"]
    query = next(
        row
        for row in raw
        if row["label"] == "task-scheduler-query" and row.get("parsed", {}).get("instances")
    )
    query["parsed"]["instances"][0]["pid"] = {"TOKEN": "do-not-serialize"}
    observation["native_service"]["raw_observations_sha256"] = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    errors = native._native_structural_errors(observation)
    assert any("parsed.instances[0].pid: exact int leaf" in error for error in errors)


@pytest.mark.parametrize(
    "path,error_fragment",
    [
        (
            ("scenarios", "interruption_recovery", "first_request"),
            "first_request: exact object required",
        ),
        (
            (
                "scenarios",
                "interruption_recovery",
                "terminal_identity",
                "source_refs",
            ),
            "source_refs: exact object required",
        ),
    ],
)
def test_closed_world_rejects_non_object_container_boundaries(
    tmp_path: Path, path: tuple[object, ...], error_fragment: str
) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    assign_path(observation, path, [{"TOKEN": "do-not-serialize"}])
    errors = native._native_structural_errors(observation)
    assert any(error_fragment in error for error in errors)


def test_closed_world_rejects_non_object_windows_instance_after_rehash(
    tmp_path: Path,
) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-windows")
    raw = observation["native_service"]["raw_observations"]
    query = next(
        row
        for row in raw
        if row["label"] == "task-scheduler-query" and row.get("parsed", {}).get("instances")
    )
    query["parsed"]["instances"][0] = [{"TOKEN": "do-not-serialize"}]
    observation["native_service"]["raw_observations_sha256"] = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    errors = native._native_structural_errors(observation)
    assert any("parsed.instances[0]: exact object required" in error for error in errors)


def receipt_observation(tmp_path: Path) -> dict:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    return failure.create_receipt("direct-linux", PROVISIONAL_CANDIDATE, observation)


def test_closed_world_accepts_exact_receipt_extension(tmp_path: Path) -> None:
    receipt = receipt_observation(tmp_path)
    assert native._native_structural_errors(receipt) == []


def test_closed_world_requires_complete_receipt_when_any_receipt_field_is_present(
    tmp_path: Path,
) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    observation["schema_version"] = failure.SCHEMA_VERSION
    errors = native._native_structural_errors(observation)
    assert any("exact recorded receipt fields required" in error for error in errors)


@pytest.mark.parametrize(
    "path,replacement,error_fragment",
    [
        (("schema_version",), True, "schema_version: exact int leaf"),
        (("receipt_kind",), [], "receipt_kind: exact str leaf"),
        (("receipt_state",), 1, "receipt_state: exact str leaf"),
        (("status",), {}, "status: exact str leaf"),
        (("status",), "unknown", "status: pass/fail string required"),
        (("verification", "state"), [], "verification.state: exact str leaf"),
        (
            ("verification", "state"),
            "unknown",
            "verification.state: verified-pass/verified-fail string required",
        ),
        (("verification", "verifier"), {"TOKEN": "secret"}, "verifier: exact str leaf"),
        (("verification", "verifier"), ["nested"], "verifier: exact str leaf"),
        (("verification", "verifier"), 1, "verifier: exact str leaf"),
        (
            ("verification", "failed_assertions"),
            [{"TOKEN": "secret"}],
            "failed_assertions: exact string list required",
        ),
        (
            ("verification", "failed_assertions"),
            [["nested"]],
            "failed_assertions: exact string list required",
        ),
        (
            ("checks", "interruption_recovery"),
            True,
            "checks.interruption_recovery: pass/fail string required",
        ),
        (
            ("checks", "failed_candidate_rollback"),
            {"TOKEN": "secret"},
            "checks.failed_candidate_rollback: pass/fail string required",
        ),
        (
            ("checks", "keep_data_uninstall"),
            "unknown",
            "checks.keep_data_uninstall: pass/fail string required",
        ),
    ],
)
def test_closed_world_rejects_receipt_scalar_and_nested_smuggling(
    tmp_path: Path,
    path: tuple[object, ...],
    replacement: object,
    error_fragment: str,
) -> None:
    receipt = receipt_observation(tmp_path)
    assign_path(receipt, path, replacement)
    errors = native._native_structural_errors(receipt)
    assert any(error_fragment in error for error in errors)


@pytest.mark.parametrize("container", ["verification", "checks"])
def test_closed_world_rejects_receipt_nested_extra_keys(tmp_path: Path, container: str) -> None:
    receipt = receipt_observation(tmp_path)
    receipt[container]["TOKEN"] = "secret"
    errors = native._native_structural_errors(receipt)
    assert any(f"{container}: unknown fields" in error for error in errors)


def test_closed_world_rejects_receipt_top_level_extra_key(tmp_path: Path) -> None:
    receipt = receipt_observation(tmp_path)
    receipt["TOKEN"] = "secret"
    errors = native._native_structural_errors(receipt)
    assert any("observation: unknown fields" in error for error in errors)


@pytest.mark.parametrize(
    "path,replacement",
    [
        (
            ("native_service", "controller_interruption", "runtime_operation_id"),
            "foreign-operation",
        ),
        (
            ("native_service", "controller_interruption", "durable_journal_sha256"),
            "0" * 64,
        ),
        (
            (
                "native_service",
                "controller_interruption",
                "interrupted_controller",
                "controller_sha256",
            ),
            "0" * 64,
        ),
        (
            (
                "native_service",
                "controller_interruption",
                "recovery_controller",
                "controller",
                "controller_instance_id",
            ),
            "controller-instance-700",
        ),
    ],
)
def test_controller_observation_mutations_cannot_pass(
    tmp_path: Path, path: tuple[object, ...], replacement: object
) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    assign_path(observation, path, replacement)
    assert any(
        "interruption_recovery" in error for error in native._native_structural_errors(observation)
    )


def test_same_controller_pid_and_birth_cannot_be_relabelled_as_recovery(
    tmp_path: Path,
) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    controller = observation["native_service"]["controller_interruption"]
    interrupted = controller["interrupted_controller"]["controller"]
    recovery = controller["recovery_controller"]
    recovery["controller"]["pid"] = interrupted["pid"]
    recovery["controller"]["process_birth_id"] = interrupted["process_birth_id"]
    recovery["controller_sha256"] = hashlib.sha256(
        json.dumps(recovery["controller"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    observation["scenarios"]["interruption_recovery"]["native_controller_interruption"] = (
        copy.deepcopy(controller)
    )
    errors = native._native_structural_errors(observation)
    assert any("not a distinct process" in error for error in errors)


def test_recovery_controller_cannot_reuse_logical_instance_id(
    tmp_path: Path,
) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    controller = observation["native_service"]["controller_interruption"]
    interrupted = controller["interrupted_controller"]["controller"]
    recovery = controller["recovery_controller"]
    recovery["controller"]["controller_instance_id"] = interrupted["controller_instance_id"]
    recovery["controller_sha256"] = hashlib.sha256(
        json.dumps(recovery["controller"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    observation["scenarios"]["interruption_recovery"]["native_controller_interruption"] = (
        copy.deepcopy(controller)
    )
    errors = native._native_structural_errors(observation)
    assert any("not a distinct process" in error for error in errors)


def test_same_runtime_pid_and_birth_cannot_be_relabelled_as_recovery(
    tmp_path: Path,
) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    rows = observation["native_service"]["instances"]
    before = next(
        row
        for row in rows
        if row["scenario"] == "interruption_recovery" and row["phase"] == "before"
    )
    after = next(
        row
        for row in rows
        if row["scenario"] == "interruption_recovery" and row["phase"] == "after"
    )
    after["pid"] = before["pid"]
    after["process_birth_id"] = before["process_birth_id"]
    errors = native._native_structural_errors(observation)
    assert any("did not produce a new Runtime instance" in error for error in errors)


def test_recovered_runtime_cannot_reuse_logical_instance_id(tmp_path: Path) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    rows = observation["native_service"]["instances"]
    before = next(
        row
        for row in rows
        if row["scenario"] == "interruption_recovery" and row["phase"] == "before"
    )
    after = next(
        row
        for row in rows
        if row["scenario"] == "interruption_recovery" and row["phase"] == "after"
    )
    after["runtime_instance_id"] = before["runtime_instance_id"]
    errors = native._native_structural_errors(observation)
    assert any("did not produce a new Runtime instance" in error for error in errors)


def test_same_runtime_pid_and_executable_cannot_hide_data_identity_mismatch(
    tmp_path: Path,
) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-linux")
    row = observation["native_service"]["instances"][0]
    original_pid = row["pid"]
    original_executable = row["executable_sha256"]
    row["data_identity_sha256"] = "0" * 64
    errors = native._native_structural_errors(observation)
    assert row["pid"] == original_pid
    assert row["executable_sha256"] == original_executable
    assert any("scenario data identity differs" in error for error in errors)


@pytest.mark.parametrize("action_count,instance_count", [(2, 1), (1, 0), (1, 2)])
def test_windows_requires_one_action_and_one_running_com_instance(
    tmp_path: Path, action_count: int, instance_count: int
) -> None:
    with pytest.raises(RuntimeError, match="Scheduled Task|running Scheduled Task"):
        collect(
            tmp_path,
            "direct-windows",
            action_count=action_count,
            instance_count=instance_count,
        )


@pytest.mark.parametrize(
    "variant",
    ["changed-action", "changed-principal", "multiple-actions", "changed-settings"],
)
def test_windows_rejects_changed_exported_xml_semantics(tmp_path: Path, variant: str) -> None:
    with pytest.raises(RuntimeError, match="Scheduled Task XML|XML identity"):
        collect(tmp_path, "direct-windows", exported_xml_variant=variant)


def test_windows_rejects_non_exec_source_action_before_registration(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="requires one action"):
        collect(tmp_path, "direct-windows", extra_source_action=True)


@pytest.mark.parametrize("slot_id", ["direct-macos", "direct-linux"])
def test_posix_requires_positive_manager_pid(tmp_path: Path, slot_id: str) -> None:
    with pytest.raises(RuntimeError, match="running PID|running instance"):
        collect(tmp_path, slot_id, zero_pid=True)


def test_systemd_rejects_appended_execstart_argument(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="systemd unit/action identity"):
        collect(tmp_path, "direct-linux", adversarial_argv=True)


def test_wrong_noncanonical_product_definition_is_rejected_before_registration(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="product-canonical"):
        collect(tmp_path, "direct-linux", wrong_definition_path=True)


def test_failed_product_removal_is_not_a_pass_and_uses_separate_exact_cleanup(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="exact cleanup observed: adapter-cleanup"):
        collect(tmp_path, "direct-linux", removal_sticks=True)


def test_product_removal_rejects_respawned_owned_action_process(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="residue may remain"):
        collect(tmp_path, "direct-linux", respawn_after_removal=True)


def test_product_removal_rejects_respawned_previously_observed_candidate(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="residue may remain"):
        collect(tmp_path, "direct-linux", respawn_candidate_after_removal=True)


@pytest.mark.parametrize(
    "option",
    ["delete_data_manifest_on_uninstall", "tamper_data_manifest_on_uninstall"],
)
def test_keep_data_uninstall_reobserves_exact_data_manifest(tmp_path: Path, option: str) -> None:
    with pytest.raises(RuntimeError, match="residue may remain"):
        collect(tmp_path, "direct-linux", **{option: True})


def test_denied_process_absence_is_not_treated_as_cleanup_proof(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="residue may remain"):
        collect(tmp_path, "direct-linux", deny_absence=True)


def test_sanitized_manager_evidence_never_publishes_probe_output_or_commands(
    tmp_path: Path,
) -> None:
    observation, _runner, _process = collect(tmp_path, "direct-macos")
    raw = observation["native_service"]["raw_observations"]
    serialized = json.dumps(raw)
    assert "SECRET_TOKEN" not in serialized
    assert "do-not-publish" not in serialized
    assert all("stdout" not in row and "stderr" not in row and "argv" not in row for row in raw)
    assert not any("show-environment" in " ".join(call) for call in _runner.calls)
    assert ["launchctl", "print", "gui/501"] not in _runner.calls


def test_failure_after_exact_observation_performs_separate_safe_cleanup(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="exact cleanup observed: adapter-cleanup"):
        collect(tmp_path, "direct-linux", fail_at="after-observe")


def test_adapter_rejects_tampered_real_data_identity_manifest(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="residue may remain"):
        collect(tmp_path, "direct-linux", fail_at="tamper-data-manifest")


def test_adapter_rejects_tampered_real_durable_journal(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="exact cleanup observed: adapter-cleanup"):
        collect(tmp_path, "direct-linux", fail_at="tamper-durable-journal")


def test_failure_without_exact_instance_preserves_residue_instead_of_unsafe_remove(
    tmp_path: Path,
) -> None:
    repos, heads = make_repos(tmp_path)
    process = FakeProcessProbe()
    runner = StatefulRunner("linux", heads, process)
    driver = FakeJourneyDriver(PROVISIONAL_CANDIDATE, process, runner, fail_at="before-observe")
    env = hosted_environment(tmp_path, "linux", None)
    with pytest.raises(RuntimeError, match="cleanup refused; residue may remain"):
        native.execute(
            "direct-linux",
            PROVISIONAL_CANDIDATE,
            FIXTURE,
            tmp_path / "run",
            app_repo=repos["app"],
            agent_repo=repos["agent"],
            runtime_repo=repos["runtime"],
            pinokio_repo=repos["pinokio"],
            environment=env,
            _runner=runner,
            _process_probe=process,
            _journey_driver=driver,
            _allow_platform_fixture=True,
        )
    assert runner.registered is True


def test_post_driver_validation_failure_still_uses_exact_safe_cleanup(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="exact cleanup observed: adapter-cleanup"):
        collect(tmp_path, "direct-linux", fail_at="return-before-removal")


def test_swapped_action_refuses_restart_and_unsafe_cleanup(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="cleanup refused; residue may remain"):
        collect(tmp_path, "direct-linux", fail_at="swapped-restart")


@pytest.mark.parametrize(
    "slot_id",
    [slot for slot in failure.REQUIRED_SLOT_IDS if not slot.startswith("direct-")],
)
def test_all_pinokio_slots_use_distinct_fixed_custody_provider_slot(
    tmp_path: Path, slot_id: str
) -> None:
    assert len(failure.REQUIRED_SLOT_IDS) == 9
    with pytest.raises(RuntimeError, match="NEEDS B/platform direct-Pinokio custody provider"):
        native.execute(
            slot_id,
            PROVISIONAL_CANDIDATE,
            FIXTURE,
            tmp_path / "run",
            app_repo=tmp_path,
            agent_repo=tmp_path,
            runtime_repo=tmp_path,
            pinokio_repo=tmp_path,
        )
    assert not (tmp_path / "run").exists()


def test_atomic_publication_helper_publishes_complete_tree_without_admission_claim(
    tmp_path: Path,
) -> None:
    observation, _runner, _process = collect(tmp_path / "fixture", "direct-linux")
    output = tmp_path / "published" / failure.RECEIPT_NAME
    receipt = {"mechanical_publication_test": True}
    native._publish_atomic(observation, receipt, output)
    assert failure.read_json(output) == receipt
    assert sorted(path.name for path in (output.parent / "evidence").iterdir()) == sorted(
        f"{scenario}.json" for scenario in failure.SCENARIOS
    )


def test_atomic_publication_cleans_stage_after_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation, _runner, _process = collect(tmp_path / "fixture", "direct-linux")
    output = tmp_path / "published" / failure.RECEIPT_NAME

    def fail_write(*_args, **_kwargs):
        raise OSError("controlled receipt write failure")

    monkeypatch.setattr(failure, "write_json", fail_write)
    with pytest.raises(OSError, match="controlled receipt write failure"):
        native._publish_atomic(observation, {}, output)
    assert not output.parent.exists()
    assert not list(tmp_path.glob(".published.native-publication-stage-*"))


def test_cli_rejects_fixture_result_without_publishing_receipt_or_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation, _runner, _process = collect(tmp_path / "fixture", "direct-linux")
    collector = observation["collector_provenance"]
    collector.update(
        {
            "kind": "live-platform",
            "hosted_admissible": True,
            "environment": "live-process",
            "command_runner": "SubprocessCommandRunner",
            "process_probe": "LiveProcessProbe",
            "journey_driver": "registered-shared-driver",
        }
    )
    observation["execution_scope"] = native.HOSTED_SCOPE
    observation["collector_structural_sha256"] = hashlib.sha256(
        json.dumps(collector, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for item in observation["evidence_files"]:
        item["collector_kind"] = "live-platform"
        item["collector_structural_sha256"] = observation["collector_structural_sha256"]
        item["execution_scope"] = native.HOSTED_SCOPE
    candidate_path = tmp_path / "candidate.json"
    failure.write_json(candidate_path, PROVISIONAL_CANDIDATE)
    output = tmp_path / "published" / failure.RECEIPT_NAME
    monkeypatch.setattr(native, "execute", lambda *args, **kwargs: copy.deepcopy(observation))
    result = native.main(
        [
            "--slot-id",
            "direct-linux",
            "--candidate",
            str(candidate_path),
            "--fixture",
            str(FIXTURE),
            "--work-root",
            str(tmp_path / "unused-work"),
            "--app-repo",
            str(tmp_path),
            "--agent-repo",
            str(tmp_path),
            "--runtime-repo",
            str(tmp_path),
            "--pinokio-repo",
            str(tmp_path),
            "--output",
            str(output),
        ]
    )
    assert result == 1
    assert not output.exists()
    assert not (output.parent / "evidence").exists()


def test_direct_script_help_works_outside_repository(tmp_path: Path) -> None:
    script = Path(native.__file__).resolve()
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": ""}
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--slot-id" in result.stdout
