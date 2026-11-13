"""Provisioning logic for the local ComfyUI installation.

Everything in this package is pure Python 3.12 standard library. It runs with
the system interpreter before a virtualenv exists, and it never mutates the
machine: plans are computed here, bash executes them.

Public entry point: ``python -m comfyprov``.
"""

__all__ = ["__version__"]

__version__ = "2.0.0"
