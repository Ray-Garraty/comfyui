"""Planner tests: idempotency, reboot classification and prerequisite blocking."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import (
    FakeProbe,
    FakeRunner,
    base_dirs,
    base_files,
    base_listdir,
    base_perms,
    clean_host,
    configured_host,
    make_config,
)

from comfyprov import layout, models
from comfyprov.facts import collect
from comfyprov.plan import (
    EFFECT_BOOT,
    Options,
    Planner,
    reboot_needed,
    status,
)
from comfyprov.probe import RunResult


def plan(config, probe, step, **options):
    facts = collect(config, probe)
    return Planner(config, facts, probe, Options(**options)).build(step)


def host_after_system(config, plan_obj):
    """Replay a system plan onto a fake host to test idempotency."""
    files = base_files(config)
    dirs = set(base_dirs(config))
    modes = {}
    sizes = {}
    swap = set()
    packages = set()
    sysctl = {"vm.max_map_count": 65530, "fs.inotify.max_user_watches": 8192}
    mounted = set()
    for action in plan_obj.actions:
        if action.kind == "mkdir":
            dirs.add(action.params["path"])
            if action.params["mode"]:
                modes[action.params["path"]] = action.params["mode"]
        elif action.kind == "write_file":
            files[action.params["path"]] = action.params["content"]
        elif action.kind == "append_line":
            files[action.params["path"]] = (
                files.get(action.params["path"], "")
                + action.params["line"]
                + ("\n" if not action.params["line"].endswith("\n") else "")
            )
        elif action.kind == "apt_install":
            packages.update(action.params["packages"])
        elif action.id == "sysctl-apply":
            sysctl.update(layout.SYSCTL_VALUES)
        elif action.id == "crash-sysctl":
            sysctl.update(layout.CRASH_SYSCTL_VALUES)
        elif action.id == "mount-big":
            mounted.add(str(config.big_mount))
        elif action.id == "swap-allocate":
            sizes[str(config.swap_file)] = layout.parse_size(config.swap_size)
        elif action.id == "swap-chmod":
            modes[str(config.swap_file)] = "600"
        elif action.id == "swap-on":
            swap.add(str(config.swap_file))
    return FakeProbe(
        files=files,
        dirs=dirs,
        modes=modes,
        sizes=sizes,
        swap=swap,
        perms=base_perms(),
        listdir_map=base_listdir(config),
        packages=packages,
        apt=frozenset(layout.APT_REQUIRED + layout.APT_OPTIONAL),
        sysctl=sysctl,
        mounted=mounted,
        session_groups=("vlabe",),
        runner=FakeRunner(
            [("blkid", RunResult(0, "swap\n", "")), ("systemctl is-enabled", RunResult(0, "static\n", ""))]
        ),
    )


class PlanTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config = make_config(self.tmp)


class SystemPlanTests(PlanTestCase):
    def test_clean_host_needs_packages_directories_and_tuning(self) -> None:
        plan_obj = plan(self.config, clean_host(self.config), "system")
        ids = {action.id for action in plan_obj.actions}
        kinds = {action.kind for action in plan_obj.actions}
        self.assertIn("apt", ids)
        self.assertIn("sysctl-file", ids)
        self.assertIn("sysctl-apply", ids)
        self.assertIn("mount-big", ids)
        self.assertIn("mkdir", kinds)
        self.assertEqual(plan_obj.status, "pending")

        apt = next(action for action in plan_obj.actions if action.id == "apt")
        self.assertTrue(apt.sudo)
        self.assertIn("git", apt.params["packages"])
        self.assertTrue(apt.params["update"])

    def test_plan_is_idempotent_once_applied(self) -> None:
        first = plan(self.config, clean_host(self.config), "system")
        host = host_after_system(self.config, first)
        second = plan(self.config, host, "system")
        self.assertEqual(second.pending, [])
        self.assertEqual(second.status, "ok")

    def test_models_directories_are_planned(self) -> None:
        plan_obj = plan(self.config, clean_host(self.config), "system")
        paths = {action.params.get("path") for action in plan_obj.actions if action.kind == "mkdir"}
        for subdir in ("diffusion_models", "text_encoders", "vae"):
            self.assertIn(str(self.config.models_dir / subdir), paths)

    def test_system_step_does_not_install_python_packages(self) -> None:
        plan_obj = plan(self.config, clean_host(self.config), "system")
        self.assertNotIn("pip_install", {action.kind for action in plan_obj.actions})
        self.assertNotIn("git_checkout", {action.kind for action in plan_obj.actions})


class GpuPlanTests(PlanTestCase):
    def test_clean_host_requires_reboot(self) -> None:
        plan_obj = plan(self.config, clean_host(self.config), "gpu")
        self.assertEqual(plan_obj.status, "needs-reboot")
        self.assertEqual(plan_obj.exit_code, 3)
        self.assertTrue(any(action.id == "usermod-groups" for action in plan_obj.pending))

    def test_configured_host_is_ok(self) -> None:
        plan_obj = plan(self.config, configured_host(self.config), "gpu")
        self.assertEqual(plan_obj.pending, [])
        self.assertEqual(plan_obj.status, "ok")

    def test_group_added_but_session_is_stale(self) -> None:
        probe = configured_host(self.config, accessible=False, session_groups=("vlabe",))
        plan_obj = plan(self.config, probe, "gpu")
        self.assertEqual(plan_obj.pending, [])
        self.assertEqual(plan_obj.status, "needs-reboot")
        self.assertTrue(any("session" in note for note in plan_obj.notes))

    def test_udev_rule_is_written_with_sudo(self) -> None:
        plan_obj = plan(self.config, clean_host(self.config), "gpu")
        udev = next(action for action in plan_obj.actions if action.id.startswith("write:"))
        self.assertTrue(udev.sudo)
        self.assertEqual(udev.params["path"], str(layout.UDEV_FILE))
        self.assertIn('KERNEL=="kfd"', udev.params["content"])

    def test_missing_kfd_blocks_the_step(self) -> None:
        perms = {path: value for path, value in base_perms().items() if path != "/dev/kfd"}
        probe = clean_host(self.config, perms=perms)
        plan_obj = plan(self.config, probe, "gpu")
        self.assertEqual(plan_obj.status, "blocked")
        self.assertEqual(plan_obj.exit_code, 4)
        self.assertIn("amdgpu", " ".join(plan_obj.notes))

    def test_no_matrix_core_assumptions(self) -> None:
        """gfx1031 must be taken from the KFD topology, not hardcoded."""
        plan_obj = plan(self.config, clean_host(self.config), "gpu")
        self.assertNotIn("HSA_OVERRIDE_GFX_VERSION", str(plan_obj.actions))


class ComfyuiPlanTests(PlanTestCase):
    def host_with_python(self, extra_files=None, extra_dirs=None, runner=None):
        files = dict(extra_files or {})
        files[str(self.config.comfyui_dir / "requirements.txt")] = "torch\n"
        runner = runner or FakeRunner([("python3.12 -c", RunResult(0, "3.12\n", ""))])
        return clean_host(
            self.config,
            files={**base_files(self.config), **files},
            dirs=set(base_dirs(self.config)) | set(extra_dirs or ()),
            runner=runner,
        )

    def test_without_python_the_step_is_blocked(self) -> None:
        probe = clean_host(self.config, runner=FakeRunner([("python3.12 -c", RunResult(1, "", ""))]))
        plan_obj = plan(self.config, probe, "comfyui")
        self.assertEqual(plan_obj.status, "blocked")
        self.assertIn("Python 3.12", " ".join(plan_obj.notes))

    def test_checkout_venv_and_torch_are_planned(self) -> None:
        plan_obj = plan(self.config, self.host_with_python(), "comfyui")
        ids = {action.id for action in plan_obj.actions}
        self.assertIn("comfyui-checkout", ids)
        self.assertIn("venv", ids)
        self.assertIn("pip-bootstrap", ids)
        self.assertIn("torch", ids)
        self.assertIn("requirements", ids)
        self.assertIn("models-symlink", ids)
        self.assertEqual(plan_obj.status, "pending")

    def test_torch_specs_use_the_detected_arch(self) -> None:
        plan_obj = plan(self.config, self.host_with_python(), "comfyui")
        torch_action = next(action for action in plan_obj.actions if action.id == "torch")
        self.assertIn(f"torch[device-gfx1031]=={self.config.torch_tag}", torch_action.params["specs"])
        self.assertTrue(torch_action.params["fallback_specs"])
        self.assertEqual(torch_action.params["index_url"], self.config.torch_index)

    def test_dirty_checkout_is_blocked(self) -> None:
        comfy_dir = str(self.config.comfyui_dir)
        runner = FakeRunner(
            [
                ("python3.12 -c", RunResult(0, "3.12\n", "")),
                (
                    f"git -C {comfy_dir} remote get-url origin",
                    RunResult(0, "https://github.com/Comfy-Org/ComfyUI.git\n", ""),
                ),
                (f"git -C {comfy_dir} rev-parse HEAD", RunResult(0, "deadbeef\n", "")),
                (f"git -C {comfy_dir} status --porcelain", RunResult(0, " M main.py\n", "")),
            ]
        )
        probe = clean_host(
            self.config,
            files={
                **base_files(self.config),
                str(self.config.comfyui_dir / "requirements.txt"): "torch\n",
            },
            dirs=set(base_dirs(self.config)) | {comfy_dir, comfy_dir + "/.git"},
            runner=runner,
        )
        plan_obj = plan(self.config, probe, "comfyui")
        self.assertEqual(plan_obj.status, "blocked")
        self.assertIn("local changes", " ".join(plan_obj.notes))

    def test_missing_requirements_blocks_after_checkout(self) -> None:
        runner = FakeRunner([("python3.12 -c", RunResult(0, "3.12\n", ""))])
        probe = clean_host(self.config, runner=runner)
        plan_obj = plan(self.config, probe, "comfyui")
        self.assertEqual(plan_obj.status, "blocked")
        self.assertIn("requirements", " ".join(plan_obj.notes))


class ManagerPlanTests(PlanTestCase):
    def test_blocked_without_comfyui(self) -> None:
        plan_obj = plan(self.config, clean_host(self.config), "manager")
        self.assertEqual(plan_obj.status, "blocked")
        self.assertIn("comfyui", " ".join(plan_obj.notes).lower())

    def test_disabled_manager_is_a_noop(self) -> None:
        probe = clean_host(self.config)
        probe.files[str(self.config.install_dir / "x")] = ""
        facts = collect(self.config, probe)
        from dataclasses import replace as dataclass_replace

        disabled = dataclass_replace(self.config, values={**self.config.values, "INSTALL_MANAGER": "0"})
        plan_obj = Planner(disabled, facts, probe).build("manager")
        self.assertEqual(plan_obj.pending, [])


class LauncherPlanTests(PlanTestCase):
    def test_files_and_modes_are_planned(self) -> None:
        launcher = str(self.config.launcher_path)
        provision_script = str(self.config.install_dir / "provision.sh")
        desktop_dir = self.tmp / "Desktop"
        probe = clean_host(
            self.config,
            files={**base_files(self.config), launcher: "#!/bin/bash\n", provision_script: "#!/bin/bash\n"},
            dirs=set(base_dirs(self.config)) | {str(desktop_dir)},
            modes={launcher: "644", provision_script: "644"},
        )
        plan_obj = plan(self.config, probe, "launcher")
        ids = {action.id for action in plan_obj.actions}
        self.assertIn(f"chmod:{launcher}", ids)
        self.assertIn(f"write:{self.config.icon_path}", ids)
        self.assertIn(f"write:{self.config.desktop_entry_path}", ids)
        self.assertEqual(plan_obj.status, "pending")

    def test_missing_launcher_blocks(self) -> None:
        plan_obj = plan(self.config, clean_host(self.config), "launcher")
        self.assertEqual(plan_obj.status, "blocked")


class VerifyPlanTests(PlanTestCase):
    def installed_host(self):
        comfy_dir = str(self.config.comfyui_dir)
        venv_python = str(self.config.venv_python)
        files = {
            **base_files(self.config),
            venv_python: "#python\n",
            str(self.config.venv_pip): "#pip\n",
        }
        dirs = set(base_dirs(self.config)) | {comfy_dir, comfy_dir + "/.git"}
        return clean_host(self.config, files=files, dirs=dirs)

    def test_needs_reboot_when_gpu_is_not_reachable(self) -> None:
        plan_obj = plan(self.config, self.installed_host(), "verify")
        self.assertEqual(plan_obj.status, "needs-reboot")
        self.assertEqual(plan_obj.exit_code, 3)

    def test_blocked_without_venv(self) -> None:
        probe = clean_host(self.config)
        probe.dirs.add(str(self.config.comfyui_dir))
        probe.dirs.add(str(self.config.comfyui_dir / ".git"))
        plan_obj = plan(self.config, probe, "verify")
        self.assertEqual(plan_obj.status, "blocked")
        self.assertIn("virtualenv", " ".join(plan_obj.notes).lower())

    def test_pending_when_everything_is_ready(self) -> None:
        probe = self.installed_host()
        probe.accessible_paths.add("/dev/kfd")
        plan_obj = plan(self.config, probe, "verify")
        self.assertEqual(plan_obj.status, "pending")


class ModelsPlanTests(PlanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.manifest = models.load_manifest()
        self.token_path = str(self.config.civitai_token_file)

    def test_blocked_without_token(self) -> None:
        plan_obj = plan(self.config, clean_host(self.config), "models", models_set="jibmix")
        self.assertEqual(plan_obj.status, "blocked")
        self.assertIn("Civitai", " ".join(plan_obj.notes))

    def test_jibmix_needs_three_downloads(self) -> None:
        probe = clean_host(self.config, files={**base_files(self.config), self.token_path: "token\n"})
        plan_obj = plan(self.config, probe, "models", models_set="jibmix")
        downloads = [action for action in plan_obj.actions if action.kind == "download" and not action.satisfied]
        self.assertEqual(len(downloads), 3)
        self.assertEqual(
            {action.params["sha256"] for action in downloads},
            {self.manifest[key].sha256 for key in models.SETS["jibmix"]},
        )
        self.assertTrue(
            all(action.params["source"] == "civitai" for action in downloads if action.params["label"] == "jibmix_v2")
        )

    def test_sft_set_is_public(self) -> None:
        plan_obj = plan(self.config, clean_host(self.config), "models", models_set="sft")
        self.assertEqual(plan_obj.status, "pending")
        downloads = [action for action in plan_obj.actions if action.kind == "download"]
        self.assertEqual(len(downloads), 3)

    def test_present_files_are_satisfied(self) -> None:
        entries = models.select(self.manifest, "jibmix")
        sizes = {str(self.config.models_dir / entry.relative_path): entry.size for entry in entries}
        probe = clean_host(
            self.config,
            sizes=sizes,
            mounted=(str(self.config.big_mount),),
            files={**base_files(self.config), self.token_path: "token\n"},
        )
        plan_obj = plan(self.config, probe, "models", models_set="jibmix")
        self.assertEqual(plan_obj.pending, [])
        self.assertEqual(plan_obj.status, "ok")

    def test_check_only_reports_missing_files(self) -> None:
        probe = clean_host(self.config, mounted=(str(self.config.big_mount),))
        plan_obj = plan(self.config, probe, "models", check_only=True)
        self.assertEqual(plan_obj.exit_code, 5)
        self.assertEqual([action for action in plan_obj.actions if not action.satisfied], [])
        self.assertTrue(any("missing" in note for note in plan_obj.notes))

    def test_no_set_lists_available_sets(self) -> None:
        plan_obj = plan(self.config, clean_host(self.config), "models")
        self.assertIn("--set jibmix", " ".join(plan_obj.notes))

    def test_unknown_set_is_rejected_by_the_cli_layer(self) -> None:
        with self.assertRaises(models.ManifestError):
            models.select(self.manifest, "nope")


class SwapPlanTests(PlanTestCase):
    def satisfied_host(self):
        path = str(self.config.swap_file)
        size = layout.parse_size(self.config.swap_size)
        runner = FakeRunner([("blkid", RunResult(0, "swap\n", ""))])
        return FakeProbe(
            files={**base_files(self.config), "/etc/fstab": f"{path} none swap sw 0 0\n"},
            dirs=base_dirs(self.config),
            perms=base_perms(),
            listdir_map=base_listdir(self.config),
            sizes={path: size},
            modes={path: "600"},
            swap=(path,),
            session_groups=("vlabe",),
            runner=runner,
        )

    def test_configured_swap_is_idempotent(self) -> None:
        plan_obj = plan(self.config, self.satisfied_host(), "swap")
        self.assertEqual(plan_obj.pending, [])
        self.assertEqual(plan_obj.status, "ok")

    def test_missing_swap_is_planned(self) -> None:
        plan_obj = plan(self.config, clean_host(self.config), "swap")
        ids = {action.id for action in plan_obj.actions}
        self.assertEqual(
            ids,
            {"swap-allocate", "swap-chmod", "swap-mkswap", "swap-on", "swap-fstab"},
        )
        self.assertEqual(plan_obj.status, "pending")

    def test_swap_size_parsing(self) -> None:
        self.assertEqual(layout.parse_size("16G"), 16 * 1024**3)
        self.assertEqual(layout.parse_size("512M"), 512 * 1024**2)
        self.assertEqual(layout.parse_size("1024"), 1024)
        with self.assertRaises(ValueError):
            layout.parse_size("0G")


class BigFstabPlanTests(PlanTestCase):
    def test_missing_big_fstab_is_planned(self) -> None:
        plan_obj = plan(self.config, clean_host(self.config), "system")
        ids = {action.id for action in plan_obj.actions}
        self.assertIn("big-fstab", ids)
        action = next(a for a in plan_obj.actions if a.id == "big-fstab")
        self.assertFalse(action.satisfied)
        self.assertEqual(action.params["path"], "/etc/fstab")
        self.assertTrue(
            action.params["line"].startswith(
                f"UUID={self.config.big_uuid} {self.config.big_mount} ext4 defaults,nofail 0 2"
            )
        )
        self.assertTrue(action.sudo)
        self.assertEqual(action.effect, EFFECT_BOOT)

    def test_present_big_fstab_is_satisfied(self) -> None:
        line = f"UUID={self.config.big_uuid} {self.config.big_mount} ext4 defaults,nofail 0 2\n"
        probe = clean_host(
            self.config,
            files={**base_files(self.config), "/etc/fstab": line},
        )
        plan_obj = plan(self.config, probe, "system")
        action = next(a for a in plan_obj.actions if a.id == "big-fstab")
        self.assertTrue(action.satisfied)


class StatusTests(PlanTestCase):
    def test_status_reports_reboot_requirement(self) -> None:
        probe = clean_host(self.config)
        plans = status(self.config, collect(self.config, probe), probe)
        self.assertTrue(reboot_needed(plans))
        self.assertEqual(
            {item.step for item in plans},
            {"system", "gpu", "comfyui", "manager", "launcher", "models", "verify"},
        )

    def test_status_is_read_only(self) -> None:
        probe = clean_host(self.config)
        before = dict(probe.files)
        status(self.config, collect(self.config, probe), probe)
        self.assertEqual(probe.files, before)


if __name__ == "__main__":
    unittest.main()
