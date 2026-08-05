from __future__ import annotations

from aigateway_api.local_generation import builtin_presets


def test_wan22_preset_advertises_chinese_and_english():
    presets = {
        preset["id"]: preset
        for preset in builtin_presets(
            {
                "checkpoint_name": "default.safetensors",
                "allowed_checkpoints": ["default.safetensors"],
                "video_enabled": True,
                "video_diffusion_model": "wan.safetensors",
                "video_text_encoder": "wan-clip.safetensors",
                "video_vae": "wan-vae.safetensors",
            }
        )
    }

    assert presets["wan2.2-ti2v-5b"]["languages"] == ["zh", "en"]
