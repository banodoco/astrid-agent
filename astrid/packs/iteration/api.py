"""Public programmatic entry points for iteration pack consumers."""

from astrid.pack_api import canonical_runtime_entrypoint

with canonical_runtime_entrypoint("iteration.assemble"):
    from astrid.packs.iteration.actions.assemble.run import (
        AssembleError,
        assemble_iteration,
        build_parser as build_assemble_parser,
    )

with canonical_runtime_entrypoint("iteration.experiment_prepare"):
    from astrid.packs.iteration.actions.experiment_prepare.run import (
        main as prepare_experiment,
    )

__all__ = [
    "AssembleError",
    "assemble_iteration",
    "build_assemble_parser",
    "prepare_experiment",
]
