"""Model manifest: parsing, set selection and file verification.

The manifest (``provision/config/models.tsv``) is the single source of truth
for filenames, sizes, checksums and URLs. Both the planner and ``--check`` use
the same verification code, so "verified" means exactly one thing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

MANIFEST_PATH = Path(__file__).resolve().parents[2] / "config" / "models.tsv"

SETS: dict[str, tuple[str, ...]] = {
    "jibmix": ("jibmix_v2", "moss_te", "moss_vae"),
    "sft": ("moss_sft", "moss_te", "moss_vae"),
    "both": ("moss_sft", "jibmix_v2", "moss_te", "moss_vae"),
}

# Model kind metadata used by verify and by the launcher summary.
VERIFY_STEPS = {"jibmix_v2": 8, "moss_sft": 25}
VERIFY_CFG = {"jibmix_v2": 1.0, "moss_sft": 2.5}


class ManifestError(RuntimeError):
    """The manifest is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class ModelEntry:
    key: str
    source: str
    target: str
    filename: str
    size: int
    sha256: str
    url: str

    @property
    def relative_path(self) -> str:
        return f"{self.target}/{self.filename}"

    @property
    def requires_token(self) -> bool:
        return self.source == "civitai"


def parse_manifest(text: str, origin: str = "models.tsv") -> dict[str, ModelEntry]:
    entries: dict[str, ModelEntry] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        columns = line.split("\t")
        if len(columns) != 7:
            raise ManifestError(f"{origin}:{lineno}: expected 7 tab-separated columns, got {len(columns)}")
        key, source, target, filename, size, sha, url = columns
        if key in entries:
            raise ManifestError(f"{origin}:{lineno}: duplicate key {key!r}")
        try:
            size_bytes = int(size)
        except ValueError:
            raise ManifestError(f"{origin}:{lineno}: size is not an integer: {size!r}") from None
        if size_bytes <= 0:
            raise ManifestError(f"{origin}:{lineno}: size must be positive")
        if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha.lower()):
            raise ManifestError(f"{origin}:{lineno}: sha256 must be 64 hex characters")
        if source not in ("hf", "civitai"):
            raise ManifestError(f"{origin}:{lineno}: unsupported source {source!r}")
        entries[key] = ModelEntry(
            key=key,
            source=source,
            target=target,
            filename=filename,
            size=size_bytes,
            sha256=sha.lower(),
            url=url,
        )
    if not entries:
        raise ManifestError(f"{origin}: manifest is empty")
    return entries


def load_manifest(path: str | Path | None = None) -> dict[str, ModelEntry]:
    target = Path(path) if path is not None else MANIFEST_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read {target}: {exc}") from exc
    return parse_manifest(text, str(target))


def select(manifest: dict[str, ModelEntry], set_name: str) -> list[ModelEntry]:
    if set_name not in SETS:
        raise ManifestError(f"unknown model set {set_name!r}; use {', '.join(SETS)}")
    missing = [key for key in SETS[set_name] if key not in manifest]
    if missing:
        raise ManifestError(f"manifest is missing entries for set {set_name!r}: {', '.join(missing)}")
    return [manifest[key] for key in SETS[set_name]]


def entries_for_check(manifest: dict[str, ModelEntry], set_name: str | None) -> list[ModelEntry]:
    return list(manifest.values()) if set_name is None else select(manifest, set_name)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FileCheck:
    state: str  # ok | missing | size | sha
    detail: str

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def check_entry(
    entry: ModelEntry,
    path: Path,
    verify_sha: bool = False,
    size_of=None,
    sha_of=None,
) -> FileCheck:
    """Compare a file on disk with the manifest entry.

    ``size_of``/``sha_of`` are injectable so plans can be tested against a fake
    filesystem instead of real multi-gigabyte files.
    """
    size_of = size_of or (lambda target: target.stat().st_size)
    sha_of = sha_of or sha256_of
    try:
        size = size_of(path)
    except OSError:
        return FileCheck("missing", f"missing: {path}")
    if size is None:
        return FileCheck("missing", f"missing: {path}")
    if size != entry.size:
        return FileCheck("size", f"size {size} != expected {entry.size}: {path}")
    if verify_sha:
        try:
            actual = sha_of(path)
        except OSError as exc:
            return FileCheck("sha", f"cannot read {path}: {exc}")
        if actual != entry.sha256:
            return FileCheck("sha", f"sha256 {actual} != expected {entry.sha256}: {path}")
    return FileCheck("ok", f"ok: {path}")


MODEL_SUFFIXES = (".safetensors", ".ckpt", ".pt", ".pth", ".gguf")


def model_counts(probe, models_dir: Path) -> dict[str, int]:
    """Count model files per top-level folder (used by the launcher and doctor)."""
    counts: dict[str, int] = {}
    for name in ("diffusion_models", "checkpoints", "text_encoders", "vae", "loras"):
        folder = models_dir / name
        counts[name] = sum(
            1 for item in probe.listdir(folder) if item.endswith(MODEL_SUFFIXES) and probe.is_file(folder / item)
        )
    return counts
