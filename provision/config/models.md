# Generative AI model settings

This file holds settings specific to downloadable generative models.
It is intentionally separate from `AGENTS.md` so model specifics do not
clutter the project guidance.

## Model sets

### jibmix — Jib Mix Moss Realistic (MOSS Image, not SDXL)

Required files:

- `Jib_Mix_Moss_V2_H_int8_00001_.safetensors` — diffusion model (Civitai)
- `qwen3.5_9b_w4a8_gptq.safetensors` — Qwen3.5 text encoder (Hugging Face)
- `flux2-vae_small.safetensors` — FLUX.2 VAE (Hugging Face)

### sft — official MOSS Image SFT

Required files:

- `moss_image_sft_int8_convrot.safetensors` — diffusion model (Hugging Face)
- `qwen3.5_9b_w4a8_gptq.safetensors` — Qwen3.5 text encoder (Hugging Face)
- `flux2-vae_small.safetensors` — FLUX.2 VAE (Hugging Face)

## Civitai

- Civitai requires an API token (stored at `$CIVITAI_TOKEN_FILE`, mode `600`).
- Hugging Face files are public; no token needed.

## Download properties

- Resume via `.part`, retries, size + SHA256 verification, atomic rename.
- Verification is delegated to `comfyprov verify-file`, so the manifest check
  has exactly one implementation.

## Recommended generation settings

- Jib Mix: 4–9 steps, CFG 1, sampler `er_sde`, scheduler `simple`.
- Official SFT: closer to 25 steps / CFG 2.5.

## Metadata in downloaded images

Images downloaded from Civitai (including civitai.red) may embed generation
metadata:
- PNG: ComfyUI parameters stored in tEXt chunks (prompt, steps, CFG, sampler,
  scheduler, seed, model hash).
- JPEG: EXIF metadata may contain the same information.

These metadata can be reused to reproduce the generation settings. ComfyUI
itself can read PNG metadata via the `PNG Info` node.

## Hardware note

RDNA2 (RX 6700 XT, gfx1031) has no matrix cores; `comfy-kitchen` int8/WMMA
paths may fall back to slower kernels. This is expected, not a provisioning bug.
