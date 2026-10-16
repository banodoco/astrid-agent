"""Generate ordinary bundled imports from explicitly trusted V3 admission.

This is build input, not executable authority or a second component catalog.
The caller owns a private output directory and must verify emitted file hashes
before its bundler consumes them (the App Vite plugin does so).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from astrid.core.pack.components import bind_component, resolve_component
from astrid.sdk.timeline.commands import CommandError


def stage_components(requests, *, trusted_roots, output, host="app", project_root=None):
    output = Path(output).resolve()
    roots = tuple(str(Path(root).resolve()) for root in trusted_roots)
    if not roots:
        raise CommandError("component build requires explicit trusted roots")
    imports, entries, files, seen = [], [], {}, set()
    for index, request in enumerate(requests):
        identity, expected = request["identity"], request["implementation_digest"]
        if identity in seen:
            raise CommandError(f"component collision: {identity}")
        seen.add(identity)
        component = resolve_component(identity, project_root=project_root, extra_pack_roots=roots)
        binding = bind_component(component, host=host)
        if binding["implementation_digest"] != expected:
            raise CommandError(f"stale component implementation digest: {identity}")
        # Hash the complete identity, never a basename or per-product dispatch key.
        key = identity + "@" + expected
        directory = output / hashlib.sha256(key.encode()).hexdigest()
        for relative, source in binding["sources"].items():
            target = directory / relative
            if not target.resolve().is_relative_to(directory):
                raise CommandError("component source escapes staging directory")
            data = source["text"].encode("utf-8")
            if hashlib.sha256(data).hexdigest() != source["sha256"]:
                raise CommandError("component source digest mismatch")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files[str(target)] = source["sha256"]
        # Recheck admission, descriptor and every source after staging.
        if bind_component(component, host=host) != binding:
            raise CommandError("component changed while staging")
        for filename, digest in files.items():
            if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != digest:
                raise CommandError("staged component source changed")
        source = directory / binding["descriptor"]["implementation"]["path"]
        imports.append(f"import * as implementation{index} from {json.dumps(str(source))};")
        metadata = {k: binding[k] for k in ("identity", "implementation_digest", "descriptor")}
        metadata["key"] = key
        entries.append("{..." + json.dumps(metadata) + f", module: implementation{index}" + "}")
    output.mkdir(parents=True, exist_ok=True)
    module = output / "components.generated.ts"
    module.write_text("\n".join(imports) + "\nexport const componentBindings = [\n" + ",\n".join(entries) + "\n];\n")
    files[str(module)] = hashlib.sha256(module.read_bytes()).hexdigest()
    return {"module": str(module), "files": files}


def main():
    import sys
    request = json.load(sys.stdin)
    print(json.dumps(stage_components(**request)))


if __name__ == "__main__":
    main()
