"""Read-only observation of Runtime's command-claim refusal after S01 fps validation."""
import sys
from s01_observer import emit, trace as fixture_trace


def trace(frame, event, arg):
    if (frame.f_code.co_filename.endswith('/runtime_protocol/timeline_commit_auth.py')
            and frame.f_code.co_name == 'validate_claims'):
        if event == 'exception':
            state = frame.f_locals
            emit({'event': 'command_claim_refusal',
                  'exception_type': arg[0].__name__, 'exception_message': str(arg[1]),
                  'core_matches': state.get('core_matches'),
                  'created_matches': state.get('created_matches'),
                  'inverse_restoration': state.get('inverse_restoration'),
                  'track_creation': state.get('track_creation'),
                  'submitted': state.get('submitted'),
                  'record_command': state.get('record', {}).get('command'),
                  'record_created': state.get('record', {}).get('created')})
        return trace
    observed = fixture_trace(frame, event, arg)
    return trace if observed is not None else None


def pytest_configure():
    sys.settrace(trace)


def pytest_unconfigure():
    sys.settrace(None)
