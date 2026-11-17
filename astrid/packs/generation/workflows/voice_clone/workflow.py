# vibecomfy: generated
# For hand-editing, run: python -m vibecomfy.cli copy-to-recipe <id>
# vibecomfy: surface=canonical
"""Auto-generated ready_template — use python -m vibecomfy.cli copy-to-recipe <id> for hand-editing."""
from __future__ import annotations

from vibecomfy.templates import InputSpec, ModelAsset, OutputSpec, ReadyMetadata, new_workflow, ref
from vibecomfy.workflow import VibeWorkflow
from vibecomfy.nodes.core import LoadAudio, SaveAudioMP3
from vibecomfy.nodes.qwentts import AILab_Qwen3TTSVoiceClone


DEFAULT_SEED = 125
REFERENCE_WAV = 'reference.wav'
DEFAULT_REFERENCE_TEXT = ''
DEFAULT_TARGET_TEXT = ''
DEFAULT_LANGUAGE = 'English'


MODELS = {
    'config': ModelAsset(url='https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base/resolve/fd4b254389122332181a7c3db7f27e918eec64e3/config.json', sha256='b4f01752d15a488abde3e1ab44723ae4f4b9e68a4037257b098b3737893cc1f9', size_bytes=4494, subdir='TTS/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-Base'),
    'generation_config': ModelAsset(url='https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base/resolve/fd4b254389122332181a7c3db7f27e918eec64e3/generation_config.json', sha256='f1b90b4513f3b34c62851049e2492d7b4c5940daf1276f89c82b8ef04127f3aa', size_bytes=245, subdir='TTS/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-Base'),
    'merges': ModelAsset(url='https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base/resolve/fd4b254389122332181a7c3db7f27e918eec64e3/merges.txt', sha256='599bab54075088774b1733fde865d5bd747cbcc7a547c5bc12610e874e26f5e3', size_bytes=1671839, subdir='TTS/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-Base'),
    'preprocessor_config': ModelAsset(url='https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base/resolve/fd4b254389122332181a7c3db7f27e918eec64e3/preprocessor_config.json', sha256='efdde1022ea9d76928bf7a9cd53139138f5ba2e466e837f08f6105ab1af1c119', size_bytes=127, subdir='TTS/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-Base'),
    'tokenizer_config': ModelAsset(url='https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base/resolve/fd4b254389122332181a7c3db7f27e918eec64e3/tokenizer_config.json', sha256='dc3c31c3bdaedd5016382bb3cbe07323026775ad51f5a4fb564505992ae4a670', size_bytes=7344, subdir='TTS/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-Base'),
    'vocab': ModelAsset(url='https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base/resolve/fd4b254389122332181a7c3db7f27e918eec64e3/vocab.json', sha256='ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910', size_bytes=2776833, subdir='TTS/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-Base'),
    'model': ModelAsset(url='https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base/resolve/fd4b254389122332181a7c3db7f27e918eec64e3/model.safetensors', sha256='38fc7fc51c5e776e840414b6fd443962e9411b9654888fd7913e4da643cb857c', size_bytes=3857413744, subdir='TTS/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-Base'),
    'config_2': ModelAsset(url='https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base/resolve/fd4b254389122332181a7c3db7f27e918eec64e3/speech_tokenizer/config.json', sha256='ee65bb901c876664ab8707c487157aa1a6ee57c65969b28fb5ec9dc211e68167', size_bytes=2336, subdir='TTS/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-Base/speech_tokenizer'),
    'configuration': ModelAsset(url='https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base/resolve/fd4b254389122332181a7c3db7f27e918eec64e3/speech_tokenizer/configuration.json', sha256='6bc26d64eb5024b4d1dab5a52371958b429256d6c9d59787f1f5294a54e0cebd', size_bytes=76, subdir='TTS/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-Base/speech_tokenizer'),
    'preprocessor_config_2': ModelAsset(url='https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base/resolve/fd4b254389122332181a7c3db7f27e918eec64e3/speech_tokenizer/preprocessor_config.json', sha256='fcb3805e597e786d4067706e602f6688524640f8d3396790e2e09b5942fcbdfb', size_bytes=234, subdir='TTS/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-Base/speech_tokenizer'),
    'model_2': ModelAsset(url='https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base/resolve/fd4b254389122332181a7c3db7f27e918eec64e3/speech_tokenizer/model.safetensors', sha256='836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258', size_bytes=682293092, subdir='TTS/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-Base/speech_tokenizer'),
}


PUBLIC_INPUT_METADATA = {
    'seed': InputSpec(node=ref('ailab_qwen3ttsvoiceclone'), field='seed', default=DEFAULT_SEED, type='INT', infer_type=False),
    'reference_audio': InputSpec(node=ref('loadaudio'), field='audio', default=REFERENCE_WAV, type='COMBO', required=True, media_semantics='audio', infer_type=False),
    'reference_text': InputSpec(node=ref('ailab_qwen3ttsvoiceclone'), field='reference_text', default=DEFAULT_REFERENCE_TEXT, type='STRING', required=True, infer_type=False),
    'target_text': InputSpec(node=ref('ailab_qwen3ttsvoiceclone'), field='target_text', default=DEFAULT_TARGET_TEXT, type='STRING', required=True, infer_type=False),
    'language': InputSpec(node=ref('ailab_qwen3ttsvoiceclone'), field='language', default=DEFAULT_LANGUAGE, type='COMBO', range={'choices': ['Auto', 'Chinese', 'English', 'Japanese', 'Korean', 'French', 'German', 'Spanish', 'Portuguese', 'Russian', 'Italian']}, infer_type=False),
}


READY_METADATA = ReadyMetadata.build(
    capability='audio',
    template_id='audio/voice_clone',
    inputs=PUBLIC_INPUT_METADATA,
    models=MODELS,
    custom_node_packs={'ComfyUI-QwenTTS': {'commit': 'd8122a8ba835b65fd65c113d2b273b1ad1579293', 'url': 'https://github.com/1038lab/ComfyUI-QwenTTS.git', 'class_schema_sha256': '4137bb4f37ea178be0e794377829905d9ede1bc65496a23a51d766a3f03b2c84', 'classes_used': ['AILab_Qwen3TTSVoiceClone'], 'pip_packages': ['accelerate', 'librosa', 'openai-whisper', 'qwen-tts', 'soundfile', 'tiktoken'], 'status': 'discovered'}},
    provenance={'operation': 'authored'},
    operation='authored',
    ready_id='audio/voice_clone',
    source_bundle={'format_version': 2, 'generation_id': 'f2adc5e66d98d351504e0fdc65a4b79498b7fb2e7c81a359b537a15e35ad483e', 'custody_digest': '6b523d52fc689282403b5866f063109111394ddf66a101aedda938ce58851711'},
)

def build() -> VibeWorkflow:
    """Build the workflow (auto-generated)."""
    with new_workflow(READY_METADATA, source_path=__file__) as wf:

        loadaudio = LoadAudio(audio=REFERENCE_WAV, widget_0='reference.wav')

        ailab_qwen3ttsvoiceclone = AILab_Qwen3TTSVoiceClone(
            target_text=DEFAULT_TARGET_TEXT,
            model_size='1.7B',
            language=DEFAULT_LANGUAGE,
            reference_text=DEFAULT_REFERENCE_TEXT,
            seed=DEFAULT_SEED,
            reference_audio=loadaudio.out('AUDIO'),
        )

        saveaudiomp3 = SaveAudioMP3(
            filename_prefix='audio/voice_clone',
            audio=ailab_qwen3ttsvoiceclone.out('AUDIO'),
        )

        return wf.finalize(PUBLIC_INPUT_METADATA, outputs=[OutputSpec(node=saveaudiomp3, output_type='SaveAudioMP3', name='audio', artifact_kind='audio', mime_type='audio/mpeg', filename_prefix='audio/voice_clone', expected_cardinality='one')])
