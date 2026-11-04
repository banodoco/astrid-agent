"""Harness-only wrapper around the installed OMP module for one diagnostic call.

No credential, response, arbitrary path, or non-loopback endpoint is recorded.
"""
import json
import runpy
import sys
from pathlib import Path
from urllib.parse import urlsplit


def main():
    output, expected_root, request = sys.argv[1:4]
    record = {"root_observed": False, "endpoint_observed": False}
    try:
        import astrid.sdk.storage_root as storage
        import astrid.sdk.workspace_client as workspace

        original_root = storage.resolve_runtime_data_root
        original_init = workspace.WorkspaceClient.__init__

        def root(*args, **kwargs):
            value = original_root(*args, **kwargs)
            try:
                record["root_observed"] = True
                record["root_matches_run_data"] = str(value) == expected_root
                if str(value) == expected_root:
                    record["data_root"] = expected_root
            except Exception:
                pass
            return value

        def init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            try:
                endpoint = urlsplit(self.endpoint)
                record["endpoint_observed"] = True
                if (endpoint.scheme == "http" and endpoint.hostname == "127.0.0.1"
                        and endpoint.port and not endpoint.username and not endpoint.password
                        and not endpoint.path and not endpoint.query and not endpoint.fragment):
                    record["endpoint"] = f"http://127.0.0.1:{endpoint.port}"
                else:
                    record["endpoint_withheld"] = True
            except Exception:
                pass
        storage.resolve_runtime_data_root = root
        workspace.WorkspaceClient.__init__ = init
    except Exception:
        record["instrumentation_unavailable"] = True
    try:
        sys.argv = ["astrid.omp_invoke", request]
        runpy.run_module("astrid.omp_invoke", run_name="__main__", alter_sys=True)
    finally:
        try:
            Path(output).write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        except Exception:
            pass


if __name__ == "__main__":
    main()
