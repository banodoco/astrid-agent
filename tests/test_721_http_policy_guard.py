"""Keep shipped Agent HTTP/TLS openings at their audited trust-policy boundaries.

The keys name a path, qualified function and API, never a whole-file exemption.
Fingerprints seal the complete function and its imported/global bindings, so
an extra opening or a changed context cannot hide beside an approved call.
Loopback exceptions also seal their callers' URL validation in the same module.
Dependencies and development/CI scripts are outside the shipped Agent boundary.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTTP_METHODS = {"request", "get", "post", "put", "patch", "delete", "head", "options"}
NETWORK_MODULES = {"http", "httpx", "requests", "aiohttp", "urllib", "urllib3", "ssl"}
OPENINGS = {
    "http.client.HTTPSConnection",
    "urllib3.PoolManager",
    "urllib3.poolmanager.PoolManager",
    "ssl.SSLContext.wrap_socket",
    "ssl.wrap_socket",
    "urllib.request.urlopen",
    "urllib.request.build_opener",
    "urllib.request.OpenerDirector",
    "urllib.request.HTTPSHandler",
    "httpx.Client",
    "httpx.AsyncClient",
    "httpx.stream",
    "requests.Session",
    "requests.session",
    "requests.sessions.Session",
    "requests.sessions.session",
    "aiohttp.ClientSession",
    "aiohttp.client.ClientSession",
    "aiohttp.TCPConnector",
    "aiohttp.connector.TCPConnector",
    "aiohttp.request",
    "aiohttp.client.request",
    *(
        f"{module}.{method}"
        for module in ("httpx", "requests", "requests.api")
        for method in HTTP_METHODS
    ),
}

# These six openings have a separately audited loopback HTTP caller contract.
# The last two rely on that caller contract, not a scheme check in the opener.
LOOPBACK_FUNCTIONS = {
    ("astrid/mcp_server.py", "_comfy_workflow_import"),
    ("astrid/installer/backend.py", "Backend.verify"),
    ("astrid/packs/vibecomfy/native_preparation.py", "_read_object_info"),
    ("astrid/packs/vibecomfy/local.py", "_get_json"),
    ("astrid/core/dev/journey.py", "runtime_request"),
    ("astrid/packs/runpod/pod_reset.py", "_post"),
}

# Explicit source records; refresh only with the affected policy/caller proof.
# Values are (reference count, SHA-256 of the audited syntax and bindings).
POLICY_OPENINGS = {
    # The packaged native verifier must wrap its socket before checking the
    # peer chain. Seal that one implementation; no module-wide exemption.
    (
        "astrid/installer/_tls_truststore/_api.py",
        "SSLContext.wrap_socket",
        "ssl.SSLContext.wrap_socket",
    ): (1, "ec5e52fde9c0c3c6f13d3fbb0230e821f9df9fdfd89f9df0463f9c585ad15d7b"),
    (
        "astrid/core/execution/executor/banodoco_catalog.py",
        "_fetch_catalog_payload",
        "urllib.request.urlopen",
    ): (1, "70c8f2ee5813c8a55978e956571ed9f3f26026b35978363b66f056cae8570672"),
    (
        "astrid/core/generation/backends/comfy_target.py",
        "open_comfy_http",
        "urllib.request.HTTPSHandler",
    ): (1, "cdafb657bc9b41c9236239b3b0e3d49ae6d2488d3044d0871a94dfad78617493"),
    (
        "astrid/core/generation/backends/comfy_target.py",
        "open_comfy_http",
        "urllib.request.build_opener",
    ): (1, "cdafb657bc9b41c9236239b3b0e3d49ae6d2488d3044d0871a94dfad78617493"),
    ("astrid/core/generation/backends/openai.py", "_request_json", "urllib.request.urlopen"): (
        1,
        "2913ce0e966270cd9cc6aed4ac2a100546a4d9707c0ee5937abc96a32de9b65e",
    ),
    ("astrid/core/generation/model_downloader.py", "_download_model", "urllib.request.urlopen"): (
        1,
        "82f434a72ba6056f740d1ee34a8219805e8450bb6722902477a5cd19367cf7ea",
    ),
    ("astrid/core/hivemind/executors/_common.py", "_http_get", "urllib.request.urlopen"): (
        1,
        "65f27fb7de20ddfdfc5e1cc4afed97ecf5ba7afee2632d9f8c0c687ae006ad73",
    ),
    ("astrid/core/hivemind/executors/_common.py", "_http_post", "urllib.request.urlopen"): (
        1,
        "f60a01d5f8dd2cab955965a2a3c113e15592a354acea8b96986b001c891c69f8",
    ),
    (
        "astrid/core/hivemind/executors/embedding_contract.py",
        "_stdlib_transport",
        "urllib.request.urlopen",
    ): (1, "7aadf459dc30699356911efb4dd3f5e7900f1a6231b243832fe37c7164154f18"),
    (
        "astrid/core/hivemind/executors/ingest_article/run.py",
        "fetch_html",
        "urllib.request.urlopen",
    ): (1, "89bcfc30acb5c202f1d0e0ef23d40fbb070debc4fef6fdff1804e9ca528d227f"),
    (
        "astrid/core/hivemind/executors/ingest_workflow/run.py",
        "load_workflow_from_url",
        "urllib.request.urlopen",
    ): (1, "c3347cc15a0a33ddba562d3ca77bc1ead13da0fe9562139e6d5232a57e46c11f"),
    ("astrid/core/model_setup/acquire.py", "_download", "urllib.request.urlopen"): (
        1,
        "4f2574a0ee3f31eab2d4abe3441faf0507c2b13505c07b3373bfc040b90e757a",
    ),
    ("astrid/core/util/http.py", "_real_transport", "urllib.request.urlopen"): (
        1,
        "5cd6b587a0a4d6e43a0ea41f2995d4af61e6393c73d72bd16b95fad4c326a797",
    ),
    ("astrid/core/util/http.py", "trusted_async_httpx_client", "httpx.AsyncClient"): (
        2,
        "86e141ba3e37a7f1d376fd2e6a8edca498837093009f4c2e49e304c8188b7e0a",
    ),
    ("astrid/core/util/http.py", "trusted_httpx_client", "httpx.Client"): (
        2,
        "845d3b87551a1dc651c52355301c313ee215ecc5d9198624504c8b7c55566c16",
    ),
    ("astrid/installer/tls_fallback.py", "open_with_tls_fallback", "urllib.request.HTTPSHandler"): (
        1,
        "d04862fc6dc737b84a4385de72fcabe40ea9a76fab708d4bd40f94810bdf487b",
    ),
    ("astrid/installer/tls_fallback.py", "open_with_tls_fallback", "urllib.request.build_opener"): (
        1,
        "d04862fc6dc737b84a4385de72fcabe40ea9a76fab708d4bd40f94810bdf487b",
    ),
    ("astrid/installer/tls_fallback.py", "urlopen_with_tls_fallback", "urllib.request.urlopen"): (
        1,
        "eded05fe924860ebe567a726cf4520d3eebab3de73990b8c1afb502328a39025",
    ),
    ("astrid/omp_agent.py", "_run_key_command", "urllib.request.urlopen"): (
        1,
        "65affbfda7adea1099e88456c39cede21e2f9d70341be31f562e916ebb094d05",
    ),
    (
        "astrid/packs/rendering/actions/sprite_sheet/upscale.py",
        "_call_image_edit_api",
        "urllib.request.urlopen",
    ): (1, "a99df1072704d9ab87a647d24bd08c23c7c17dc85e8ec196aad8e09144a71712"),
    ("astrid/packs/runpod/h3_release_remote.py", "download_model", "urllib.request.urlopen"): (
        1,
        "5f9bec95f544aecbe7dca1cab570ca8cba95df5be7b350c3cce923f3aaf0aada",
    ),
    ("astrid/packs/runpod/pod_watchdog.py", "urlopen", "urllib.request.urlopen"): (
        1,
        "9bc5ba745a40792205b80bd10cde4c122eae7263fbb8801d9794d3d5c3db1576",
    ),
    ("astrid/packs/runpod/provider.py", "RunPodMachineProvider.find.fetch", "httpx.get"): (
        1,
        "5913905948a879a4963c0915638d10f53772b5eb49091087a7528341caa1867d",
    ),
    ("astrid/packs/runpod/provider.py", "RunPodMachineProvider.read_raw", "httpx.get"): (
        1,
        "6abc9cf9c9b22652427c9c43b22649bac1cc69b4ad56a44074dd483edf1a47ac",
    ),
    (
        "astrid/packs/training/actions/search_loras/run.py",
        "_fetch_models",
        "urllib.request.urlopen",
    ): (1, "e4e7198ba6a4d67c5e5ebd6d07ad8c5249169da7bf389aec20a1eceb4bb1b256"),
    (
        "astrid/packs/vibecomfy/extensions/generation.engine/comfyui_engine.py",
        "ComfyUIEngine.probe",
        "urllib.request.urlopen",
    ): (1, "c4dbcad97c4d52282d9d26c9db00169ef40112b8f16a30d262c4832d2cb42c87"),
}
LOOPBACK_HTTP_OPENINGS = {
    ("astrid/core/dev/journey.py", "runtime_request", "urllib.request.urlopen"): (
        1,
        "7c1a7e7485cdfe2e342282b2ef8885d23e0b0349ec1a138c3d1cc52a78a91349",
    ),
    ("astrid/installer/backend.py", "Backend.verify", "urllib.request.build_opener"): (
        1,
        "20f66fe079c64a1709d8c6bbbf298b5198a04336308a6c317fac35dfcf6797e9",
    ),
    ("astrid/mcp_server.py", "_comfy_workflow_import", "urllib.request.build_opener"): (
        1,
        "4da7fcc51b87402b63716a4195db09233a4e0f68ac979d6d38fd5ff286503ea7",
    ),
    ("astrid/packs/runpod/pod_reset.py", "_post", "urllib.request.urlopen"): (
        1,
        "83377cc82d5a184512e4085970a12ce20a1636506203c4097757ea4282654545",
    ),
    ("astrid/packs/vibecomfy/local.py", "_get_json", "urllib.request.urlopen"): (
        1,
        "355401289403130d0ca0c4ada7827ccd3f70e1fdece5e688226df7932620461c",
    ),
    # One GET per attempt, up to two per engine and four across one restart.
    # Literal IPv4 loopback only; redirects and environment proxies are disabled.
    ("astrid/packs/vibecomfy/native_preparation.py", "_read_object_info", "httpx.stream"): (
        1,
        "2c8233fd6ca8a751f928d67b935041398791fad298e503f55aea38104d934ebe",
    ),
}


def _qualified(node, bindings):
    if isinstance(node, ast.Name):
        return bindings.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        prefix = _qualified(node.value, bindings)
        return f"{prefix}.{node.attr}" if prefix else None
    if isinstance(node, ast.Call):
        target = _qualified(node.func, bindings)
        if target in ("__import__", "importlib.import_module") and node.args:
            value = node.args[0]
            return (
                value.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
                else None
            )
        if target == "getattr" and len(node.args) >= 2:
            prefix = _qualified(node.args[0], bindings)
            attribute = node.args[1]
            if prefix and isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
                return f"{prefix}.{attribute.value}"
            if prefix and prefix.split(".")[0] in NETWORK_MODULES:
                return prefix + ".<dynamic>"
    return None


def _bindings(tree):
    bindings = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    # Callable/module aliases also count when passed into partials or callbacks.
    for _ in range(len(bindings) + 1):
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = _qualified(node.value, bindings)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if value and value.split(".")[0] in NETWORK_MODULES:
                    for target in targets:
                        if isinstance(target, ast.Name) and bindings.get(target.id) != value:
                            bindings[target.id] = value
                            changed = True
        if not changed:
            break
    return bindings


def _syntax(node):
    """Stable syntax across supported Python versions, without source positions."""
    if isinstance(node, ast.AST):
        return [
            type(node).__name__,
            {
                key: _syntax(value)
                for key, value in ast.iter_fields(node)
                if not (key == "type_params" and not value)
            },
        ]
    if isinstance(node, list):
        return [_syntax(value) for value in node]
    if node is Ellipsis or isinstance(node, (bytes, complex)):
        return repr(node)
    return node


def _source_openings(path, source):
    tree = ast.parse(source, filename=path)
    bindings = _bindings(tree)
    globals_ast = [
        _syntax(node)
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom))
    ]
    found = defaultdict(list)

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.names = []
            self.scopes = [tree]

        def visit_scope(self, node):
            self.names.append(node.name)
            self.scopes.append(node)
            self.generic_visit(node)
            self.scopes.pop()
            self.names.pop()

        visit_FunctionDef = visit_scope
        visit_AsyncFunctionDef = visit_scope
        visit_ClassDef = visit_scope

        def record(self, api):
            function = ".".join(self.names) or "<module>"
            scope = tree if (path, function) in LOOPBACK_FUNCTIONS else self.scopes[-1]
            payload = [_syntax(scope), bindings, globals_ast]
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            found[(path, function, api)].append(digest)

        def visit(self, node):
            if isinstance(node, (ast.Name, ast.Attribute, ast.Call)) and not isinstance(
                getattr(node, "ctx", None), ast.Store
            ):
                api = _qualified(node, bindings)
                # Contexts may arrive as parameters or arbitrary expressions.
                # Audit every wrap_socket reference, without assuming that its
                # receiver was constructed by a trusted context factory.
                if (isinstance(node, ast.Attribute) and node.attr == "wrap_socket") or (
                    api and api.endswith(".wrap_socket")
                ):
                    api = (
                        "ssl.wrap_socket" if api == "ssl.wrap_socket" else "ssl.SSLContext.wrap_socket"
                    )
                if api in OPENINGS or (api and api.endswith(".<dynamic>")):
                    self.record(api)
            return super().visit(node)

        def visit_ImportFrom(self, node):
            if node.module and node.module.split(".")[0] in NETWORK_MODULES:
                if any(alias.name == "*" for alias in node.names):
                    self.record(node.module + ".*")

    Visitor().visit(tree)
    return {key: (len(values), values[0]) for key, values in found.items()}


def _inventory(sources):
    return {
        key: value
        for path, source in sources.items()
        for key, value in _source_openings(path, source).items()
    }


def _violations(observed, approved):
    return {
        "new": sorted(observed.keys() - approved.keys()),
        "changed": sorted(
            key for key in observed.keys() & approved.keys() if observed[key] != approved[key]
        ),
        "stale": sorted(approved.keys() - observed.keys()),
    }


def _agent_sources():
    # These authoring tests/golden inputs are excluded from the Agent wheel.
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in (ROOT / "astrid").rglob("*.py")
        if "tests" not in path.relative_to(ROOT / "astrid").parts
        and "golden" not in path.relative_to(ROOT / "astrid").parts
    }


def test_agent_http_openings_use_the_audited_policy_or_loopback_contract():
    assert not POLICY_OPENINGS.keys() & LOOPBACK_HTTP_OPENINGS.keys()
    assert {(path, function) for path, function, _ in LOOPBACK_HTTP_OPENINGS} == LOOPBACK_FUNCTIONS
    violations = _violations(
        _inventory(_agent_sources()), {**POLICY_OPENINGS, **LOOPBACK_HTTP_OPENINGS}
    )
    assert not any(violations.values()), violations


@pytest.mark.parametrize(
    "source",
    [
        'import urllib.request\nurllib.request.urlopen("https://outside.test")',
        'from urllib.request import urlopen as open_url\nopen_url("https://outside.test")',
        "from urllib import request as r\nr.build_opener()",
        "import httpx\nhttpx.Client()",
        "import httpx as h\nh.AsyncClient(verify=False)",
        'from httpx import get as fetch\nfetch("https://outside.test")',
        'import requests\nrequests.get("https://outside.test")',
        "from requests import Session as S\nS()",
        "import requests.sessions as s\ns.Session()",
        "import aiohttp\naiohttp.ClientSession()",
        "from aiohttp import TCPConnector as C\nC(ssl=False)",
        'import aiohttp\naiohttp.request("GET", "https://outside.test")',
        "import functools, urllib.request\nopen_url = functools.partial(urllib.request.urlopen)",
        'import httpx\nprovider_call(httpx.get, "https://outside.test")',
        "import httpx\nopen_client = httpx.Client\nopen_client()",
        'import httpx\ngetattr(httpx, "Client")()',
        'import importlib\nh = importlib.import_module("httpx")\nh.Client()',
        'from urllib.request import *\nurlopen("https://outside.test")',
    ],
)
def test_guard_rejects_injected_openings_and_aliases(source):
    observed = _inventory({"astrid/new_client.py": source})
    assert _violations(observed, {})["new"]


@pytest.mark.parametrize(
    "source,api",
    [
        pytest.param(
            'import http.client\nhttp.client.HTTPSConnection("outside.test")',
            "http.client.HTTPSConnection",
            id="https-connection",
        ),
        pytest.param(
            "import urllib3\nurllib3.PoolManager()",
            "urllib3.PoolManager",
            id="pool-manager",
        ),
        pytest.param(
            "import ssl\ncontext = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)\n"
            'context.wrap_socket(sock, server_hostname="outside.test")',
            "ssl.SSLContext.wrap_socket",
            id="raw-wrap-socket",
        ),
    ],
)
def test_guard_rejects_reviewed_raw_tls_openings(source, api):
    path = "astrid/new_client.py"
    assert (path, "<module>", api) in _violations(_inventory({path: source}), {})["new"]


@pytest.mark.parametrize(
    "source",
    [
        'from http.client import HTTPSConnection as C\nopen_connection = C\n'
        'open_connection("outside.test")',
        "from urllib3 import PoolManager as P\nP()",
        "import urllib3.poolmanager as p\np.PoolManager()",
        "import ssl as s\ns.SSLContext.wrap_socket(context, sock)",
        "import ssl\nssl.wrap_socket(sock)",
        "import ssl\nssl.create_default_context().wrap_socket(sock)",
        'def connect(context, sock):\n    return getattr(context, "wrap_socket")(sock)',
    ],
)
def test_guard_rejects_raw_tls_aliases_and_context_receivers(source):
    assert _violations(_inventory({"astrid/new_client.py": source}), {})["new"]


def test_guard_seals_native_socket_certificate_verification():
    path = "astrid/installer/_tls_truststore/_api.py"
    source = (ROOT / path).read_text()
    original = "_verify_peercerts(ssl_sock, server_hostname=server_hostname)"
    assert original in source
    expected = {key: value for key, value in POLICY_OPENINGS.items() if key[0] == path}
    assert expected and not any(_violations(_inventory({path: source}), expected).values())
    changed = source.replace(original, "pass", 1)
    assert _violations(_inventory({path: changed}), expected)["changed"]


@pytest.mark.parametrize(
    "replacement",
    [
        '    kwargs["verify"] = False',
        '    kwargs["verify"] = None',
        '    kwargs["verify"] = ssl.create_default_context()',
    ],
)
def test_guard_rejects_replacing_an_approved_context(replacement):
    path = "astrid/core/util/http.py"
    source = (ROOT / path).read_text()
    old = '    kwargs["verify"] = os_trust_ssl_context()'
    assert old in source
    source = source.replace(old, replacement, 1)
    expected = {key: value for key, value in POLICY_OPENINGS.items() if key[0] == path}
    assert _violations(_inventory({path: source}), expected)["changed"]


def test_guard_rejects_an_extra_opening_inside_an_approved_function():
    path = "astrid/core/util/http.py"
    source = (
        (ROOT / path)
        .read_text()
        .replace(
            "    return httpx.Client(**kwargs)",
            '    httpx.get("https://outside.test")\n    return httpx.Client(**kwargs)',
            1,
        )
    )
    expected = {key: value for key, value in POLICY_OPENINGS.items() if key[0] == path}
    violations = _violations(_inventory({path: source}), expected)
    assert violations["new"] and violations["changed"]


def test_guard_seals_the_loopback_callers_and_rejects_a_stale_exception():
    path = "astrid/packs/vibecomfy/local.py"
    source = (ROOT / path).read_text()
    expected = {key: value for key, value in LOOPBACK_HTTP_OPENINGS.items() if key[0] == path}
    assert expected and not any(_violations(_inventory({path: source}), expected).values())
    changed = source.replace("http://127.0.0.1", "https://outside.test")
    assert changed != source
    assert _violations(_inventory({path: changed}), expected)["changed"]
    assert _violations({}, expected)["stale"]
