"""The CPU target advertises its supplied model inventory without relaxing choices."""
from pathlib import Path

from tests.helpers.h3_cpu_runtime import _object_info_from_checkout


def test_managed_schema_choices_include_only_supplied_inventory_additions():
    import vibecomfy

    checkout = Path(vibecomfy.__file__).resolve().parents[1]
    baseline = _object_info_from_checkout(checkout)
    paths = (
        "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
        "vae/minimax_h3_audio_vae_fp32.safetensors",
        "vae/minimax_h3_video_vae_int8_convrot.safetensors",
        "loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
    )
    selected = _object_info_from_checkout(checkout, model_paths=paths)
    for class_type, field, folder in (
        ("UNETLoader", "unet_name", "diffusion_models"),
        ("CLIPLoader", "clip_name", "text_encoders"),
        ("VAELoader", "vae_name", "vae"),
        ("LoraLoaderModelOnly", "lora_name", "loras"),
    ):
        original = set(baseline[class_type]["input"]["required"][field][0])
        choices = selected[class_type]["input"]["required"][field][0]
        expected = {path.split("/", 1)[1] for path in paths if path.startswith(folder + "/")}
        assert set(choices) == original | expected
        assert "uninstalled-model.safetensors" not in choices
    assert selected["CLIPLoader"]["input"]["required"]["type"] == baseline["CLIPLoader"]["input"]["required"]["type"]
