"""A direct bundled command retains the host's Python network guard."""

from dataclasses import replace
from pathlib import Path

from astrid.core.contracts.schema import CommandSpec
from astrid.core.execution.generic_host import GenericPackHost, _is_selected_command


def test_builtin_selected_command_installs_network_guard_in_real_child(tmp_path):
    import astrid.core.execution.generic_host as generic_host

    builtin = Path(generic_host.__file__).resolve().parents[2] / "packs" / "rendering"
    host = GenericPackHost(pack_roots=[builtin], credential_source={})
    host.discover()
    original = host.capabilities["rendering.html_canvas_effect"]
    selected = host._selected_pack_map(original)
    code = (
        "import socket\n"
        "from pathlib import Path\n"
        "from astrid.core.execution import network_policy\n"
        "assert network_policy._INSTALLED\n"
        "try:\n"
        "    socket.socket().connect(('127.0.0.1', 9))\n"
        "except network_policy.NetworkPolicyError:\n"
        "    Path('{out}/guarded').write_text('denied before connect')\n"
        "else:\n"
        "    raise AssertionError('loopback connect was not denied')\n"
    )
    metadata = {**original.definition.metadata,
                "network_policy": {"allowed_protocols": [], "allowed_destinations": []}}
    metadata.pop("output_result_manifest", None)
    definition = replace(
        original.definition,
        command=CommandSpec(argv=("{python_exec}", "-c", code)),
        inputs=(), outputs=(),
        metadata=metadata,
    )
    record = replace(original, definition=definition)
    assert not _is_selected_command(record)  # Builtin commands use the direct path.
    attempt = tmp_path / "attempt"
    output = attempt / "outputs"
    output.mkdir(parents=True)

    result = host._run_command_definition(
        record, {}, output, attempt, admission={"selected_pack_map": selected},
    )

    assert result.returncode == 0
    assert (output / "guarded").read_text() == "denied before connect"
