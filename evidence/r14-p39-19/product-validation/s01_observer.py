"""Source-neutral observation of the failing S01 shared-placement transition."""
import copy
import json
import os
from pathlib import Path
import sys


def emit(event):
    with Path(os.environ['S01_OBSERVER_OUTPUT']).open('a') as output:
        output.write(json.dumps(event, sort_keys=True) + '\n')


def fps_fields(bundle):
    parent = bundle.get('parent', {}).get('config', {}).get('output', {})
    values = {'parent.config.output.fps': parent['fps']} if 'fps' in parent else {}
    for identity, shot in bundle.get('shots', {}).items():
        output = shot.get('internal_timeline', {}).get('output', {})
        if 'fps' in output:
            values[f'shots[{identity!r}].internal_timeline.output.fps'] = output['fps']
    return values


def trace(frame, event, arg):
    filename = frame.f_code.co_filename
    name = frame.f_code.co_name
    if filename.endswith('/astrid/sdk/timeline/validation.py') and name == '_validate_fps':
        if event == 'exception':
            values = frame.f_locals
            candidate, baseline = values['candidate'], values['baseline']
            emit({'event': 'fps_refusal', 'exception_type': arg[0].__name__,
                  'exception_message': str(arg[1]),
                  'baseline_fps': fps_fields(baseline), 'candidate_fps': fps_fields(candidate),
                  'baseline_shot_origins': baseline['source_mapping'].get('shots', {}),
                  'candidate_shot_origins': candidate['source_mapping'].get('shots', {}),
                  'baseline_placements': baseline['placements'],
                  'candidate_placements': candidate['placements'],
                  'allowed_removed': sorted(values.get('removed', []))})
        return trace
    if filename.endswith('/tests/editing_conformance/test_commit_runtime_occurrence_media.py') and name == '_create_fixture':
        if event == 'line' and frame.f_lineno == 99:
            values = frame.f_locals
            service, project = values['service'], values['project']
            head = service.get_project_timeline(project, 'film')
            parent = service.get_project_parent_composition_revision(project, 'film', head['head_revision_id'])
            occurrence, = parent['payload']['occurrences']
            shot = service.get_project_shot_revision(project, occurrence['shot_id'], occurrence['shot_revision_id'])
            source = service.get_project_timeline_revision(project, 'film', shot['internal_timeline_revision_id'])
            expected_shot = copy.deepcopy(values['document']['shot_revisions'][0]['payload'])
            expected_shot['internal_timeline_revision_id'] = source['revision_id']
            emit({'event': 'accepted_before_shared',
                  'parent_revision_id': parent['revision_id'],
                  'shot_id': shot['shot_id'], 'shot_revision_id': shot['revision_id'],
                  'internal_revision_id': source['revision_id'],
                  'managed_link_matches_payload': shot['internal_timeline_revision_id'] == shot['payload']['internal_timeline_revision_id'],
                  'exact_authored_shot': shot['payload'] == expected_shot,
                  'exact_authored_internal': source['payload'] == values['document']['internal_timeline_revisions'][0]['payload'],
                  'owner_count': len(parent['payload']['occurrences']),
                  'shot_payload': shot['payload']})
        return trace
    return None


def pytest_configure():
    sys.settrace(trace)


def pytest_unconfigure():
    sys.settrace(None)
