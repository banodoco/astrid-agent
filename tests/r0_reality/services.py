"""R0 login-service snapshots and deterministic manager models for tests.

The snapshots are byte-for-byte copies, not definitions generated for a local
installation. Windows' StartBoundary was normalized in the upstream snapshot.
The state models expose only behaviors used by these fixtures; they do not
invoke a service manager or claim native timing, permission, or process proof.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

R0_AGENT = "3204bb00135bec8ee2d2d3fb21eed5abfceca649"
LABEL = "haus.astrid.runtime"
TASK = "Astrid Runtime"
_SNAPSHOTS = {"darwin": "launchd.plist", "linux": "systemd.service", "win32": "task.xml"}


def service_definition(system: Literal["darwin", "linux", "win32"]) -> bytes:
    """Return the authentic reviewed R0 sample definition for *system*."""
    try:
        name = _SNAPSHOTS[system]
    except KeyError:
        raise ValueError(f"unsupported service platform: {system}") from None
    return (Path(__file__).parent / "service_definitions" / name).read_bytes()


@dataclass
class ServiceManagerFake:
    """Small, explicit manager state machine driven by a virtual clock.

    register/start, kill, disable, stop, bootout and advance are the only
    modeled operations. A kill represents unexpected exit, not a drain.
    Windows' two repeating triggers are collapsed to one cadence; IgnoreNew
    gives the same single-instance result. Native scheduling is not modeled.
    """
    system: Literal["darwin", "linux", "win32"]
    registered: bool = False
    enabled: bool = False
    running: bool = False
    now: int = 0
    starts: int = 0
    events: list[str] = field(default_factory=list)
    _restart_at: int | None = None
    _next_repeat: int | None = None
    _failure_retries: int = 0

    def __post_init__(self):
        if self.system not in _SNAPSHOTS:
            raise ValueError(f"unsupported service platform: {self.system}")

    def register(self):
        self.registered = self.enabled = True
        self.events.append("register")
        if self.system == "darwin":  # RunAtLoad
            self._start()
        if self.system == "win32":
            self._next_repeat = self.now + 60

    def _start(self):
        if not self.running:
            self.running = True
            self.starts += 1
            self.events.append("start")
        else:
            self.events.append("ignore-new")

    def start(self):
        if not self.registered:
            raise RuntimeError("service is not registered")
        # R0 explicitly enables the Windows task before starting it.
        if self.system == "win32":
            self.enabled = True
            if self._next_repeat is None:
                self._next_repeat = self.now + 60
        self._restart_at = None
        self._start()

    def kill(self, *, failed: bool = True):
        if not self.running:
            raise RuntimeError("service is not running")
        self.running = False
        self.events.append("exit")
        if self.system == "darwin" and self.registered:
            self._restart_at = self.now  # KeepAlive; delay unspecified in this model
        elif self.system == "linux" and self.registered:
            # disable removes boot enablement, not the loaded Restart=always policy.
            self._restart_at = self.now + 5
        elif self.system == "win32" and self.enabled and failed and self._failure_retries < 999:
            self._failure_retries += 1
            self._restart_at = self.now + 60

    def disable(self):
        """Disable future enablement; launchd's R0 disable uses bootout."""
        self.events.append("disable")
        if self.system == "darwin":
            self.bootout()
        else:
            self.enabled = False
            if self.system == "win32":
                self._restart_at = None

    def stop(self):
        """Manager-directed stop; unlike an unexpected exit it does not restart."""
        if self.system == "darwin":
            raise ValueError("R0 launchd disable stops via bootout")
        self.running = False
        self._restart_at = None
        self.events.append("stop")

    def bootout(self):
        if self.system != "darwin":
            raise ValueError("bootout is a launchd operation")
        self.running = self.registered = self.enabled = False
        self._restart_at = None
        self.events.append("bootout")

    def advance(self, seconds: int):
        if seconds < 0:
            raise ValueError("cannot reverse virtual time")
        end = self.now + seconds
        while True:
            candidates = [t for t in (self._restart_at, self._next_repeat) if t is not None and t <= end]
            if not candidates:
                break
            self.now = min(candidates)
            if self._restart_at == self.now:
                self._restart_at = None
                if self.registered and (self.system != "win32" or self.enabled):
                    self._start()
            if self._next_repeat == self.now:
                self._next_repeat += 60
                if self.registered and self.enabled:
                    self.events.append("repeat")
                    self._start()
        self.now = end
