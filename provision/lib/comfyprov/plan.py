"""Planners: pure functions from host facts to an executable plan.

Nothing here writes to disk, calls sudo or starts processes. That is the whole
point: the interesting decisions (is this step needed? does it require a
reboot? is a prerequisite missing?) are testable without touching the machine.

The shell executor in ``provision/lib/common.sh`` understands the action kinds
produced here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from . import layout, models
from .actions import (
    EFFECT_BOOT,
    EFFECT_NOW,
    EFFECT_SESSION,
    EXIT_REBOOT,
    EXIT_VERIFY,
    STATUS_PENDING,
    Action,
    Plan,
)
from .config import Config
from .facts import Facts, local_tags, rev_parse
from .probe import Probe

STEPS = ("system", "gpu", "comfyui", "manager", "launcher", "models", "verify")


@dataclass(frozen=True)
class Options:
    """Command-line knobs that influence planning."""

    force: bool = False
    ref: str | None = None
    models_set: str | None = None
    check_only: bool = False
    verify_sha: bool = False
    full: bool = False


def _gib_percent(part: int | None, whole: int | None) -> str:
    if not part or not whole:
        return ""
    return f"{part / whole * 100:.0f}%"


class Planner:
    def __init__(self, config: Config, facts: Facts, probe: Probe, options: Options | None = None) -> None:
        self.config = config
        self.facts = facts
        self.probe = probe
        self.options = options or Options()

    # -- primitive builders --------------------------------------------------
    def mount(self, plan: Plan) -> None:
        plan.add(
            Action(
                id="mount-big",
                kind="exec",
                summary=f"Mount {self.config.big_mount}",
                params={"argv": ["udisksctl", "mount", "-b", self.config.big_device], "allow_fail": True},
                satisfied=self.facts.big_mounted,
                effect=EFFECT_NOW,
            )
        )

    def mkdir(self, plan: Plan, path, mode: str | None = None) -> None:
        current = self.probe.file_mode(path) if mode else None
        satisfied = self.probe.is_dir(path) and (mode is None or current == mode)
        plan.add(
            Action(
                id=f"mkdir:{path}",
                kind="mkdir",
                summary=f"Create {path}",
                params={"path": str(path), "mode": mode or ""},
                satisfied=satisfied,
            )
        )

    def write(
        self,
        plan: Plan,
        path,
        content: str,
        mode: str = "644",
        sudo: bool = False,
        current: str | None = None,
    ) -> None:
        plan.add(
            Action(
                id=f"write:{path}",
                kind="write_file",
                summary=f"Write {path}",
                params={"path": str(path), "content": content, "mode": mode},
                satisfied=current == content,
                sudo=sudo,
            )
        )

    def exec(
        self,
        plan: Plan,
        id: str,
        summary: str,
        argv,
        sudo: bool = False,
        satisfied: bool = False,
        allow_fail: bool = False,
        effect: str = EFFECT_NOW,
        cwd: str | None = None,
    ) -> None:
        plan.add(
            Action(
                id=id,
                kind="exec",
                summary=summary,
                params={"argv": list(argv), "allow_fail": allow_fail, "cwd": cwd or ""},
                satisfied=satisfied,
                sudo=sudo,
                effect=effect,
            )
        )

    def pip(
        self,
        plan: Plan,
        id: str,
        summary: str,
        specs,
        index_url: str = "",
        args=(),
        fallback=(),
        constraints: str = "",
        requirements: str = "",
        stamp: str = "",
        stamp_content: str = "",
        check_after: bool = False,
        satisfied: bool = False,
    ) -> None:
        plan.add(
            Action(
                id=id,
                kind="pip_install",
                summary=summary,
                params={
                    "python": str(self.config.venv_python),
                    "args": list(args),
                    "specs": list(specs),
                    "fallback_specs": list(fallback),
                    "index_url": index_url,
                    "constraints": constraints,
                    "requirements": requirements,
                    "stamp_path": stamp,
                    "stamp_content": stamp_content,
                    "check_after": check_after,
                },
                satisfied=satisfied,
            )
        )

    # -- steps ---------------------------------------------------------------
    def system(self) -> Plan:
        plan = Plan("system")
        self.mount(plan)
        _big_fstab_actions(self, plan)

        chosen, skipped = _resolve_packages(self.facts, layout.APT_REQUIRED + layout.APT_OPTIONAL)
        missing = [name for name in chosen if name not in self.facts.packages]
        if skipped:
            plan.note(f"not available in the configured repositories: {', '.join(skipped)}")
        plan.add(
            Action(
                id="apt",
                kind="apt_install",
                summary=f"Install {len(missing)} system package(s)" if missing else "System packages",
                params={"packages": missing, "update": bool(missing)},
                satisfied=not missing,
                sudo=True,
            )
        )

        plan.add(
            Action(
                id="sysctl-file",
                kind="write_file",
                summary=f"Write {layout.SYSCTL_FILE}",
                params={"path": str(layout.SYSCTL_FILE), "content": layout.SYSCTL_CONTENT, "mode": "644"},
                satisfied=self.facts.file("sysctl") == layout.SYSCTL_CONTENT,
                sudo=True,
            )
        )
        low = {name: value for name, value in layout.SYSCTL_VALUES.items() if self.facts.sysctl.get(name, 0) < value}
        self.exec(
            plan,
            "sysctl-apply",
            "Apply kernel tuning",
            ["sysctl", "--system"],
            sudo=True,
            satisfied=not low and self.facts.file("sysctl") == layout.SYSCTL_CONTENT,
        )

        if self.config.enable_crash_capture:
            _crash_logging_actions(self, plan)
        else:
            plan.note("crash capture is disabled (ENABLE_CRASH_CAPTURE=0)")

        for path in _app_dirs(self.config):
            self.mkdir(plan, path)
        self.mkdir(plan, self.config.config_home, mode="700")
        for path in _models_dirs(self.config):
            self.mkdir(plan, path, mode="775")

        if self.config.enable_swap:
            _swap_actions(self, plan)
        else:
            plan.note("swap is disabled (ENABLE_SWAP=0); use './provision.sh swap' later if needed")

        if self.facts.install_free_gb is not None and self.facts.install_free_gb < 20:
            plan.note(f"low disk space on {self.config.install_dir}: {self.facts.install_free_gb} GB")
        return plan.finalize()

    def gpu(self) -> Plan:
        plan = Plan("gpu")
        if not self.facts.kfd_present:
            return plan.block(
                "/dev/kfd is missing: the amdgpu KFD driver is not available. "
                "Check 'lsmod | grep amdgpu' and the kernel boot log."
            )

        for name in layout.RENDER_GROUPS:
            group = self.facts.group(name)
            if group is not None and group.exists:
                continue
            self.exec(
                plan,
                f"groupadd:{name}",
                f"Create system group {name}",
                ["groupadd", "--system", name],
                sudo=True,
            )

        all_in_db = all(
            (group := self.facts.group(name)) is not None and group.in_db(self.facts.user)
            for name in layout.RENDER_GROUPS
        )
        self.exec(
            plan,
            "usermod-groups",
            f"Add {self.facts.user} to {', '.join(layout.RENDER_GROUPS)}",
            ["usermod", "-aG", ",".join(layout.RENDER_GROUPS), self.facts.user],
            sudo=True,
            satisfied=all_in_db,
            effect=EFFECT_SESSION,
        )

        self.write(
            plan,
            layout.UDEV_FILE,
            layout.UDEV_CONTENT,
            mode="644",
            sudo=True,
            current=self.facts.file("udev"),
        )

        nodes_ok = all(
            self.facts.device_perms.get(path) == (group, layout.DEVTMP_MODE) for path, group in _node_groups(self.facts)
        )
        self.exec(
            plan,
            "udev-reload",
            "Reload udev rules for the GPU nodes",
            ["udevadm", "control", "--reload-rules"],
            sudo=True,
            satisfied=nodes_ok and self.facts.file("udev") == layout.UDEV_CONTENT,
        )
        self.exec(
            plan,
            "udev-trigger",
            "Trigger udev for drm and kfd",
            ["udevadm", "trigger", "--subsystem-match=drm"],
            sudo=True,
            satisfied=nodes_ok and self.facts.file("udev") == layout.UDEV_CONTENT,
        )
        for path, group in _node_groups(self.facts):
            current = self.facts.device_perms.get(path)
            self.exec(
                plan,
                f"chgrp:{path}",
                f"chgrp {group} {path}",
                ["chgrp", group, path],
                sudo=True,
                satisfied=current is not None and current[0] == group,
            )
            self.exec(
                plan,
                f"chmod:{path}",
                f"chmod {layout.DEVTMP_MODE} {path}",
                ["chmod", layout.DEVTMP_MODE, path],
                sudo=True,
                satisfied=current is not None and current[1] == layout.DEVTMP_MODE,
            )

        plan.finalize()
        if not self.facts.kfd_accessible:
            plan.need_reboot(
                "GPU permissions are configured, but this login session predates the group change. "
                "Reboot (recommended) or log out and back in, then run './provision.sh all' again."
            )
        return plan

    def comfyui(self) -> Plan:
        plan = Plan("comfyui")
        if not self.facts.python312:
            return plan.block("Python 3.12 is required. Run './provision.sh system' first.")

        self.mount(plan)
        ref = self.options.ref or self.config.comfyui_ref
        comfy = self.facts.checkout("comfyui")
        if comfy.exists and not comfy.is_git:
            return plan.block(f"{comfy.path} exists but is not a git checkout. Move it aside first.")
        if comfy.is_git and comfy.origin and not any(item in comfy.origin for item in layout.COMFYUI_ORIGINS):
            return plan.block(f"unexpected ComfyUI git origin: {comfy.origin}")
        if comfy.is_git and not comfy.clean:
            return plan.block("ComfyUI has local changes. Commit or stash them, then re-run.")

        if ref == "latest" and comfy.is_git:
            tags = local_tags(self.probe, comfy.path)
            ref = tags[0] if tags else ref
        plan.add(
            Action(
                id="comfyui-checkout",
                kind="git_checkout",
                summary=f"ComfyUI checkout at {ref}",
                params={
                    "dir": str(self.config.comfyui_dir),
                    "url": layout.COMFYUI_REPO,
                    "ref": ref,
                    "allow_missing": False,
                },
                satisfied=_checkout_satisfied(self.probe, comfy, ref),
            )
        )

        if self.facts.venv_python and self.facts.venv_version not in (None, "3.12"):
            return plan.block(
                f"{self.config.venv_dir} uses Python {self.facts.venv_version}. Remove it and re-run the comfyui step."
            )
        self.exec(
            plan,
            "venv",
            "Create the Python 3.12 virtualenv",
            [self.facts.python312, "-m", "venv", str(self.config.venv_dir)],
            satisfied=bool(self.facts.venv_python) and self.facts.venv_version == "3.12",
        )

        pip_stamp = self.config.state_dir / layout.STAMPS["pip"]
        self.pip(
            plan,
            "pip-bootstrap",
            "Upgrade pip, setuptools and wheel",
            [],
            args=["--upgrade", "pip", "setuptools", "wheel"],
            stamp=str(pip_stamp),
            stamp_content="pip\n",
            satisfied=self.facts.stamp("pip") == "pip\n",
        )

        expected = {
            "torch": self.config.torch_tag,
            "torchvision": self.config.torchvision_tag,
            "torchaudio": self.config.torchaudio_tag,
        }
        gfx = self.facts.gfx or self.config.gpu_gfx
        torch_satisfied = all(self.facts.torch_versions.get(name) == version for name, version in expected.items())
        self.pip(
            plan,
            "torch",
            f"Install PyTorch {self.config.torch_tag} with {gfx} kernels",
            [
                f"torch[device-{gfx}]=={self.config.torch_tag}",
                f"torchvision[device-{gfx}]=={self.config.torchvision_tag}",
                f"torchaudio=={self.config.torchaudio_tag}",
            ],
            index_url=self.config.torch_index,
            fallback=[
                f"torch=={self.config.torch_tag}",
                f"amd-torch-device-{gfx}=={self.config.torch_tag}",
                f"torchvision=={self.config.torchvision_tag}",
                f"amd-torchvision-device-{gfx}=={self.config.torchvision_tag}",
                f"torchaudio=={self.config.torchaudio_tag}",
            ],
            satisfied=torch_satisfied,
        )

        constraints = self.config.state_dir / "constraints.txt"
        constraints_content = layout.constraints_text(self.config)
        self.write(plan, constraints, constraints_content, current=self.facts.file("constraints"))

        requirements = self.config.comfyui_dir / "requirements.txt"
        req_bytes = self.probe.read_bytes(requirements)
        if req_bytes is None:
            return plan.block(
                f"ComfyUI requirements are missing: {requirements}. "
                "The checkout is incomplete; re-run this step after the clone."
            )
        import hashlib

        stamp_content = (
            f"{hashlib.sha256(req_bytes).hexdigest()} "
            f"{hashlib.sha256(constraints_content.encode()).hexdigest()} "
            f"{self.config.torch_tag}\n"
        )
        req_stamp = self.config.state_dir / layout.STAMPS["requirements"]
        requirements_satisfied = self.facts.stamp("requirements") == stamp_content
        self.pip(
            plan,
            "requirements",
            "Install ComfyUI requirements",
            [],
            requirements=str(requirements),
            constraints=str(constraints),
            stamp=str(req_stamp),
            stamp_content=stamp_content,
            check_after=True,
            satisfied=requirements_satisfied,
        )

        models_link = self.config.comfyui_dir / "models"
        link_ok = self.probe.is_symlink(models_link) and self.probe.readlink(models_link) in (
            str(self.config.models_dir),
            str(self.config.models_dir) + "/",
        )
        if self.probe.exists(models_link) and not self.probe.is_symlink(models_link):
            if self.probe.is_dir(models_link) and not self.probe.listdir(models_link):
                plan.add(
                    Action(
                        id="rmdir-models",
                        kind="exec",
                        summary=f"Remove the empty {models_link}",
                        params={"argv": ["rmdir", str(models_link)], "allow_fail": False, "cwd": ""},
                    )
                )
            else:
                plan.note(f"{models_link} is a real non-empty directory; leaving it untouched")
                link_ok = True
        if not link_ok:
            plan.add(
                Action(
                    id="models-symlink",
                    kind="symlink",
                    summary=f"Link {models_link} to {self.config.models_dir}",
                    params={"link": str(models_link), "target": str(self.config.models_dir)},
                )
            )
        return plan.finalize()

    def manager(self) -> Plan:
        plan = Plan("manager")
        if not self.config.install_manager:
            plan.note("INSTALL_MANAGER=0; skipping ComfyUI-Manager")
            return plan
        comfy = self.facts.checkout("comfyui")
        if not comfy.installed:
            return plan.block("ComfyUI is not installed. Run './provision.sh comfyui' first.")
        if not self.facts.venv_python:
            return plan.block("The virtualenv is missing. Run './provision.sh comfyui' first.")

        manager = self.facts.checkout("manager")
        manager_dir = self.config.comfyui_dir / "custom_nodes" / "ComfyUI-Manager"
        if manager.exists and not manager.is_git:
            return plan.block(f"{manager_dir} exists but is not a git checkout. Move it aside first.")
        if manager.is_git and not manager.clean:
            return plan.block("ComfyUI-Manager has local changes. Commit or stash them first.")

        ref = self.config.manager_ref
        if ref == "latest" and manager.is_git:
            tags = local_tags(self.probe, manager.path)
            ref = tags[0] if tags else ref
        plan.add(
            Action(
                id="manager-checkout",
                kind="git_checkout",
                summary=f"ComfyUI-Manager checkout at {ref}",
                params={
                    "dir": str(manager_dir),
                    "url": layout.MANAGER_REPO,
                    "ref": ref,
                    "allow_missing": True,
                },
                satisfied=_checkout_satisfied(self.probe, manager, ref),
            )
        )

        requirements = manager_dir / "requirements.txt"
        req_bytes = self.probe.read_bytes(requirements)
        constraints = self.config.state_dir / "constraints.txt"
        stamp = self.config.state_dir / layout.STAMPS["manager"]
        if req_bytes is None:
            plan.note("Manager requirements are not fetched yet; the install action clones first")
            stamp_content = "manager\n"
            satisfied = False
        else:
            import hashlib

            stamp_content = f"{hashlib.sha256(req_bytes).hexdigest()} {self.config.torch_tag}\n"
            satisfied = self.facts.stamp("manager") == stamp_content
        self.pip(
            plan,
            "manager-requirements",
            "Install ComfyUI-Manager requirements",
            [],
            requirements=str(requirements),
            constraints=str(constraints),
            stamp=str(stamp),
            stamp_content=stamp_content,
            check_after=True,
            satisfied=satisfied,
        )

        plan.note("ComfyUI-Manager can install and run third-party code; only install node packs you trust")
        return plan.finalize()

    def launcher(self) -> Plan:
        plan = Plan("launcher")
        if not self.probe.is_file(self.config.launcher_path):
            return plan.block(f"Launcher not found: {self.config.launcher_path}")

        for path in (self.config.launcher_path, self.config.install_dir / "provision.sh"):
            mode = self.probe.file_mode(path)
            self.exec(
                plan,
                f"chmod:{path}",
                f"Make {path.name} executable",
                ["chmod", "755", str(path)],
                satisfied=mode == "755",
            )

        self.write(plan, self.config.icon_path, layout.ICON_SVG, current=self.facts.file("icon"))
        entry = layout.desktop_entry(self.config)
        self.write(
            plan,
            self.config.desktop_entry_path,
            entry,
            mode="755",
            current=self.facts.file("desktop_entry"),
        )
        desktop_changed = self.facts.file("desktop_entry") != entry

        shortcut = self.facts.desktop_shortcut
        if self.config.create_desktop_icon and self.probe.is_dir(shortcut.parent):
            self.write(plan, shortcut, entry, mode="755", current=self.facts.file("desktop_shortcut"))
            desktop_changed = desktop_changed or self.facts.file("desktop_shortcut") != entry
        elif self.config.create_desktop_icon:
            plan.note(f"no desktop directory at {shortcut.parent}; skipping the desktop shortcut")

        self.exec(
            plan,
            "update-desktop-database",
            "Refresh the desktop database",
            ["update-desktop-database", str(self.config.applications_dir)],
            allow_fail=True,
            satisfied=not desktop_changed,
        )
        self.exec(
            plan,
            "desktop-validate",
            "Validate the desktop entry",
            ["desktop-file-validate", str(self.config.desktop_entry_path)],
            allow_fail=True,
            satisfied=not desktop_changed,
        )
        return plan.finalize()

    def swap(self) -> Plan:
        plan = Plan("swap")
        _swap_actions(self, plan)
        return plan.finalize()

    def models(self) -> Plan:
        plan = Plan("models")
        try:
            manifest = models.load_manifest()
        except models.ManifestError as exc:
            return plan.block(str(exc))
        self.mount(plan)

        if self.options.models_set is None and not self.options.check_only:
            plan.note("no --set given, nothing will be downloaded")
            for name, keys in models.SETS.items():
                plan.note(f"--set {name}: {', '.join(keys)}")
            return plan.finalize()

        entries = models.entries_for_check(manifest, self.options.models_set)
        needs_token = any(entry.requires_token for entry in entries)
        if needs_token and not self.options.check_only and not self.facts.file("civitai_token"):
            return plan.block(
                "A Civitai token is required for Jib Mix. Run './provision.sh auth' first, "
                "or use '--set sft' for the public MOSS-Image model."
            )

        problems = 0
        for entry in entries:
            dest = self.config.models_dir / entry.relative_path
            state = models.check_entry(
                entry,
                dest,
                verify_sha=self.options.verify_sha,
                size_of=self.probe.file_size,
            )
            if state.ok:
                plan.add(
                    Action(
                        id=f"model:{entry.key}",
                        kind="download",
                        summary=f"{entry.key} already present",
                        params=_download_params(self.config, entry),
                        satisfied=True,
                    )
                )
                continue
            problems += 1
            plan.note(state.detail)
            if self.options.check_only:
                continue
            plan.add(
                Action(
                    id=f"model:{entry.key}",
                    kind="download",
                    summary=f"Download {entry.key} ({entry.filename})",
                    params=_download_params(self.config, entry),
                )
            )

        if self.options.check_only:
            if problems:
                plan.status = STATUS_PENDING
                plan.exit_code = EXIT_VERIFY
            else:
                plan.note("all requested model files are present and verified")
            return plan.finalize()

        plan.finalize()
        if problems:
            plan.note(f"{problems} file(s) will be downloaded")
        return plan

    def verify(self) -> Plan:
        plan = Plan("verify")
        if not self.facts.checkout("comfyui").installed:
            return plan.block("ComfyUI is not installed. Run './provision.sh all' first.")
        if not self.facts.venv_python:
            return plan.block("The virtualenv is missing. Run './provision.sh comfyui' first.")
        self.mount(plan)
        plan.finalize()
        if not self.facts.kfd_accessible:
            return plan.need_reboot(
                "ComfyUI is installed, but the GPU is not reachable from this session. "
                "Reboot (recommended) or log out and back in, then run './provision.sh verify'."
            )
        plan.status = STATUS_PENDING
        return plan

    # -- dispatch ------------------------------------------------------------
    def build(self, step: str) -> Plan:
        builders = {
            "system": self.system,
            "gpu": self.gpu,
            "comfyui": self.comfyui,
            "manager": self.manager,
            "launcher": self.launcher,
            "swap": self.swap,
            "models": self.models,
            "verify": self.verify,
        }
        if step not in builders:
            raise ValueError(f"unknown step: {step}")
        return builders[step]()


# -- helpers -----------------------------------------------------------------
def _app_dirs(config: Config) -> list:
    return [config.install_dir / name for name in layout.APP_SUBDIRS]


def _models_dirs(config: Config) -> list:
    return [config.models_dir / name for name in layout.MODELS_SUBDIRS]


def _resolve_packages(facts: Facts, wanted) -> tuple[list[str], list[str]]:
    available = facts.apt_available
    chosen: list[str] = []
    skipped: list[str] = []
    for name in wanted:
        candidates = (name, *layout.APT_RENAMES.get(name, ()))
        picked = None
        for candidate in candidates:
            if not available or candidate in available:
                picked = candidate
                break
        if picked is None:
            skipped.append(name)
        else:
            chosen.append(picked)
    return chosen, skipped


def _node_groups(facts: Facts) -> list[tuple[str, str]]:
    nodes = [(path, "render") for path in facts.render_nodes]
    nodes.extend((path, "video") for path in facts.card_nodes)
    nodes.append(("/dev/kfd", "render"))
    return nodes


def _checkout_satisfied(probe: Probe, checkout, ref: str) -> bool:
    if not checkout.installed or not ref:
        return False
    commit = rev_parse(probe, checkout.path, ref)
    return bool(commit) and checkout.head == commit and checkout.clean


def _download_params(config: Config, entry: models.ModelEntry) -> dict:
    return {
        "url": entry.url,
        "dest": str(config.models_dir / entry.relative_path),
        "size": entry.size,
        "sha256": entry.sha256,
        "label": entry.key,
        "source": entry.source,
        "token_file": str(config.civitai_token_file) if entry.requires_token else "",
    }


def _big_fstab_actions(planner: Planner, plan: Plan) -> None:
    config = planner.config
    facts = planner.facts
    mount = str(config.big_mount)
    fstab_line = f"UUID={config.big_uuid} {mount} ext4 defaults,nofail 0 2"
    fstab_ok = any(line.split("#", 1)[0].strip() == fstab_line for line in facts.fstab.splitlines())
    plan.add(
        Action(
            id="big-fstab",
            kind="append_line",
            summary=f"Persist {mount} in /etc/fstab",
            params={"path": "/etc/fstab", "line": fstab_line},
            satisfied=fstab_ok,
            sudo=True,
            effect=EFFECT_BOOT,
        )
    )


def _swap_actions(planner: Planner, plan: Plan) -> None:
    config = planner.config
    facts = planner.facts
    path = str(config.swap_file)
    try:
        size_bytes = layout.parse_size(config.swap_size)
    except ValueError as exc:
        plan.note(f"invalid SWAP_SIZE: {exc}")
        return

    exists = planner.probe.file_size(path) is not None
    size_ok = exists and planner.probe.file_size(path) == size_bytes
    active = path in facts.swap_active
    fstab_line = f"{path} none swap sw 0 0"
    fstab_ok = any(line.split("#", 1)[0].strip() == fstab_line for line in facts.fstab.splitlines())

    planner.exec(
        plan,
        "swap-allocate",
        f"Allocate {config.swap_size} swap file at {path}",
        ["fallocate", "-l", config.swap_size, path],
        sudo=True,
        satisfied=size_ok,
    )
    planner.exec(
        plan,
        "swap-chmod",
        f"chmod 600 {path}",
        ["chmod", "600", path],
        sudo=True,
        satisfied=exists and planner.probe.file_mode(path) == "600",
    )
    # An active swapfile is already formatted: blkid refuses to probe it, and
    # "mkswap /swapfile" then fails with "is mounted; will not make swapspace".
    formatted = active or (
        size_ok and planner.probe.run(["blkid", "-o", "value", "-s", "TYPE", path]).out.strip() == "swap"
    )
    planner.exec(plan, "swap-mkswap", f"mkswap {path}", ["mkswap", path], sudo=True, satisfied=formatted)
    planner.exec(
        plan,
        "swap-on",
        f"Enable swap at {path}",
        ["swapon", path],
        sudo=True,
        effect=EFFECT_BOOT,
        satisfied=active,
    )
    plan.add(
        Action(
            id="swap-fstab",
            kind="append_line",
            summary=f"Persist {path} in /etc/fstab",
            params={"path": "/etc/fstab", "line": fstab_line},
            satisfied=fstab_ok,
            sudo=True,
            effect=EFFECT_BOOT,
        )
    )


def _crash_logging_actions(planner: Planner, plan: Plan) -> None:
    """Durable logging: journald sync, fsync'd kernel copy, watchdog panics.

    Part of the ``system`` step because it changes the machine's configuration,
    which is exactly what provisioning is for.
    """
    facts = planner.facts
    # Ubuntu ships no /etc/systemd/journald.conf.d, and writing a file must not
    # silently fail because the directory is missing.
    planner.exec(
        plan,
        "journald-dir",
        f"Create {layout.JOURNALD_DROPIN.parent}",
        ["install", "-d", "-m", "755", str(layout.JOURNALD_DROPIN.parent)],
        sudo=True,
        satisfied=facts.dir_exists(layout.JOURNALD_DROPIN.parent),
    )
    planner.write(
        plan,
        layout.JOURNALD_DROPIN,
        layout.JOURNALD_CONTENT,
        mode="644",
        sudo=True,
        current=facts.file("journald"),
    )

    journald_ok = facts.file("journald") == layout.JOURNALD_CONTENT
    planner.exec(
        plan,
        "journald-restart",
        "Restart systemd-journald with the durable sync settings",
        ["systemctl", "restart", "systemd-journald"],
        sudo=True,
        satisfied=journald_ok,
    )

    if "rsyslog" in facts.packages:
        planner.write(
            plan,
            layout.RSYSLOG_DROPIN,
            layout.RSYSLOG_CONTENT,
            mode="644",
            sudo=True,
            current=facts.file("rsyslog"),
        )
        planner.exec(
            plan,
            "rsyslog-reload",
            "Reload rsyslog (fsync'd kernel log copy)",
            ["systemctl", "reload-or-restart", "rsyslog"],
            sudo=True,
            allow_fail=True,
            satisfied=facts.file("rsyslog") == layout.RSYSLOG_CONTENT,
        )
    else:
        plan.note("rsyslog is not installed; kernel messages only go through journald")

    planner.write(
        plan,
        layout.CRASH_SYSCTL_FILE,
        layout.CRASH_SYSCTL_CONTENT,
        mode="644",
        sudo=True,
        current=facts.file("crash_sysctl"),
    )
    wrong = {name: value for name, value in layout.CRASH_SYSCTL_VALUES.items() if facts.sysctl.get(name) != value}
    planner.exec(
        plan,
        "crash-sysctl",
        "Apply watchdog and panic settings",
        ["sysctl", "--system"],
        sudo=True,
        satisfied=not wrong and facts.file("crash_sysctl") == layout.CRASH_SYSCTL_CONTENT,
    )
    # A freeze must reset the box on its own. The AMD chipset TCO timer is
    # exposed by sp5100_tco; systemd keeps it fed and reboots if PID 1 stops.
    module, reason = layout.watchdog_module(facts.isa_bridge, facts.acpi_wdat)
    content = layout.modules_load_content(module)
    planner.write(
        plan,
        layout.MODULES_LOAD_FILE,
        content,
        mode="644",
        sudo=True,
        current=facts.file("modules_load"),
    )
    planner.exec(
        plan,
        "watchdog-load",
        f"Load the hardware watchdog ({module}: {reason})",
        ["modprobe", module],
        sudo=True,
        allow_fail=True,
        satisfied=facts.watchdog_present,
    )
    planner.write(
        plan,
        layout.WATCHDOG_DROPIN,
        layout.WATCHDOG_CONTENT,
        mode="644",
        sudo=True,
        current=facts.file("watchdog"),
    )
    planner.exec(
        plan,
        "watchdog-reexec",
        "Reload systemd with the watchdog settings",
        ["systemctl", "daemon-reexec"],
        sudo=True,
        allow_fail=True,
        satisfied=facts.file("watchdog") == layout.WATCHDOG_CONTENT and facts.watchdog_present,
    )
    planner.exec(
        plan,
        "pstore-collect",
        "Collect firmware crash records after a reset",
        ["systemctl", "enable", "--now", layout.PSTORE_UNIT],
        sudo=True,
        allow_fail=True,
        satisfied=facts.pstore_enabled,
    )

    # --- force-load watchdog despite modprobe.d denylist ---
    # modules-load.d refuses deny-listed modules; modprobe itself ignores the
    # blacklist. A oneshot unit runs modprobe directly before any GPU workload.
    fw_mod, _fw_reason = layout.watchdog_module(facts.isa_bridge, facts.acpi_wdat)
    fw_override_dir = layout.FORCE_WATCHDOG_DIR
    planner.mkdir(
        plan,
        fw_override_dir,
        mode="755",
    )
    planner.write(
        plan,
        layout.FORCE_WATCHDOG_SERVICE,
        layout.FORCE_WATCHDOG_SERVICE_CONTENT.format(module=fw_mod),
        mode="644",
        sudo=True,
        current=facts.file("force_watchdog_service"),
    )
    planner.write(
        plan,
        fw_override_dir / "override.conf",
        layout.FORCE_WATCHDOG_OVERRIDE_CONTENT.format(module=fw_mod),
        mode="644",
        sudo=True,
        current=facts.file("force_watchdog_override"),
    )
    planner.exec(
        plan,
        "force-watchdog-enable",
        f"Enable oneshot force-load of {fw_mod}",
        ["systemctl", "enable", "--now", "comfyui-force-watchdog.service"],
        sudo=True,
        allow_fail=True,
        satisfied=facts.watchdog_present,
    )

    # --- netconsole: out-of-band kernel log capture ---
    # Sends printk over UDP to a second machine. The only way to read the
    # last lines of a true platform freeze.
    planner.write(
        plan,
        layout.NETCONSOLE_MODULE_CONF,
        layout.NETCONSOLE_MODULE_CONTENT,
        mode="644",
        sudo=True,
        current=facts.file("netconsole_module"),
    )
    planner.write(
        plan,
        layout.NETCONSOLE_CONF,
        "# ComfyUI / netconsole parameters\n# options netconsole netconsole=@<src>/<eth>,@<dst>/<port>\n",
        mode="644",
        sudo=True,
        current=facts.file("netconsole_conf"),
    )
    planner.exec(
        plan,
        "netconsole-load",
        "Load netconsole module",
        ["modprobe", "netconsole"],
        sudo=True,
        allow_fail=True,
        satisfied=bool(facts.file("netconsole_module")),
    )

    # --- ramoops: persistent RAM for pstore ---
    # Reserves a small region of RAM that survives a reset so the last dmesg
    # lines can be read after reboot, even if journald never syncs.
    planner.write(
        plan,
        layout.RAMOOPS_MODULE_CONF,
        layout.RAMOOPS_MODULE_CONTENT,
        mode="644",
        sudo=True,
        current=facts.file("ramoops_module"),
    )
    planner.write(
        plan,
        layout.RAMOOPS_CONF,
        layout.RAMOOPS_PARAMS_CONTENT,
        mode="644",
        sudo=True,
        current=facts.file("ramoops_conf"),
    )
    planner.exec(
        plan,
        "ramoops-load",
        "Load ramoops module",
        ["modprobe", "ramoops"],
        sudo=True,
        allow_fail=True,
        satisfied=bool(facts.file("ramoops_module")),
    )

    # --- crash doctor: automated root-cause determination ---
    planner.mkdir(
        plan,
        layout.CRASH_DOCTOR_DIR,
        mode="755",
    )
    planner.write(
        plan,
        layout.CRASH_DOCTOR,
        layout.CRASH_DOCTOR_CONTENT,
        mode="755",
        sudo=True,
        current=facts.file("crash_doctor"),
    )

    # --- log sync script: fsync logs every LOG_SYNC_INTERVAL seconds ---
    planner.write(
        plan,
        layout.LOG_SYNC_SCRIPT,
        layout.LOG_SYNC_CONTENT,
        mode="755",
        sudo=True,
        current=facts.file("log_sync"),
    )


def status(config: Config, facts: Facts, probe: Probe) -> list[Plan]:
    """Read-only summary used by ``provision.sh status``."""
    base = Planner(config, facts, probe, Options(check_only=True))
    plans = [base.build(step) for step in STEPS]
    return [replace(plan, status=plan.status) for plan in plans]


def reboot_needed(plans) -> bool:
    return any(plan.exit_code == EXIT_REBOOT for plan in plans)
