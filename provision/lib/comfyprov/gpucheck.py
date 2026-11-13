"""PyTorch/ROCm sanity check.

Runs *inside* the virtualenv (``venv/bin/python -m comfyprov.gpucheck``) and
prints one line per check. Exit code 0 means the GPU is usable.
"""

from __future__ import annotations

import sys


def line(status: str, name: str, detail: str = "") -> None:
    suffix = f" ({detail})" if detail else ""
    print(f"  {status:<5} {name}{suffix}")


def main() -> int:
    try:
        import torch  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports]
    except Exception as exc:
        line("FAIL", "import torch", str(exc))
        return 2

    line("OK", "torch", torch.__version__)
    if torch.version.hip is None:
        line("FAIL", "ROCm build", f"torch.version.hip is None, cuda={torch.version.cuda}")
        return 2
    line("OK", "ROCm build", f"hip {torch.version.hip}")

    if not torch.cuda.is_available():
        line("FAIL", "device availability", "torch.cuda.is_available() is False")
        return 2

    line("OK", "device count", str(torch.cuda.device_count()))
    line("OK", "device 0", torch.cuda.get_device_name(0))

    try:
        props = torch.cuda.get_device_properties(0)
        line("OK", "arch", getattr(props, "gcnArchName", "unknown"))
        line("OK", "VRAM", f"{props.total_memory / 1024**3:.1f} GiB")
    except Exception as exc:
        line("WARN", "device properties", str(exc))

    failures = 0
    for dtype_name, dtype in (("float16", torch.float16), ("bfloat16", torch.bfloat16)):
        try:
            a = torch.randn(1024, 1024, device="cuda", dtype=dtype)
            b = a @ a
            torch.cuda.synchronize()
            value = b.float().abs().mean().item()
            line("OK", f"matmul {dtype_name}", f"mean|ab|={value:.4f}")
        except Exception as exc:
            # bfloat16 is the dtype MOSS uses internally; float16 is the hard requirement.
            status = "FAIL" if dtype_name == "float16" else "WARN"
            if status == "FAIL":
                failures += 1
            line(status, f"matmul {dtype_name}", str(exc).splitlines()[0])

    try:
        free, total = torch.cuda.mem_get_info()
        line("OK", "memory", f"{free / 1024**3:.1f} GiB free of {total / 1024**3:.1f} GiB")
    except Exception as exc:
        line("WARN", "memory", str(exc).splitlines()[0])

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
