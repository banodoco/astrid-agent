# ported from ready_templates/sources/official/image/qwen_image_2_1_turbo.json (sha256: 3eb9cef2866c0186fbe30172701f112b35e574cf3c6575c6e4a6b5dd1e76a403)
"""Official Qwen Image 2.1 Turbo default path, with prompt enhancement off."""
from __future__ import annotations

from vibecomfy.templates import InputSpec, ModelAsset, ReadyMetadata, new_workflow, node
from vibecomfy.nodes.core import CLIPLoader, VAELoader, UNETLoader, EmptyLatentImage, LoadImage, ManualSigmas, KSamplerSelect, RandomNoise, CFGGuider, SamplerCustomAdvanced, VAEDecode, SaveImage

REVISION = 'aacf31a6d48e707f352ecba714cee0680da3479b'
BASE = f'https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/{REVISION}'
UNET_NAME = 'qwen_image_2.1_turbo_int8_convrot.safetensors'
CLIP_NAME = 'qwen3vl_8b_int8_convrot.safetensors'
VAE_NAME = 'qwen_image_2.1_vae_bf16.safetensors'
MODELS = {
    'diffusion_model': ModelAsset(url=f'{BASE}/diffusion_models/{UNET_NAME}', subdir='diffusion_models', hf_revision=REVISION, sha256='8144a0182964aa8403ad6dbc011db837304167d235161b9aabaa8532e967314c', size_bytes=7256787829),
    'text_encoder': ModelAsset(url=f'{BASE}/text_encoders/{CLIP_NAME}', subdir='text_encoders', hf_revision=REVISION, sha256='8bfd0f6e12abf2d2d697ecc888e5e90b0d6741d6708f05799f53afa560452e8f', size_bytes=9350798360),
    'vae': ModelAsset(url=f'{BASE}/vae/{VAE_NAME}', subdir='vae', hf_revision=REVISION, sha256='bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9', size_bytes=675509688),
}
PUBLIC_INPUT_METADATA = {
    'diffusion_model': InputSpec(node='1', field='unet_name', default=UNET_NAME, type='CHOICE'),
    'text_encoder': InputSpec(node='2', field='clip_name', default=CLIP_NAME, type='CHOICE'),
    'vae': InputSpec(node='3', field='vae_name', default=VAE_NAME, type='CHOICE'),
    'prompt': InputSpec(node='5', field='prompt', default='', type='STRING', required=True, media_semantics='text'),
    'negative_prompt': InputSpec(node='5', field='negative_prompt', default='', type='STRING', media_semantics='text'),
    'seed': InputSpec(node='8', field='noise_seed', default=0, type='INT'),
    'width': InputSpec(node='4', field='width', default=1024, type='INT'),
    'height': InputSpec(node='4', field='height', default=1024, type='INT'),
}
READY_METADATA = ReadyMetadata.build(capability='image', inputs=PUBLIC_INPUT_METADATA, models=MODELS, unbound_inputs={},
    provenance={'source_workflow': 'ready_templates/sources/official/image/qwen_image_2_1_turbo.json'})


def build():
    wf = new_workflow(READY_METADATA, source_path=__file__)
    unet = UNETLoader(_id='1', unet_name=UNET_NAME, weight_dtype='default')
    clip = CLIPLoader(_id='2', clip_name=CLIP_NAME, type='qwen_image')
    vae = VAELoader(_id='3', vae_name=VAE_NAME)
    latent = EmptyLatentImage(_id='4', width=1024, height=1024, batch_size=1)
    encoded = node('TextEncodeQwenImage21', _id='5', _native_ports={'native_input_names': ['clip', 'prompt', 'negative_prompt', 'vae', 'resolution', 'images.image_1'], 'native_input_types': ['CLIP', 'STRING', 'STRING', 'VAE', 'INT', 'IMAGE'], 'native_output_names': ['positive', 'negative', 'latent'], 'native_output_types': ['CONDITIONING', 'CONDITIONING', 'LATENT']}, clip=clip, prompt='', negative_prompt='', resolution=1024)
    model = node('QwenImage21Cache', _id='6', _outputs=('MODEL',), model=unet, device='auto', dtype='default')
    sigmas = ManualSigmas(_id='7', sigmas='1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0')
    noise = RandomNoise(_id='8', noise_seed=0)
    sampler = KSamplerSelect(_id='9', sampler_name='euler')
    guider = CFGGuider(_id='10', model=model, positive=encoded.out(0), negative=encoded.out(1), cfg=1.0)
    samples = SamplerCustomAdvanced(_id='11', noise=noise, guider=guider, sampler=sampler, sigmas=sigmas, latent_image=latent)
    decoded = VAEDecode(_id='12', samples=samples.out(0), vae=vae)
    saved = SaveImage(_id='13', filename_prefix='Qwen_image_2.1', images=decoded)
    return wf.finalize(PUBLIC_INPUT_METADATA, output_node=saved, output_type='SaveImage', name='image', artifact_kind='image', mime_type='image/png', expected_cardinality='one', filename_prefix='Qwen_image_2.1')
