"""Local Runtime image import metadata."""
from tests.timeline.test_audio_import_runtime import realm, session_for  # noqa: F401,E402


def test_local_image_import_records_resolution(realm, tmp_path):
    from PIL import Image
    from tests.timeline.test_function_media_replay import opened

    service, writer, candidate = realm
    session = session_for(service, writer, candidate, tmp_path / 'outbox.json')
    source = tmp_path / 'picture.png'
    Image.new('RGB', (16, 12), 'red').save(source)
    result = opened(session).commit(lambda t: t.add(source, at=0, hold=1, id='picture'),
                                    idempotency_key='import-resolution')
    parent = result['data']['canonical_document']['parent_revision']['payload']
    asset = parent['clips'][0]['asset']
    assert parent['registry']['assets'][asset]['resolution'] == '16x12'
