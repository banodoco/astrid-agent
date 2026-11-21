# vibecomfy: generated
# For hand-editing, run: python -m vibecomfy.cli copy-to-recipe <id>
# vibecomfy: surface=canonical
"""Auto-generated ready_template — use python -m vibecomfy.cli copy-to-recipe <id> for hand-editing."""
from __future__ import annotations

from vibecomfy.nodes.core import LoadAudio
from vibecomfy.templates import InputSpec, ModelAsset, OutputSpec, ReadyMetadata, new_workflow, ref
from vibecomfy.templates import node as raw_call
from vibecomfy.workflow import VibeWorkflow

MODEL_NAME = 'large-v3-turbo'
DEFAULT_AUDIO_REF = 'input.wav'


MODELS = {
    'large_v3_turbo': ModelAsset(url='https://openaipublic.azureedge.net/main/whisper/models/aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a/large-v3-turbo.pt', sha256='aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a', size_bytes=1617941637, subdir='stt/whisper'),
}


PUBLIC_INPUT_METADATA = {
    'audio_ref': InputSpec(node=ref('loadaudio'), field='audio', default=DEFAULT_AUDIO_REF, type='COMBO', required=True, media_semantics='audio', infer_type=False),
}


READY_METADATA = ReadyMetadata.build(
    capability='audio',
    template_id='audio/transcribe',
    inputs=PUBLIC_INPUT_METADATA,
    models=MODELS,
    requirements={'custom_nodes': [{'name': 'ComfyUI-Whisper', 'slug': 'ComfyUI-Whisper', 'source': 'git', 'commit': '006a709848259ed3d69e2f9fcfd7a75f6c860ddd', 'url': 'https://github.com/yuvraj108c/ComfyUI-Whisper.git', 'class_set': ['Apply Whisper'], 'class_schema_sha256': 'a035372485b38b70022c249aa4cfb2034a832b27b683069b8de7762597c1115e', 'pip_packages': ['openai-whisper', 'soundfile']}]},
    custom_node_packs={'ComfyUI-Whisper': {'commit': '006a709848259ed3d69e2f9fcfd7a75f6c860ddd', 'url': 'https://github.com/yuvraj108c/ComfyUI-Whisper.git', 'class_schema_sha256': 'a035372485b38b70022c249aa4cfb2034a832b27b683069b8de7762597c1115e', 'classes_used': ['Apply Whisper'], 'pip_packages': ['openai-whisper', 'soundfile'], 'status': 'discovered'}},
    provenance={'operation': 'authored'},
    ready_id='audio/transcribe',
    source_bundle={'format_version': 2, 'generation_id': '114a862a6255214668a6247ba935f55f102b1389d3c58e5581b6a3030afd443c', 'custody_digest': 'bcfb5503a3296e2cb24275504d579cf4cb21399f835f571113fafe4dd537531e'},
)

def build() -> VibeWorkflow:
    """Build the workflow (auto-generated)."""
    with new_workflow(READY_METADATA, source_path=__file__) as wf:

        loadaudio = LoadAudio(audio=DEFAULT_AUDIO_REF)

        apply_whisper = raw_call('Apply Whisper',
            _outputs=('text', 'segments_alignment', 'words_alignment'),
            language='auto',
            model=MODEL_NAME,
            prompt='',
            audio=loadaudio.out('AUDIO'),
        )

        vibecomfysavetranscript = raw_call('VibeComfySaveTranscript',
            filename_prefix='audio/transcript',
            segments_alignment=apply_whisper.out('segments_alignment'),
            text=apply_whisper.out('text'),
            words_alignment=apply_whisper.out('words_alignment'),
        )

        return wf.finalize(PUBLIC_INPUT_METADATA, outputs=[OutputSpec(node=vibecomfysavetranscript, output_type='VibeComfySaveTranscript', name='transcript_json', artifact_kind='json', mime_type='application/json', filename_prefix='audio/transcript', expected_cardinality='one')])
