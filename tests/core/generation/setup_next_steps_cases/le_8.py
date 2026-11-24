"""LE-8: a real advisory producer has no fix or blocking error."""
from astrid.core.generation.model_storage import DownloadStorageObservation, model_download_notice
from astrid.core.generation.preflight import readiness_envelope


def _download_notice():
    issue = model_download_notice(
        {"fixture-pack": 1024},
        DownloadStorageObservation(None, True, "not_declared"),
    )
    return readiness_envelope(issues=[issue])


CASES = [("first-run-without-volume", "model_download_notice", _download_notice)]
