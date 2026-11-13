# sitecustomize.py — automatically imported by Python at startup.
# On RDNA2 (gfx1031, no matrix cores) comfy-kitchen int8 matmul crashes with
# HIPBLAS_STATUS_INVALID_VALUE when calling torch._int_mm (hipBLAS Lt).
# Patch torch._int_mm to fall back to bf16 matmul so int8 quantized models
# (Jib Mix, SFT) can run on this hardware.
import sys

sys.stderr.write("[sitecustomize] loading...\n")

try:
    import comfy_kitchen

    comfy_kitchen.disable_backend("hip")
    sys.stderr.write("[sitecustomize] comfy-kitchen hip backend disabled\n")
except ModuleNotFoundError:
    sys.stderr.write("[sitecustomize] comfy_kitchen not found\n")

try:
    import torch

    _orig_int_mm = torch._int_mm

    def _patched_int_mm(a, b):
        """bf16 fallback for int8 matmul on RDNA2 without matrix cores."""
        return (a.to(torch.bfloat16) @ b.to(torch.bfloat16)).to(torch.int32)

    torch._int_mm = _patched_int_mm
    if hasattr(torch, "int8_mm"):
        torch.int8_mm = _patched_int_mm
    sys.stderr.write("[sitecustomize] torch._int_mm patched to bf16 fallback\n")
except ModuleNotFoundError:
    sys.stderr.write("[sitecustomize] torch not found\n")
