"""Generation-bound reuse of ManagedEnginePreparation's verified readiness.

The resident adapter retains this resource and its existing install lock. It
does not prepare an engine itself: a miss returns to ManagedEnginePreparation.
Content verification and an authenticated native owner observation remain on
every hit. No task's cancellation callback or mutable runner is reused.
"""
from __future__ import annotations

import copy
import hashlib
import os
import stat
from pathlib import Path

from .engine_contracts import CustodyUnresolved, IntegrityError
from .managed_preparation import digest


def _content_tree(path, check, *, omitted=frozenset()):
    """Content identity, including new files and link destinations, never mtime."""
    result = hashlib.sha256()
    seen = set()
    size = 0

    def visit(node, label):
        nonlocal size
        check()
        info = node.lstat()
        result.update(label.encode("utf-8", "surrogatepass"))
        result.update(str(stat.S_IMODE(info.st_mode)).encode())
        if node.is_symlink():
            target = os.readlink(node)
            result.update(b"link:" + target.encode("utf-8", "surrogatepass"))
            resolved = node.resolve(strict=True)
            if resolved in seen:
                return
            visit(resolved, label + "->" + str(resolved))
        elif stat.S_ISDIR(info.st_mode):
            if node.resolve() in seen:
                return
            seen.add(node.resolve())
            for child in sorted(node.iterdir()):
                relative = label + "/" + child.name
                if relative in omitted:
                    continue
                if child.name == ".git":
                    if not child.is_dir() or child.is_symlink():
                        raise IntegrityError("managed Git generation is not a directory")
                    for name in ("HEAD", "config", "packed-refs", "refs"):
                        member = child / name
                        if member.exists():
                            visit(member, relative + "/" + name)
                else:
                    visit(child, relative)
        elif stat.S_ISREG(info.st_mode):
            with node.open("rb") as source:
                before = os.fstat(source.fileno())
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    check()
                    size += len(block)
                    result.update(block)
                after = os.fstat(source.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise IntegrityError("managed generation changed while reading content")
        else:
            raise IntegrityError("managed generation contains a non-file member")
    visit(Path(path), "")
    return result.hexdigest(), size


def installation_generation(preparation, report):
    """Observe code/environment/receipt/target generations under the held lock."""
    from .engine_recipe import recipe_digest
    from .engine_runner import helper_manifest
    from .model_verification import ModelVerification, model_file_fact
    from .settings import model_trust_unchanged

    root, recipe = Path(preparation.root), preparation.recipe
    members = [root / ".astrid-engine/install.json", root / ".astrid-engine/environments",
               root / "runtime/out/sessions" / recipe.engine / "launch.json"]
    facts, read_bytes = [], 0
    for source in recipe.sources:
        folder = root / source.relative_path
        # These are Comfy's per-job IO directories, not executable source.
        excluded = frozenset({"/input", "/output", "/temp"}) if source.name == "comfyui" else frozenset()
        value, size = _content_tree(folder, preparation.check, omitted=excluded)
        facts.append([str(folder), value])
        read_bytes += size
    members.extend(root / environment.relative_path for environment in recipe.environments)
    for member in members:
        value, size = _content_tree(member, preparation.check)
        facts.append([str(member), value])
        read_bytes += size
    verification = ModelVerification(model_trust_unchanged())
    # Retain the existing user-selected model verification policy. Its witness
    # is not substituted for the strong executable-source checks above.
    if preparation.mode != "trust":
        for model in report["manifest"]["models"]:
            expected = model["sha256"].removeprefix("sha256:")
            path = Path(report["manifest"]["models_root"]) / model["subdir"] / model["name"]
            fact = model_file_fact(str(path), root=preparation.root,
                models_root=report["manifest"]["models_root"], declared_sha256=expected,
                deadline=preparation.deadline, check=preparation.check, verification=verification)
            if (not fact.regular or fact.symlink or fact.bytes != model["size"] or fact.sha256 != expected):
                raise IntegrityError("managed model generation differs from its declared digest")
            facts.append([str(path), fact.sha256, fact.bytes])
    return digest([facts, recipe_digest(recipe), helper_manifest(recipe), verification.trust_unchanged]), read_bytes


class RetainedPreparation:
    """Transfer an audited generation's lock, never a previous job context."""
    def __init__(self, preparation):
        if preparation.report is None or preparation.lock is None or preparation.uncertain:
            raise IntegrityError("only completed managed preparation can be retained")
        self.report = copy.deepcopy(preparation.report)
        self.plan = preparation._alignment_runner.plan
        self.owner = preparation._alignment_runner.service_owner
        self.key = (preparation.root, preparation.mode, preparation.visibility,
                    self.report["execution_identity"])
        self.generation, self.content_bytes = installation_generation(preparation, self.report)
        self.lock_path = Path(preparation.root) / ".astrid-engine/install.lock"
        info = self.lock_path.lstat()
        self.lock_identity = info.st_dev, info.st_ino, info.st_mode
        self.lock, preparation.lock = preparation.lock, None
        self.jobs = {}
        self.uncertain = False
        self.closed = False

    def register(self, preparation, output):
        if self.closed or self.uncertain:
            raise CustodyUnresolved("retained preparation is fenced")
        self.jobs[id(preparation)] = (preparation, Path(output))
        preparation.retained_readiness = self

    def observe(self, preparation):
        """Verify the retained generation before reusing canonical target facts."""
        if self.closed or self.uncertain:
            return False
        if self.key[:3] != (preparation.root, preparation.mode, preparation.visibility):
            return False
        from .engine_runner import _Runner
        preparation.check()
        info = self.lock_path.lstat()
        if info.st_nlink != 1 or (info.st_dev, info.st_ino, info.st_mode) != self.lock_identity:
            raise CustodyUnresolved("retained installation lock identity changed")
        try:
            current, size = installation_generation(preparation, self.report)
        except (OSError, IntegrityError):
            return False  # The existing full preparation diagnoses or repairs it.
        if current != self.generation:
            return False
        runner = _Runner(self.plan, preparation.context, preparation.adapter, preparation.deadline,
                         device=preparation.device, visibility=preparation.visibility)
        runner.service_owner = self.owner
        facts = runner.service("probe", launch=runner.launch_spec())
        if facts["state"] != "owned" or facts["owner_id"] != self.owner:
            return False
        preparation.readiness_reuse = {"generation": current, "content_bytes_verified": size,
                                       "audits": 0, "native_preparation": 0, "health_fetches": 0}
        preparation._retained_observation = self
        return True

    def reuse(self, preparation, *, execution_identity, input_digests):
        if (self.closed or self.uncertain or getattr(preparation, "_retained_observation", None) is not self
                or self.key != (preparation.root, preparation.mode, preparation.visibility, execution_identity)):
            return None
        report = copy.deepcopy(self.report)
        report.update(task_id=preparation.task, attempt_id=preparation.attempt,
                      execution_identity=execution_identity, inputs=dict(input_digests))
        # A hit reports verified readiness, not a new native mutation/doctor.
        report.pop("native_preparation", None)
        report["alignment"]["fetched"] = []
        preparation.report = report
        preparation.engine_reused = True
        return report

    def finish(self, preparation, *, worker_drained):
        item = self.jobs.get(id(preparation))
        if item is None:
            return
        try:
            preparation.close(execution_root=item[1], worker_drained=worker_drained)
        except BaseException:
            self.uncertain = True
            raise
        self.jobs.pop(id(preparation))

    def close(self):
        if self.closed:
            return
        if self.uncertain:
            raise CustodyUnresolved("retained managed readiness has unresolved prompt custody")
        # Called by the resident adapter only after it proved native absence;
        # reconcile every remaining attempt before relinquishing installation.
        for preparation, _output in tuple(self.jobs.values()):
            self.finish(preparation, worker_drained=True)
        self.lock.__exit__(None, None, None)
        self.closed = True
