import hashlib
import json
import math

import pytest

from astrid.core.rendering.contracts import FrameWindow
from astrid.core.rendering.service import RenderService
from astrid.packs.rendering.rendering.renderers.remotion.run import _stage_effect_assets_for_timeline
from astrid.packs.rendering.rendering.renderers.threejs.run import _support_reasons
from astrid.packs.rendering.shared.live_scenes.maple import prepare_maple
from astrid.packs.rendering.shared.live_scenes.package import validate_package


def _digest(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def _package(html: str = '<html></html>') -> dict:
    html_bytes = html.encode('utf-8')
    body = {
        'manifest': {'formatVersion': 1, 'entry': 'scene.html', 'duration': 100, 'authoredFps': 30},
        'entry': {
            'object_id': 'entry-object',
            'digest': _digest(html_bytes),
            'media_type': 'text/html',
            'size': len(html_bytes),
            'filename': 'scene.html',
        },
        'assets': [],
    }
    package_body = json.dumps(body, separators=(',', ':'), ensure_ascii=False)
    revision = _digest(package_body.encode('utf-8'))
    return {
        'revision': revision,
        'source': {'objectId': 'package-object', 'revision': revision},
        'packageBody': package_body,
        'html': html,
    }


PACKAGE = _package()


def _with_body(package: dict, body: dict) -> dict:
    package_body = json.dumps(body, separators=(',', ':'), ensure_ascii=False)
    revision = _digest(package_body.encode('utf-8'))
    return {
        **package,
        'revision': revision,
        'source': {**package['source'], 'revision': revision},
        'packageBody': package_body,
    }


def test_nonzero_window_preserves_scene_revision_and_advances_source_rate():
    clip = {'id':'c', 'clipType':'haus.astrid.liveScene', 'at':2, 'from':55, 'to':75, 'speed':2, 'track':'v', 'app':{'liveScene':PACKAGE}}
    config = {'tracks':[{'id':'v','kind':'visual'}], 'clips':[clip]}
    sliced = RenderService._window_timeline(config, FrameWindow(start_frame=180, end_frame=195, fps_rational=(30,1)))
    rendered = sliced['clips'][0]
    assert rendered['from'] == 63
    assert rendered['to'] == 64
    assert rendered['at'] == 0
    assert rendered['app']['liveScene'] is PACKAGE
    assert clip['from'] == 55
    assert RenderService._clip_end(clip, clip_start=RenderService._timeline_number(2,'at')) == 12


@pytest.mark.parametrize('field,value', [('formatVersion',2), ('entry','../escape.html'), ('duration',math.nan), ('authoredFps',False)])
def test_package_validation_errors(field,value):
    body = json.loads(PACKAGE['packageBody'])
    body['manifest'][field] = value
    with pytest.raises(ValueError):
        validate_package(_with_body(PACKAGE, body))


def test_valid_package_acceptance():
    validate_package(PACKAGE)


@pytest.mark.parametrize('assets', [None, {}, [{'object_id': 'extra'}]])
def test_prepared_scene_rejects_nonempty_or_missing_asset_contract(assets):
    body = json.loads(PACKAGE['packageBody'])
    body['assets'] = assets
    with pytest.raises(ValueError, match='empty array'):
        validate_package(_with_body(PACKAGE, body))


def test_entry_path_can_differ_from_uploaded_filename():
    body = json.loads(PACKAGE['packageBody'])
    body['manifest']['entry'] = 'src/index.html'
    body['entry']['filename'] = 'index.html'
    validate_package(_with_body(PACKAGE, body))


def test_manifest_only_package_body_change_is_rejected():
    body = json.loads(PACKAGE['packageBody'])
    body['manifest']['duration'] = 101
    changed = {**PACKAGE, 'packageBody': json.dumps(body, separators=(',', ':'))}
    with pytest.raises(ValueError, match='packageBody digest'):
        validate_package(changed)


def test_same_length_wrong_html_body_is_rejected():
    assert len('<html></html>') == len('<body></body>')
    with pytest.raises(ValueError, match='entry digest'):
        validate_package({**PACKAGE, 'html': '<body></body>'})


def test_package_source_revision_mismatch_is_rejected():
    with pytest.raises(ValueError, match='package/source revision'):
        validate_package({**PACKAGE, 'source': {**PACKAGE['source'], 'revision': 'sha256:' + '0' * 64}})


@pytest.mark.parametrize('field,message', [('digest', 'entry digest'), ('size', 'entry size')])
def test_entry_digest_or_size_mismatch_is_rejected(field, message):
    body = json.loads(PACKAGE['packageBody'])
    body['entry'][field] = _digest(b'other') if field == 'digest' else body['entry']['size'] + 1
    with pytest.raises(ValueError, match=message):
        validate_package(_with_body(PACKAGE, body))


def test_backend_admits_valid_source_and_fails_missing_source():
    clip = {'id':'c','clipType':'haus.astrid.liveScene','at':0,'hold':1,'track':'v','app':{'liveScene':PACKAGE}}
    config = {'tracks':[{'id':'v','kind':'visual'}], 'clips':[clip], 'theme_overrides':{'visual':{'canvas':{'width':320,'height':180,'fps':30}}}}
    assert _support_reasons(config) == []
    clip['app'] = {}
    assert any('invalid live scene' in reason for reason in _support_reasons(config))


def test_maple_adapter_rejects_unrecognized_source():
    with pytest.raises(ValueError, match='anchor changed'):
        prepare_maple('<html></html>')


def test_composition_clip_admission_does_not_weaken_standard_remotion(tmp_path):
    config = {'clips':[{'id':'c','clipType':'haus.astrid.liveScene','app':{'liveScene':PACKAGE}}]}
    options = {'project_dir':tmp_path,'theme_path':None,'render_hash':'fixture'}
    with pytest.raises(ValueError, match='unregistered effect'):
        _stage_effect_assets_for_timeline(config, **options)
    result = _stage_effect_assets_for_timeline(config, **options, composition_clip_types=frozenset({'haus.astrid.liveScene'}))
    assert result['effects'] == []
    config['clips'].append({'id':'unknown','clipType':'not-an-effect'})
    with pytest.raises(ValueError, match='not-an-effect'):
        _stage_effect_assets_for_timeline(config, **options, composition_clip_types=frozenset({'haus.astrid.liveScene'}))


def _without_html(package: dict) -> dict:
    return {key: value for key, value in package.items() if key != 'html'}


def test_htmlless_reference_package_validates_and_threejs_admits_it():
    validate_package(_without_html(PACKAGE))
    clip = {'id':'c','clipType':'haus.astrid.liveScene','at':0,'hold':1,'track':'v','app':{'liveScene':_without_html(PACKAGE)}}
    config = {'tracks':[{'id':'v','kind':'visual'}], 'clips':[clip], 'theme_overrides':{'visual':{'canvas':{'width':320,'height':180,'fps':30}}}}
    assert _support_reasons(config) == []


@pytest.mark.parametrize('html', ['', '   ', 7, None])
def test_present_html_must_still_be_a_nonempty_string(html):
    # ``"html": null`` is a present key, which the Runtime rejects: Python agrees.
    with pytest.raises(ValueError, match='envelope'):
        validate_package({**PACKAGE, 'html': html})


def test_resolver_fills_html_from_entry_object_and_verifies_digest():
    from astrid.packs.rendering.shared.live_scenes.package import resolve_live_scene_html

    html = '<html><body>scene</body></html>'
    package = _without_html(_package(html))
    reads = []

    def read(object_id):
        reads.append(object_id)
        return html.encode('utf-8') if object_id == 'entry-object' else None

    resolved = resolve_live_scene_html(package, read)
    assert resolved['html'] == html
    assert 'html' not in package
    assert reads == ['entry-object']
    validate_package(resolved)


def test_resolver_never_reads_objects_for_inline_html():
    from astrid.packs.rendering.shared.live_scenes.package import resolve_live_scene_html

    def read(object_id):
        raise AssertionError('inline html must not touch the object reader')

    assert resolve_live_scene_html(PACKAGE, read) is PACKAGE


@pytest.mark.parametrize('reader,reason', [
    (lambda object_id: None, 'is missing'),
    (lambda object_id: (_ for _ in ()).throw(RuntimeError('boom')), 'is unreadable'),
    (lambda object_id: b'<html></html>' + b'x', 'digest does not match'),
    (lambda object_id: b'\xff\xfe', 'digest does not match'),
])
def test_resolver_missing_or_mismatched_object_raises_typed_error_naming_object(reader, reason):
    from astrid.packs.rendering.shared.live_scenes.package import (
        LiveSceneHtmlError,
        resolve_live_scene_html,
    )

    with pytest.raises(LiveSceneHtmlError, match=reason) as error:
        resolve_live_scene_html(_without_html(PACKAGE), reader)
    assert error.value.object_id == 'entry-object'
    assert "'entry-object'" in str(error.value)


def test_resolver_rejects_wrong_size_even_when_digest_is_forged():
    from astrid.packs.rendering.shared.live_scenes.package import (
        LiveSceneHtmlError,
        resolve_live_scene_html,
    )

    body = json.loads(PACKAGE['packageBody'])
    body['entry']['size'] = len(b'<html></html>') + 1
    package = _without_html(_with_body(PACKAGE, body))
    with pytest.raises(LiveSceneHtmlError, match='size does not match'):
        resolve_live_scene_html(package, lambda object_id: b'<html></html>')


def test_remotion_props_timeline_gets_html_for_reference_clips_only(tmp_path):
    from astrid.packs.rendering.rendering.renderers.remotion.root_preparation import (
        _timeline_with_live_scene_html,
    )
    from astrid.packs.rendering.shared.live_scenes.package import LiveSceneHtmlError

    html = '<html><body>from store</body></html>'
    reference = _without_html(_package(html))
    inline = {'id': 'inline', 'clipType': 'haus.astrid.liveScene', 'app': {'liveScene': PACKAGE}}
    ref_clip = {'id': 'ref', 'clipType': 'haus.astrid.liveScene', 'app': {'liveScene': reference}}
    timeline = {'tracks': [], 'clips': [inline, ref_clip]}

    store_path = tmp_path / 'entry.html'
    store_path.write_bytes(html.encode('utf-8'))
    props = _timeline_with_live_scene_html(timeline, {'entry-object': str(store_path)})
    assert props['clips'][0] is inline
    assert props['clips'][1]['app']['liveScene']['html'] == html
    assert 'html' not in reference

    with pytest.raises(LiveSceneHtmlError, match="'entry-object'"):
        _timeline_with_live_scene_html(timeline, {})
    with pytest.raises(LiveSceneHtmlError, match="'entry-object'"):
        _timeline_with_live_scene_html(timeline, {'entry-object': b'wrong'})
