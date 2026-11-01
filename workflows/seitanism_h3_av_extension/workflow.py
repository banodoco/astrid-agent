# vibecomfy: generated
# For hand-editing, run: python -m vibecomfy.cli copy-to-recipe <id>
# vibecomfy: surface=canonical
"""Auto-generated ready_template — use python -m vibecomfy.cli copy-to-recipe <id> for hand-editing."""
from __future__ import annotations

from vibecomfy.templates import InputSpec, ModelAsset, OutputSpec, ReadyMetadata, new_workflow, node as raw_call, ref
from vibecomfy.workflow import VibeWorkflow
from vibecomfy.nodes.core import BasicGuider, BasicScheduler, CLIPLoader, ComfyMathExpression, KSamplerSelect, LoadAudio, LoadImage, LoraLoaderModelOnly, PreviewAny, RandomNoise, ResolutionSelector, SamplerCustomAdvanced, UNETLoader, VAEDecode, VAEDecodeAudio, VAELoader
from vibecomfy.nodes.videohelpersuite import VHS_LoadVideoFFmpeg, VHS_VideoCombine


AUDIO_VAE_NAME = 'minimax_h3_audio_vae_fp32.safetensors'
CLIP_NAME = 'qwen3vl_32b_minimax_h3_int8_convrot.safetensors'
DEFAULT_PROMPT = 'Continue directly from the final moment of the selected start clip with no cut, reset, or re-establishment. The incoming protected audiovisual prefix is authoritative for pose, motion, camera trajectory, lighting, environment, object state, voice, ambience, and timing. Connected reference images are identity/appearance references only; never pull the subject back toward a reference-image pose, expression, framing, or lighting.\n\n[Shot 1] Continue the exact motion and sound already in progress, then develop the next action naturally. [DESCRIBE WHAT HAPPENS NEXT; DO NOT RESTART FROM REST.]'
DEFAULT_PROMPT_2 = 'Continue the existing scene from the previous generated H3 clip with no cut, reset, or re-establishment. The incoming protected H3 audiovisual latent prefix is authoritative for current pose, motion, camera trajectory, facial state, lighting, environment, object state, voice, ambience, and timing. Connected reference images are optional; use them only to preserve stable subject identity and appearance beneath that incoming state.\n\n[Shot 1] Continue the exact motion and sound already in progress, then develop the next action naturally. [DESCRIBE WHAT HAPPENS NEXT; DO NOT RESTART FROM REST.]'
DEFAULT_PROMPT_3 = 'Create the opening clip for a new continuous video. Establish coherent subject identity, camera, lighting, environment, motion, voice, ambience, and audiovisual timing so later masked extensions can continue seamlessly. If an H3 keyframe is enabled at frame 1, treat it as the exact opening image and animate naturally forward. Connected reference images are identity/appearance references only and should not force their pose, framing, expression, or lighting onto the shot.'
DEFAULT_PROMPT_4 = 'Regenerate the complete soundtrack for the supplied source video. The entire visual stream is protected and authoritative: do not change, reinterpret, restart, or replace the video. Generate synchronized audio for the full clip from beginning to end, including dialogue/voice when visually implied, foley, impacts, movement sounds, room tone, ambience, and other scene-appropriate sound. Match visible timing precisely and maintain continuous acoustic perspective across the whole source clip.'
DEFAULT_SEED = 123456789
DEFAULT_SEED_2 = 123469134
DEFAULT_SEED_3 = 123481479
DEFAULT_SEED_4 = 123493824
DEFAULT_SEED_5 = 123506169
DEFAULT_SEED_6 = 123518514
DEFAULT_SEED_7 = 123432109
DEFAULT_SEED_8 = 918273645
EXISTING_VIDEO = 'Existing Video'
FIXED = 'fixed'
GUIDE_STRENGTH = 0.95
LORA_NAME = 'minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors'
MATCH = 'match'
UNET_NAME = 'minimax_h3_ref2va_pruned_int8_convrot.safetensors'
VIDEO_H264_MP4 = 'video/h264-mp4'
VIDEO_VAE_NAME = 'minimax_h3_video_vae_int8_convrot.safetensors'
YUV420P = 'yuv420p'

MODEL_ASSETS = {
    'audio_vae': ModelAsset(
        filename=AUDIO_VAE_NAME,
        url='https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors',
        subdir='vae',
    ),
    'turbo_lora': ModelAsset(
        filename=LORA_NAME,
        url='https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors',
        subdir='loras',
    ),
    'ref2va': ModelAsset(
        filename=UNET_NAME,
        url='https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors',
        subdir='diffusion_models',
    ),
    'video_vae': ModelAsset(
        filename=VIDEO_VAE_NAME,
        url='https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_int8_convrot.safetensors',
        subdir='vae',
    ),
    'text_encoder': ModelAsset(
        filename=CLIP_NAME,
        url='https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors',
        subdir='text_encoders',
    ),
}


PUBLIC_INPUT_METADATA = {
    'model': InputSpec(node=ref('unetloader'), field='unet_name', default=None, infer_type=False),
    'seed': InputSpec(node=ref('randomnoise_2'), field='noise_seed', default=None, infer_type=False),
    'steps': InputSpec(node=ref('basicscheduler'), field='steps', default=None, infer_type=False),
}


READY_METADATA = ReadyMetadata.build(
    capability='video',
    template_id='NEW_AV_Extension',
    inputs=PUBLIC_INPUT_METADATA,
    models=MODEL_ASSETS,
    requirements={'missing_nodes': ['MiniMaxH3AVExtensionController', 'MiniMaxH3AVSourceAudioModeParam', 'MiniMaxH3AVStartModeParam', 'MiniMaxH3AudioVAECompatibility', 'MiniMaxH3CropTo32', 'MiniMaxH3CustomKeyframes', 'MiniMaxH3FinalizeVHSOutput', 'MiniMaxH3GeneratedAVMaskedContext', 'MiniMaxH3LastActiveVHSPreviewBarrier', 'MiniMaxH3SourceAudioPolicy', 'MiniMaxH3SourceAudioRegenLength', 'MiniMaxH3SourceAudioRegenMask', 'MiniMaxH3StartCanvasSelector', 'MiniMaxH3StartMaskedContext', 'MiniMaxH3StreamLiveExtensionAVToVHS', 'MiniMaxH3Validate24FPSVideo', 'Note']},
    source_ref='source.json',
    source_kind='raw_json',
    source_path='source.json',
    workflow_source_id='NEW_AV_Extension',
    workflow_source_type='api',
    raw_workflow_shape='ui',
    source_id='NEW_AV_Extension',
    source_type='api',
    source_workflow_path='source.json',
    source_hash='sha256:87a85563ab868d4202a3f37f02e30d4749b865bcf8b050f55991a6c980ca9fa4',
    workflow_shape={'nodes': 102, 'runtime_nodes': 99, 'helper_nodes': 3, 'edges': 262, 'inputs': 2, 'outputs': 1},
    output_mode='scratchpad',
    artifact_class='open_draft',
    execution_ready=False,
    provenance={'operation': 'imported', 'source_kind': 'raw_json', 'source_hash': 'sha256:87a85563ab868d4202a3f37f02e30d4749b865bcf8b050f55991a6c980ca9fa4', 'workflow_shape': {'nodes': 102, 'runtime_nodes': 99, 'helper_nodes': 3, 'edges': 262, 'inputs': 2, 'outputs': 1}, 'output_mode': 'scratchpad', 'source_type': 'api'},
    source_bundle={'format_version': 2, 'generation_id': '19767cde153f7630fe4e492e69a556415e0d123d9f171176a051f8528bb7b6c3', 'custody_digest': 'a038e5b4152f727c3b314b9f2544e145b8b145c712d8ed55efe9113bd5dd73a4'},
)

def build() -> VibeWorkflow:
    """Build the workflow (auto-generated)."""
    with new_workflow(READY_METADATA, source_path=__file__) as wf:

        # Loaders
        unetloader = UNETLoader(unet_name=UNET_NAME, weight_dtype='default')
        cliploader = CLIPLoader(clip_name=CLIP_NAME, type='minimax', device='default')
        vaeloader = VAELoader(vae_name=VIDEO_VAE_NAME)
        vaeloader_2 = VAELoader(vae_name=AUDIO_VAE_NAME)

        vhs_loadvideoffmpeg = VHS_LoadVideoFFmpeg(
            custom_height=0,
            custom_width=0,
            force_rate=24,
            format='AnimateDiff',
            frame_load_cap=0,
            start_time=0,
            video='',
            videopreview={'hidden': False, 'paused': False, 'params': {}, 'muted': False},
        )

        comfymathexpression = ComfyMathExpression(
            expression='max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17',
            **{'values.a': 5},
        )

        randomnoise_2 = RandomNoise(
            noise_seed=DEFAULT_SEED,
            control_after_generate=FIXED,
        )

        randomnoise_3 = RandomNoise(
            noise_seed=DEFAULT_SEED_2,
            control_after_generate=FIXED,
            _mode=4,
        )

        randomnoise_4 = RandomNoise(
            noise_seed=DEFAULT_SEED_3,
            control_after_generate=FIXED,
            _mode=4,
        )

        randomnoise_5 = RandomNoise(
            noise_seed=DEFAULT_SEED_4,
            control_after_generate=FIXED,
            _mode=4,
        )

        randomnoise_6 = RandomNoise(
            noise_seed=DEFAULT_SEED_5,
            control_after_generate=FIXED,
            _mode=4,
        )

        randomnoise_7 = RandomNoise(
            noise_seed=DEFAULT_SEED_6,
            control_after_generate=FIXED,
            _mode=4,
        )

        # Sampling
        ksamplerselect = KSamplerSelect(sampler_name='res_multistep')

        # Inputs
        loadimage_3 = LoadImage(image='', _mode=4)

        randomnoise_8 = RandomNoise(
            noise_seed=DEFAULT_SEED_7,
            control_after_generate=FIXED,
            _mode=4,
        )

        minimaxh3avextensioncontroller = raw_call('MiniMaxH3AVExtensionController',
            _outputs=('start_mode', 'active_extensions', 'audio_feather_ticks', 'preview_mode', 'source_audio_mode'),
            widget_0=EXISTING_VIDEO,
            widget_1=1,
            widget_2=8,
            widget_3='All Active',
            widget_4='Keep source audio',
        )

        loadimage = LoadImage(image='')
        loadimage_2 = LoadImage(image='')

        resolutionselector = ResolutionSelector(
            aspect_ratio='16:9 (Widescreen)',
            megapixels=0.5,
            multiple=32,
        )

        loadaudio = LoadAudio(audio='', _mode=4)
        loadaudio_2 = LoadAudio(audio='', _mode=4)

        randomnoise = RandomNoise(
            noise_seed=DEFAULT_SEED_8,
            control_after_generate=FIXED,
            _mode=4,
        )

        minimaxh3avstartmodeparam = raw_call('MiniMaxH3AVStartModeParam',
            _outputs=('start_mode',),
            widget_0=EXISTING_VIDEO,
        )

        minimaxh3avsourceaudiomodeparam = raw_call('MiniMaxH3AVSourceAudioModeParam',
            _outputs=('source_audio_mode',),
            widget_0='Keep source audio',
        )

        modelattentionbackend = raw_call('ModelAttentionBackend',
            _outputs=('MODEL',),
            attention='comfy kitchen attention',
            model=unetloader.out('MODEL'),
        )

        previewany = PreviewAny(source=comfymathexpression.out('INT'))

        minimaxh3validate24fpsvideo = raw_call('MiniMaxH3Validate24FPSVideo',
            _outputs=('images',),
            images=vhs_loadvideoffmpeg.out('IMAGE'),
            video_info=vhs_loadvideoffmpeg.out('VIDEO_INFO'),
        )

        minimaxh3audiovaecompatibility = raw_call('MiniMaxH3AudioVAECompatibility',
            _outputs=('audio_vae',),
            audio_vae=vaeloader_2.out('VAE'),
        )

        minimaxh3cropto32 = raw_call('MiniMaxH3CropTo32',
            _outputs=('images', 'width', 'height'),
            images=minimaxh3validate24fpsvideo.out('images'),
        )

        loraloadermodelonly = LoraLoaderModelOnly(
            lora_name=LORA_NAME,
            strength_model=GUIDE_STRENGTH,
            model=modelattentionbackend.out('MODEL'),
        )

        minimaxh3sigmashift = raw_call('MiniMaxH3SigmaShift',
            _outputs=('MODEL',),
            shift_audio=3,
            shift_video=12,
            model=loraloadermodelonly.out('MODEL'),
        )

        minimaxh3startcanvasselector = raw_call('MiniMaxH3StartCanvasSelector',
            _outputs=('width', 'height'),
            widget_0=960,
            widget_1=544,
            widget_2=0,
            widget_3=0,
            generated_height=resolutionselector.out('HEIGHT'),
            generated_width=resolutionselector.out('WIDTH'),
            source_height=minimaxh3cropto32.out('height'),
            source_width=minimaxh3cropto32.out('width'),
            start_mode=minimaxh3avstartmodeparam.out('start_mode'),
        )

        minimaxh3sourceaudioregenlength = raw_call('MiniMaxH3SourceAudioRegenLength',
            _outputs=('h3_length', 'source_frames_24fps'),
            source_fps=24,
            source_frames=minimaxh3cropto32.out('images'),
            _mode=4,
        )

        minimaxh3referencetovideo_2 = raw_call('MiniMaxH3ReferenceToVideo',
            _outputs=('positive', 'LATENT'),
            prompt=DEFAULT_PROMPT,
            ref_image_size=MATCH,
            audio_vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            clip=cliploader.out('CLIP'),
            height=minimaxh3startcanvasselector.out('height'),
            length=comfymathexpression.out('INT'),
            vae=vaeloader.out('VAE'),
            width=minimaxh3startcanvasselector.out('width'),
            _extras={'ref_images.ref_image_0': loadimage.out('IMAGE'), 'ref_images.ref_image_1': loadimage_2.out('IMAGE')},
        )

        minimaxh3referencetovideo_3 = raw_call('MiniMaxH3ReferenceToVideo',
            _outputs=('positive', 'LATENT'),
            prompt=DEFAULT_PROMPT_2,
            ref_image_size=MATCH,
            audio_vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            clip=cliploader.out('CLIP'),
            height=minimaxh3startcanvasselector.out('height'),
            length=comfymathexpression.out('INT'),
            vae=vaeloader.out('VAE'),
            width=minimaxh3startcanvasselector.out('width'),
            _mode=4,
            _extras={'ref_images.ref_image_0': loadimage.out('IMAGE'), 'ref_images.ref_image_1': loadimage_2.out('IMAGE')},
        )

        minimaxh3referencetovideo_4 = raw_call('MiniMaxH3ReferenceToVideo',
            _outputs=('positive', 'LATENT'),
            prompt=DEFAULT_PROMPT_2,
            ref_image_size=MATCH,
            audio_vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            clip=cliploader.out('CLIP'),
            height=minimaxh3startcanvasselector.out('height'),
            length=comfymathexpression.out('INT'),
            vae=vaeloader.out('VAE'),
            width=minimaxh3startcanvasselector.out('width'),
            _mode=4,
            _extras={'ref_images.ref_image_0': loadimage.out('IMAGE'), 'ref_images.ref_image_1': loadimage_2.out('IMAGE')},
        )

        minimaxh3referencetovideo_5 = raw_call('MiniMaxH3ReferenceToVideo',
            _outputs=('positive', 'LATENT'),
            prompt=DEFAULT_PROMPT_2,
            ref_image_size=MATCH,
            audio_vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            clip=cliploader.out('CLIP'),
            height=minimaxh3startcanvasselector.out('height'),
            length=comfymathexpression.out('INT'),
            vae=vaeloader.out('VAE'),
            width=minimaxh3startcanvasselector.out('width'),
            _mode=4,
            _extras={'ref_images.ref_image_0': loadimage.out('IMAGE'), 'ref_images.ref_image_1': loadimage_2.out('IMAGE')},
        )

        minimaxh3referencetovideo_6 = raw_call('MiniMaxH3ReferenceToVideo',
            _outputs=('positive', 'LATENT'),
            prompt=DEFAULT_PROMPT_2,
            ref_image_size=MATCH,
            audio_vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            clip=cliploader.out('CLIP'),
            height=minimaxh3startcanvasselector.out('height'),
            length=comfymathexpression.out('INT'),
            vae=vaeloader.out('VAE'),
            width=minimaxh3startcanvasselector.out('width'),
            _mode=4,
            _extras={'ref_images.ref_image_0': loadimage.out('IMAGE'), 'ref_images.ref_image_1': loadimage_2.out('IMAGE')},
        )

        minimaxh3referencetovideo_7 = raw_call('MiniMaxH3ReferenceToVideo',
            _outputs=('positive', 'LATENT'),
            prompt=DEFAULT_PROMPT_2,
            ref_image_size=MATCH,
            audio_vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            clip=cliploader.out('CLIP'),
            height=minimaxh3startcanvasselector.out('height'),
            length=comfymathexpression.out('INT'),
            vae=vaeloader.out('VAE'),
            width=minimaxh3startcanvasselector.out('width'),
            _mode=4,
            _extras={'ref_images.ref_image_0': loadimage.out('IMAGE'), 'ref_images.ref_image_1': loadimage_2.out('IMAGE')},
        )

        minimaxh3referencetovideo_8 = raw_call('MiniMaxH3ReferenceToVideo',
            _outputs=('positive', 'LATENT'),
            prompt=DEFAULT_PROMPT_3,
            ref_image_size=MATCH,
            audio_vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            clip=cliploader.out('CLIP'),
            height=minimaxh3startcanvasselector.out('height'),
            length=comfymathexpression.out('INT'),
            vae=vaeloader.out('VAE'),
            width=minimaxh3startcanvasselector.out('width'),
            _mode=4,
            _extras={'ref_images.ref_image_0': loadimage.out('IMAGE'), 'ref_images.ref_image_1': loadimage_2.out('IMAGE')},
        )

        basicscheduler = BasicScheduler(
            scheduler='simple',
            steps=8,
            denoise=1,
            model=minimaxh3sigmashift.out('MODEL'),
        )

        minimaxh3referencetovideo = raw_call('MiniMaxH3ReferenceToVideo',
            _outputs=('positive', 'LATENT'),
            prompt=DEFAULT_PROMPT_4,
            ref_image_size=MATCH,
            audio_vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            clip=cliploader.out('CLIP'),
            height=minimaxh3cropto32.out('height'),
            length=minimaxh3sourceaudioregenlength.out('h3_length'),
            vae=vaeloader.out('VAE'),
            width=minimaxh3cropto32.out('width'),
            _mode=4,
            _extras={'ref_images.ref_image_0': loadimage.out('IMAGE'), 'ref_images.ref_image_1': loadimage_2.out('IMAGE')},
        )

        basicguider_2 = BasicGuider(
            conditioning=minimaxh3referencetovideo_2.out('positive'),
            model=minimaxh3sigmashift.out('MODEL'),
        )

        basicguider_3 = BasicGuider(
            conditioning=minimaxh3referencetovideo_3.out('positive'),
            model=minimaxh3sigmashift.out('MODEL'),
            _mode=4,
        )

        basicguider_4 = BasicGuider(
            conditioning=minimaxh3referencetovideo_4.out('positive'),
            model=minimaxh3sigmashift.out('MODEL'),
            _mode=4,
        )

        basicguider_5 = BasicGuider(
            conditioning=minimaxh3referencetovideo_5.out('positive'),
            model=minimaxh3sigmashift.out('MODEL'),
            _mode=4,
        )

        basicguider_6 = BasicGuider(
            conditioning=minimaxh3referencetovideo_6.out('positive'),
            model=minimaxh3sigmashift.out('MODEL'),
            _mode=4,
        )

        basicguider_7 = BasicGuider(
            conditioning=minimaxh3referencetovideo_7.out('positive'),
            model=minimaxh3sigmashift.out('MODEL'),
            _mode=4,
        )

        minimaxh3customkeyframes = raw_call('MiniMaxH3CustomKeyframes',
            _outputs=('conditioning',),
            widget_0='{"count":1,"positions":[1]}',
            widget_1='1-based',
            widget_2='disabled',
            conditioning=minimaxh3referencetovideo_8.out('positive'),
            keyframe_image_1=loadimage_3.out('IMAGE'),
            latent=minimaxh3referencetovideo_8.out('LATENT'),
            vae=vaeloader.out('VAE'),
            _mode=4,
        )

        minimaxh3sourceaudioregenmask = raw_call('MiniMaxH3SourceAudioRegenMask',
            _outputs=('latent',),
            crop='disabled',
            source_fps=24,
            latent=minimaxh3referencetovideo.out('LATENT'),
            source_frames=minimaxh3cropto32.out('images'),
            vae=vaeloader.out('VAE'),
            _mode=4,
        )

        basicguider = BasicGuider(
            conditioning=minimaxh3referencetovideo.out('positive'),
            model=minimaxh3sigmashift.out('MODEL'),
            _mode=4,
        )

        basicguider_8 = BasicGuider(
            conditioning=minimaxh3customkeyframes.out('conditioning'),
            model=minimaxh3sigmashift.out('MODEL'),
            _mode=4,
        )

        samplercustomadvanced = SamplerCustomAdvanced(
            guider=basicguider.out('GUIDER'),
            latent_image=minimaxh3sourceaudioregenmask.out('latent'),
            noise=randomnoise.out('NOISE'),
            sampler=ksamplerselect.out('SAMPLER'),
            sigmas=basicscheduler.out('SIGMAS'),
            _mode=4,
        )

        samplercustomadvanced_8 = SamplerCustomAdvanced(
            guider=basicguider_8.out('GUIDER'),
            latent_image=minimaxh3referencetovideo_8.out('LATENT'),
            noise=randomnoise_8.out('NOISE'),
            sampler=ksamplerselect.out('SAMPLER'),
            sigmas=basicscheduler.out('SIGMAS'),
            _mode=4,
        )

        minimaxh3sourceaudiopolicy = raw_call('MiniMaxH3SourceAudioPolicy',
            _outputs=('source_audio',),
            source_fps=24,
            audio_vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            mode=minimaxh3avsourceaudiomodeparam.out('source_audio_mode'),
            regenerated_latent=samplercustomadvanced.out('OUTPUT'),
            source_frames=minimaxh3cropto32.out('images'),
            video_info=vhs_loadvideoffmpeg.out('VIDEO_INFO'),
        )

        minimaxh3startmaskedcontext = raw_call('MiniMaxH3StartMaskedContext',
            _outputs=('latent', 'trim_frames'),
            audio_feather_ticks=8,
            context_length=39,
            widget_0=39,
            widget_1=8,
            widget_2=24,
            widget_3='disabled',
            audio_vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            latent=minimaxh3referencetovideo_2.out('LATENT'),
            live_starter_latent=samplercustomadvanced_8.out('OUTPUT'),
            source_audio=minimaxh3sourceaudiopolicy.out('source_audio'),
            source_frames=minimaxh3cropto32.out('images'),
            start_mode=minimaxh3avstartmodeparam.out('start_mode'),
            vae=vaeloader.out('VAE'),
        )

        # Decode
        vaedecode_3 = VAEDecode(
            samples=samplercustomadvanced_8.out('OUTPUT'),
            vae=vaeloader.out('VAE'),
            _mode=4,
        )

        vaedecodeaudio_4 = VAEDecodeAudio(
            samples=samplercustomadvanced_8.out('OUTPUT'),
            vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            _mode=4,
        )

        samplercustomadvanced_2 = SamplerCustomAdvanced(
            guider=basicguider_2.out('GUIDER'),
            latent_image=minimaxh3startmaskedcontext.out('latent'),
            noise=randomnoise_2.out('NOISE'),
            sampler=ksamplerselect.out('SAMPLER'),
            sigmas=basicscheduler.out('SIGMAS'),
        )

        # Outputs
        vhs_videocombine_4 = VHS_VideoCombine(
            frame_rate=24,
            loop_count=0,
            filename_prefix='h3_preview/generated_starter',
            format=VIDEO_H264_MP4,
            pingpong=False,
            save_output=False,
            crf=19,
            pix_fmt=YUV420P,
            save_metadata=False,
            trim_to_audio=True,
            videopreview={'hidden': False, 'paused': False, 'params': {}},
            audio=vaedecodeaudio_4.out('AUDIO'),
            images=vaedecode_3.out('IMAGE'),
            _mode=4,
        )

        minimaxh3generatedavmaskedcontext = raw_call('MiniMaxH3GeneratedAVMaskedContext',
            _outputs=('latent', 'trim_frames'),
            widget_0=39,
            widget_1=8,
            latent=minimaxh3referencetovideo_3.out('LATENT'),
            source_latent=samplercustomadvanced_2.out('OUTPUT'),
            _mode=4,
        )

        vaedecode_4 = VAEDecode(
            samples=samplercustomadvanced_2.out('OUTPUT'),
            vae=vaeloader.out('VAE'),
        )

        vaedecodeaudio_5 = VAEDecodeAudio(
            samples=samplercustomadvanced_2.out('OUTPUT'),
            vae=minimaxh3audiovaecompatibility.out('audio_vae'),
        )

        samplercustomadvanced_3 = SamplerCustomAdvanced(
            guider=basicguider_3.out('GUIDER'),
            latent_image=minimaxh3generatedavmaskedcontext.out('latent'),
            noise=randomnoise_3.out('NOISE'),
            sampler=ksamplerselect.out('SAMPLER'),
            sigmas=basicscheduler.out('SIGMAS'),
            _mode=4,
        )

        vhs_videocombine_5 = VHS_VideoCombine(
            frame_rate=24,
            loop_count=0,
            filename_prefix='h3_preview/extension_01',
            format=VIDEO_H264_MP4,
            pingpong=False,
            save_output=False,
            crf=19,
            pix_fmt=YUV420P,
            save_metadata=False,
            trim_to_audio=True,
            videopreview={'hidden': False, 'paused': False, 'params': {}},
            audio=vaedecodeaudio_5.out('AUDIO'),
            images=vaedecode_4.out('IMAGE'),
        )

        minimaxh3generatedavmaskedcontext_2 = raw_call('MiniMaxH3GeneratedAVMaskedContext',
            _outputs=('latent', 'trim_frames'),
            widget_0=39,
            widget_1=8,
            latent=minimaxh3referencetovideo_4.out('LATENT'),
            source_latent=samplercustomadvanced_3.out('OUTPUT'),
            _mode=4,
        )

        vaedecode_5 = VAEDecode(
            samples=samplercustomadvanced_3.out('OUTPUT'),
            vae=vaeloader.out('VAE'),
            _mode=4,
        )

        vaedecodeaudio_6 = VAEDecodeAudio(
            samples=samplercustomadvanced_3.out('OUTPUT'),
            vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            _mode=4,
        )

        samplercustomadvanced_4 = SamplerCustomAdvanced(
            guider=basicguider_4.out('GUIDER'),
            latent_image=minimaxh3generatedavmaskedcontext_2.out('latent'),
            noise=randomnoise_4.out('NOISE'),
            sampler=ksamplerselect.out('SAMPLER'),
            sigmas=basicscheduler.out('SIGMAS'),
            _mode=4,
        )

        vhs_videocombine_6 = VHS_VideoCombine(
            frame_rate=24,
            loop_count=0,
            filename_prefix='h3_preview/extension_02',
            format=VIDEO_H264_MP4,
            pingpong=False,
            save_output=False,
            crf=19,
            pix_fmt=YUV420P,
            save_metadata=False,
            trim_to_audio=True,
            videopreview={'hidden': False, 'paused': False, 'params': {}},
            audio=vaedecodeaudio_6.out('AUDIO'),
            images=vaedecode_5.out('IMAGE'),
            _mode=4,
        )

        minimaxh3generatedavmaskedcontext_3 = raw_call('MiniMaxH3GeneratedAVMaskedContext',
            _outputs=('latent', 'trim_frames'),
            widget_0=39,
            widget_1=8,
            latent=minimaxh3referencetovideo_5.out('LATENT'),
            source_latent=samplercustomadvanced_4.out('OUTPUT'),
            _mode=4,
        )

        vaedecode_6 = VAEDecode(
            samples=samplercustomadvanced_4.out('OUTPUT'),
            vae=vaeloader.out('VAE'),
            _mode=4,
        )

        vaedecodeaudio_7 = VAEDecodeAudio(
            samples=samplercustomadvanced_4.out('OUTPUT'),
            vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            _mode=4,
        )

        samplercustomadvanced_5 = SamplerCustomAdvanced(
            guider=basicguider_5.out('GUIDER'),
            latent_image=minimaxh3generatedavmaskedcontext_3.out('latent'),
            noise=randomnoise_5.out('NOISE'),
            sampler=ksamplerselect.out('SAMPLER'),
            sigmas=basicscheduler.out('SIGMAS'),
            _mode=4,
        )

        vhs_videocombine_7 = VHS_VideoCombine(
            frame_rate=24,
            loop_count=0,
            filename_prefix='h3_preview/extension_03',
            format=VIDEO_H264_MP4,
            pingpong=False,
            save_output=False,
            crf=19,
            pix_fmt=YUV420P,
            save_metadata=False,
            trim_to_audio=True,
            videopreview={'hidden': False, 'paused': False, 'params': {}},
            audio=vaedecodeaudio_7.out('AUDIO'),
            images=vaedecode_6.out('IMAGE'),
            _mode=4,
        )

        minimaxh3generatedavmaskedcontext_4 = raw_call('MiniMaxH3GeneratedAVMaskedContext',
            _outputs=('latent', 'trim_frames'),
            widget_0=39,
            widget_1=8,
            latent=minimaxh3referencetovideo_6.out('LATENT'),
            source_latent=samplercustomadvanced_5.out('OUTPUT'),
            _mode=4,
        )

        vaedecode_7 = VAEDecode(
            samples=samplercustomadvanced_5.out('OUTPUT'),
            vae=vaeloader.out('VAE'),
            _mode=4,
        )

        vaedecodeaudio = VAEDecodeAudio(
            samples=samplercustomadvanced_5.out('OUTPUT'),
            vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            _mode=4,
        )

        samplercustomadvanced_6 = SamplerCustomAdvanced(
            guider=basicguider_6.out('GUIDER'),
            latent_image=minimaxh3generatedavmaskedcontext_4.out('latent'),
            noise=randomnoise_6.out('NOISE'),
            sampler=ksamplerselect.out('SAMPLER'),
            sigmas=basicscheduler.out('SIGMAS'),
            _mode=4,
        )

        vhs_videocombine = VHS_VideoCombine(
            frame_rate=24,
            loop_count=0,
            filename_prefix='h3_preview/extension_04',
            format=VIDEO_H264_MP4,
            pingpong=False,
            save_output=False,
            crf=19,
            pix_fmt=YUV420P,
            save_metadata=False,
            trim_to_audio=True,
            videopreview={'hidden': False, 'paused': False, 'params': {}},
            audio=vaedecodeaudio.out('AUDIO'),
            images=vaedecode_7.out('IMAGE'),
            _mode=4,
        )

        minimaxh3generatedavmaskedcontext_5 = raw_call('MiniMaxH3GeneratedAVMaskedContext',
            _outputs=('latent', 'trim_frames'),
            widget_0=39,
            widget_1=8,
            latent=minimaxh3referencetovideo_7.out('LATENT'),
            source_latent=samplercustomadvanced_6.out('OUTPUT'),
            _mode=4,
        )

        vaedecode = VAEDecode(
            samples=samplercustomadvanced_6.out('OUTPUT'),
            vae=vaeloader.out('VAE'),
            _mode=4,
        )

        vaedecodeaudio_2 = VAEDecodeAudio(
            samples=samplercustomadvanced_6.out('OUTPUT'),
            vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            _mode=4,
        )

        samplercustomadvanced_7 = SamplerCustomAdvanced(
            guider=basicguider_7.out('GUIDER'),
            latent_image=minimaxh3generatedavmaskedcontext_5.out('latent'),
            noise=randomnoise_7.out('NOISE'),
            sampler=ksamplerselect.out('SAMPLER'),
            sigmas=basicscheduler.out('SIGMAS'),
            _mode=4,
        )

        vhs_videocombine_2 = VHS_VideoCombine(
            frame_rate=24,
            loop_count=0,
            filename_prefix='h3_preview/extension_05',
            format=VIDEO_H264_MP4,
            pingpong=False,
            save_output=False,
            crf=19,
            pix_fmt=YUV420P,
            save_metadata=False,
            trim_to_audio=True,
            videopreview={'hidden': False, 'paused': False, 'params': {}},
            audio=vaedecodeaudio_2.out('AUDIO'),
            images=vaedecode.out('IMAGE'),
            _mode=4,
        )

        vaedecode_2 = VAEDecode(
            samples=samplercustomadvanced_7.out('OUTPUT'),
            vae=vaeloader.out('VAE'),
            _mode=4,
        )

        vaedecodeaudio_3 = VAEDecodeAudio(
            samples=samplercustomadvanced_7.out('OUTPUT'),
            vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            _mode=4,
        )

        vhs_videocombine_3 = VHS_VideoCombine(
            frame_rate=24,
            loop_count=0,
            filename_prefix='h3_preview/extension_06',
            format=VIDEO_H264_MP4,
            pingpong=False,
            save_output=False,
            crf=19,
            pix_fmt=YUV420P,
            save_metadata=False,
            trim_to_audio=True,
            videopreview={'hidden': False, 'paused': False, 'params': {}},
            audio=vaedecodeaudio_3.out('AUDIO'),
            images=vaedecode_2.out('IMAGE'),
            _mode=4,
        )

        minimaxh3lastactivevhspreviewbarrier = raw_call('MiniMaxH3LastActiveVHSPreviewBarrier',
            _outputs=('preview_gate',),
            active_extensions=1,
            widget_0=6,
            preview_1=vhs_videocombine_5.out('FILENAMES'),
            preview_2=vhs_videocombine_6.out('FILENAMES'),
            preview_3=vhs_videocombine_7.out('FILENAMES'),
            preview_4=vhs_videocombine.out('FILENAMES'),
            preview_5=vhs_videocombine_2.out('FILENAMES'),
            preview_6=vhs_videocombine_3.out('FILENAMES'),
        )

        minimaxh3streamliveextensionavtovhs = raw_call('MiniMaxH3StreamLiveExtensionAVToVHS',
            _outputs=('Filenames',),
            active_extensions=1,
            context_frames=39,
            video_overlap_frames=39,
            widget_0=6,
            widget_1=39,
            widget_10=True,
            widget_2=39,
            widget_3=24,
            widget_4='disabled',
            widget_5='video/masked_av_extension',
            widget_6='yuv420p',
            widget_7=19,
            widget_8=False,
            widget_9=True,
            audio_vae=minimaxh3audiovaecompatibility.out('audio_vae'),
            extension_1=samplercustomadvanced_2.out('OUTPUT'),
            extension_2=samplercustomadvanced_3.out('OUTPUT'),
            extension_3=samplercustomadvanced_4.out('OUTPUT'),
            extension_4=samplercustomadvanced_5.out('OUTPUT'),
            extension_5=samplercustomadvanced_6.out('OUTPUT'),
            extension_6=samplercustomadvanced_7.out('OUTPUT'),
            preview_gate=minimaxh3lastactivevhspreviewbarrier.out('preview_gate'),
            source_audio=minimaxh3sourceaudiopolicy.out('source_audio'),
            source_frames=minimaxh3cropto32.out('images'),
            start_mode=minimaxh3avstartmodeparam.out('start_mode'),
            starter_latent=samplercustomadvanced_8.out('OUTPUT'),
            video_vae=vaeloader.out('VAE'),
        )

        minimaxh3finalizevhsoutput = raw_call('MiniMaxH3FinalizeVHSOutput',
            filenames=minimaxh3streamliveextensionavtovhs.out('Filenames'),
        )

        wf = wf.finalize(PUBLIC_INPUT_METADATA, outputs=[OutputSpec(node=vhs_videocombine_5)])
        wf.strict_types = False
        return wf
