"""Tests for gfx detection and group/permission classification."""

from __future__ import annotations

import unittest

from helpers import FakeProbe

from comfyprov import gpu
from comfyprov.gpu import GroupFact


class GfxParsingTests(unittest.TestCase):
    def test_known_targets(self) -> None:
        self.assertEqual(gpu.parse_gfx_target("100301"), "gfx1031")
        self.assertEqual(gpu.parse_gfx_target("110000"), "gfx1100")
        self.assertEqual(gpu.parse_gfx_target("90010"), "gfx90a")
        self.assertEqual(gpu.parse_gfx_target(" 100301\n"), "gfx1031")

    def test_invalid_values(self) -> None:
        for value in ("", "0", "-1", "abc", None):
            self.assertIsNone(gpu.parse_gfx_target(value), value)

    def test_topology_reading(self) -> None:
        probe = FakeProbe(
            files={
                "/sys/class/kfd/kfd/topology/nodes/0/properties": "cpu_cores_count 16\ngfx_target_version 100301\n",
            },
            listdir_map={"/sys/class/kfd/kfd/topology/nodes": ["0"]},
        )
        self.assertEqual(gpu.gfx_from_topology(probe), "gfx1031")

    def test_topology_missing(self) -> None:
        self.assertIsNone(gpu.gfx_from_topology(FakeProbe()))


class GroupFactTests(unittest.TestCase):
    def test_membership_states(self) -> None:
        absent = GroupFact("render", exists=True, members=(), in_session=False)
        pending = GroupFact("render", exists=True, members=("vlabe",), in_session=False)
        active = GroupFact("render", exists=True, members=("vlabe",), in_session=True)

        self.assertFalse(absent.in_db("vlabe"))
        self.assertTrue(pending.in_db("vlabe"))
        self.assertTrue(pending.pending("vlabe"))
        self.assertFalse(active.pending("vlabe"))
        self.assertFalse(absent.exists and absent.in_db("vlabe"))


class RebootRuleTests(unittest.TestCase):
    def test_reboot_needed_only_without_access(self) -> None:
        self.assertTrue(gpu.reboot_required(False))
        self.assertFalse(gpu.reboot_required(True))


class NodePermissionTests(unittest.TestCase):
    def test_node_ok(self) -> None:
        probe = FakeProbe(perms={"/dev/kfd": ("render", "660")})
        self.assertTrue(gpu.node_ok(probe, "/dev/kfd", "render"))
        self.assertFalse(gpu.node_ok(probe, "/dev/kfd", "video"))
        self.assertFalse(gpu.node_ok(probe, "/dev/kfd", "render", mode="666"))

    def test_render_node_discovery(self) -> None:
        probe = FakeProbe(listdir_map={"/dev/dri": ["card1", "renderD128", "renderD129"]})
        self.assertEqual(gpu.render_nodes(probe), ("/dev/dri/renderD128", "/dev/dri/renderD129"))
        self.assertEqual(gpu.card_nodes(probe), ("/dev/dri/card1",))


if __name__ == "__main__":
    unittest.main()
