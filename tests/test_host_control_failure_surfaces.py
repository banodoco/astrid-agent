"""A parked activation EOF caused by a host-control failure must name that cause."""
from types import SimpleNamespace

from astrid.core.execution import generic_host


def test_activation_eof_names_the_control_failure_and_chains_it():
    cause = ValueError("peer identity unavailable")
    exc = generic_host.HostError("parked activation channel closed before a frame")
    failure = generic_host._parked_activation_error(exc, SimpleNamespace(control_error=cause))
    assert isinstance(failure, generic_host.HostError)
    assert str(failure) == "parked activation channel closed before a frame; host control failed: ValueError: peer identity unavailable"
    assert failure.__cause__ is cause


def test_activation_error_is_unchanged_without_a_control_failure():
    exc = generic_host.HostError("parked activation channel closed before a frame")
    assert generic_host._parked_activation_error(exc, SimpleNamespace(control_error=None)) is exc
    assert generic_host._parked_activation_error(exc, None) is exc
