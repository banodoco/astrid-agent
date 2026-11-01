# FAL sprite frame upscale

Use `fal.sprite_upscale` after `rendering.sprite_sheet` when AI upscaling is
explicitly requested. It accepts the generated PNG frame directory and writes
upscaled frames plus a per-frame report. Ordinary sprite generation stays local
and does not require FAL or a FAL credential.

The FAL action requires the `fal-client` package and `FAL_KEY`. Credentials are
resolved through Astrid's scoped credential resolver; an env file is used only
when passed explicitly.
