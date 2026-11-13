"""Preflight checks and diagnostics.

Both commands are read-only. They share the same ``Facts`` snapshot as the
planners, so "what preflight reports" and "what the plan decides" cannot drift
apart.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable

from . import crashlog, layout, models
from .actions import EXIT_OK, EXIT_PREREQ
from .config import Config
from .facts import Facts
from .probe import Probe
from .report import FAIL, OK, WARN, Finding, has_failures
from .server import HttpClient, HttpError, parse_stats

NETWORK_HOSTS = ("https://github.com", "https://huggingface.co", "https://civitai.com")
DEBIAN_IDS = {"ubuntu", "linuxmint", "debian", "pop"}


def http_reachable(url: str, timeout: float = 12.0) -> bool:
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def preflight(
    config: Config,
    facts: Facts,
    probe: Probe,
    network: Callable[[str], bool] | None = None,
    check_network: bool = True,
) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    probe = probe or Probe()
    check = network or http_reachable

    families = f"{facts.os_id} {facts.os_like}".split()
    if facts.os_id in DEBIAN_IDS or any(item in DEBIAN_IDS for item in families):
        findings.append(Finding(OK, "Distribution", facts.os_label))
    elif facts.os_id:
        findings.append(Finding(WARN, "Distribution", f"{facts.os_label}; written for Ubuntu 24.04 / Mint 22.x"))
    else:
        findings.append(Finding(WARN, "Distribution", "/etc/os-release not found"))

    if facts.arch == "x86_64":
        findings.append(Finding(OK, "Architecture", facts.arch))
    else:
        findings.append(Finding(FAIL, "Architecture", f"{facts.arch}; only x86_64 is supported"))

    if facts.python312:
        findings.append(Finding(OK, "Python 3.12", facts.python312))
    else:
        findings.append(Finding(WARN, "Python 3.12", "not installed yet; the system step installs it"))

    if not facts.kfd_present:
        findings.append(Finding(FAIL, "/dev/kfd", "missing; the amdgpu KFD driver is not available"))
    elif facts.kfd_accessible:
        findings.append(Finding(OK, "/dev/kfd", "read/write access"))
    else:
        findings.append(
            Finding(WARN, "/dev/kfd", "no access in this session; the gpu step fixes it (re-login may follow)")
        )

    if facts.render_nodes:
        findings.append(Finding(OK, "DRM render node", ", ".join(facts.render_nodes)))
    else:
        findings.append(Finding(WARN, "DRM render node", "no /dev/dri/renderD* found"))

    if facts.gfx:
        detail = facts.gfx if facts.gfx_source == "kfd" else f"{facts.gfx} (configured)"
        findings.append(Finding(OK, "gfx target", detail))
    else:
        findings.append(Finding(WARN, "gfx target", "cannot read /sys/class/kfd topologies"))

    for name in layout.RENDER_GROUPS:
        group = facts.group(name)
        if group is None or not group.exists:
            findings.append(Finding(WARN, f"group {name}", "missing; the gpu step creates it"))
        elif group.in_session:
            findings.append(Finding(OK, f"group {name}", "member (session)"))
        elif group.in_db(facts.user):
            findings.append(Finding(WARN, f"group {name}", "in the database but not in this session (reboot needed)"))
        else:
            findings.append(Finding(WARN, f"group {name}", "user is not a member yet"))

    if facts.big_mounted:
        findings.append(Finding(OK, "BIG mount", str(config.big_mount)))
    else:
        findings.append(Finding(WARN, "BIG mount", f"{config.big_mount} is not mounted (the plan mounts it)"))

    if facts.install_free_gb is None:
        findings.append(Finding(WARN, "free space", f"cannot determine for {config.install_dir}"))
    elif facts.install_free_gb < 20:
        findings.append(Finding(FAIL, "free space", f"system disk has {facts.install_free_gb} GB, 20 GB required"))
    else:
        findings.append(Finding(OK, "free space", f"system disk {facts.install_free_gb} GB"))

    if facts.big_mounted and facts.models_free_gb is not None and facts.models_free_gb < 30:
        findings.append(Finding(WARN, "models space", f"{facts.models_free_gb} GB free, 30 GB recommended"))

    missing_tools = [cmd for cmd in layout.COMMANDS_REQUIRED if cmd not in facts.tools]
    if missing_tools:
        findings.append(Finding(WARN, "commands", f"missing: {', '.join(missing_tools)}"))
    else:
        findings.append(Finding(OK, "commands", "git, curl, jq, flock, sha256sum, udisksctl"))

    if check_network:
        reachable = 0
        for url in NETWORK_HOSTS:
            if check(url):
                reachable += 1
            else:
                findings.append(Finding(WARN, "network", url))
        if reachable == 0:
            findings.append(Finding(FAIL, "network", "none of the required hosts is reachable"))
        elif reachable == len(NETWORK_HOSTS):
            findings.append(Finding(OK, "network", "github, huggingface, civitai"))

    return findings, EXIT_PREREQ if has_failures(findings) else EXIT_OK


def doctor(config: Config, facts: Facts, probe: Probe, client: HttpClient | None = None) -> list[Finding]:
    findings: list[Finding] = [
        Finding(OK, "user", f"{facts.user} (uid {facts.uid})"),
        Finding(OK, "system", f"{facts.os_label}, kernel {facts.kernel}, {facts.arch}"),
        Finding(OK, "groups", " ".join(sorted(probe.session_groups)) or "none"),
    ]

    for path, perms in facts.device_perms.items():
        if perms is None:
            findings.append(Finding(WARN, path, "missing"))
        else:
            group, mode = perms
            findings.append(Finding(OK, path, f"{group} {mode}"))
    for name in layout.RENDER_GROUPS:
        group = facts.group(name)
        if group is None:
            continue
        if group.pending(facts.user):
            findings.append(Finding(WARN, f"group {name}", "pending a new session (reboot or re-login)"))
    findings.append(
        Finding(OK if facts.kfd_accessible else WARN, "kfd access", "read/write" if facts.kfd_accessible else "denied")
    )
    findings.append(Finding(OK if facts.gfx else WARN, "gfx target", facts.gfx or "unknown"))

    if facts.big_mounted:
        findings.append(Finding(OK, "BIG mount", f"{config.big_mount}, {facts.models_free_gb} GB free"))
    else:
        findings.append(Finding(WARN, "BIG mount", f"{config.big_mount} is not mounted"))
    findings.append(Finding(OK, "app disk", f"{facts.install_free_gb} GB free"))

    comfy = facts.checkout("comfyui")
    if comfy.installed:
        findings.append(
            Finding(OK, "ComfyUI", f"{comfy.head[:12] or 'unknown'} ({'clean' if comfy.clean else 'dirty'})")
        )
    else:
        findings.append(Finding(WARN, "ComfyUI", "not installed"))

    manager = facts.checkout("manager")
    findings.append(Finding(OK if manager.installed else WARN, "ComfyUI-Manager", manager.head[:12] or "not installed"))

    if facts.venv_python:
        findings.append(Finding(OK, "venv python", f"{facts.venv_version or 'unknown'}"))
        torch = facts.torch_versions
        if torch:
            findings.append(
                Finding(OK, "torch", f"{torch.get('torch', '?')} (torchvision {torch.get('torchvision', '?')})")
            )
        else:
            findings.append(Finding(WARN, "torch", "not installed in the venv"))
    else:
        findings.append(Finding(WARN, "venv python", "not created"))

    counts = models.model_counts(probe, config.models_dir)
    findings.append(
        Finding(
            OK if counts["diffusion_models"] + counts["checkpoints"] else WARN,
            "models",
            " ".join(f"{name}={count}" for name, count in counts.items()),
        )
    )

    client = client or HttpClient(timeout=2)
    url = f"http://127.0.0.1:{config.port}"
    try:
        stats = parse_stats(client.get_json(f"{url}/system_stats", timeout=2))
        findings.append(Finding(OK, "server", f"running on port {config.port} (ComfyUI {stats['comfyui_version']})"))
    except (HttpError, ValueError):
        findings.append(Finding(WARN, "server", "not running"))

    findings.extend(crashlog.findings(config, probe))
    return findings
