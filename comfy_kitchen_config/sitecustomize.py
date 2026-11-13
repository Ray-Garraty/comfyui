# sitecustomize.py — automatically imported by Python at startup.
# On RDNA2 (gfx1031, no matrix cores) comfy-kitchen int8 matmul crashes with
# HIPBLAS_STATUS_INVALID_VALUE when calling torch._int_mm (hipBLAS Lt).
# Patch torch._int_mm to fall back to bf16 matmul so int8 quantized models
# (Jib Mix, SFT) can run on this hardware.
import os
import sys

sys.stderr.write("[sitecustomize] loading...\n")

# Mark the HIP backend unavailable before comfy-kitchen registers dispatch
# handlers, so it does not try to use hipBLAS int8 matmul on RDNA2.
os.environ.setdefault("COMFY_KITCHEN_DISABLE_HIP", "1")

try:
    import comfy_kitchen

    sys.stderr.write("[sitecustomize] comfy-kitchen loaded\n")
except ModuleNotFoundError:
    sys.stderr.write("[sitecustomize] comfy_kitchen not found\n")
    comfy_kitchen = None

try:
    import torch

    _orig_int_mm = torch._int_mm

    def _patched_int_mm(a, b):
        """bf16 fallback for int8 matmul on RDNA2 without matrix cores.

        Tries the original int8 matmul first; if HIPBLAS rejects it
        (RDNA2 has no matrix cores), falls back to bf16 matmul.
        """
        try:
            return _orig_int_mm(a, b)
        except RuntimeError:
            return (a.to(torch.bfloat16) @ b.to(torch.bfloat16)).to(torch.int32)

    torch._int_mm = _patched_int_mm
    if hasattr(torch, "int8_mm"):
        torch.int8_mm = _patched_int_mm
    sys.stderr.write("[sitecustomize] torch._int_mm patched to bf16 fallback\n")

except ModuleNotFoundError:
    sys.stderr.write("[sitecustomize] torch not found\n")
    torch = None

# Patch comfy-kitchen's register_layout_op so no int8 handler is ever
# registered for aten.linear.default.  ComfyUI imports the int8/svdquant
# /nvfp4/fp8 tensor modules lazily (during model load), so patching the
# registrar prevents the crash entirely.  Registered handlers fall through
# to QuantizedTensor._dequant_and_fallback (dequantize -> regular linear).
if comfy_kitchen is not None and torch is not None:
    try:
        from comfy_kitchen.tensor import base as _ck_base

        _orig_register = _ck_base.register_layout_op

        def _filtered_register(torch_op, layout_cls):
            if torch_op is torch.ops.aten.linear.default:
                # No-op decorator: skip registering int8 linear handlers.
                return lambda f: f
            return _orig_register(torch_op, layout_cls)

        _ck_base.register_layout_op = _filtered_register

        # Also clear any linear handlers already registered (in case a
        # tensor module was imported before sitecustomize ran).
        linear_op = torch.ops.aten.linear.default
        _ck_base._LAYOUT_DISPATCH_TABLE.pop(linear_op, None)

        sys.stderr.write(
            "[sitecustomize] comfy-kitchen register_layout_op filtered: "
            "aten.linear.default -> dequantized fallback\n"
        )
    except Exception as exc:  # pragma: no cover - defensive
        sys.stderr.write(
            f"[sitecustomize] could not filter register_layout_op: {exc}\n"
        )
