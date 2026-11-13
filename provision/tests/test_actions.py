"""Tests for the plan/action wire format consumed by the bash executor."""

from __future__ import annotations

import base64
import io
import unittest

from comfyprov.actions import (
    STATUS_BLOCKED,
    STATUS_OK,
    STATUS_PENDING,
    STATUS_REBOOT,
    Action,
    Plan,
    emit_stream,
    merge,
)


def decode(value: str) -> str:
    return base64.b64decode(value).decode("utf-8")


class ActionTests(unittest.TestCase):
    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Action(id="x", kind="teleport", summary="nope")

    def test_unknown_effect_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Action(id="x", kind="mkdir", summary="nope", effect="tomorrow")

    def test_to_dict_keeps_params(self) -> None:
        action = Action(id="a", kind="mkdir", summary="dir", params={"path": "/tmp/x"})
        self.assertEqual(action.to_dict()["params"], {"path": "/tmp/x"})


class StreamTests(unittest.TestCase):
    def render(self, plan: Plan) -> list[list[str]]:
        buffer = io.StringIO()
        emit_stream(plan, buffer)
        return [line.split("\t") for line in buffer.getvalue().splitlines()]

    def test_header_and_footer(self) -> None:
        plan = Plan(step="gpu")
        lines = self.render(plan)
        self.assertEqual(lines[0], ["PLAN", STATUS_OK, "0", "gpu"])
        self.assertEqual(lines[-1], ["END"])

    def test_action_fields_and_params(self) -> None:
        plan = Plan(step="gpu")
        plan.add(
            Action(
                id="udev",
                kind="write_file",
                summary="Write udev rule",
                params={"path": "/etc/udev/rules.d/x", "content": 'KERNEL=="kfd"\n', "mode": "644"},
                sudo=True,
            )
        )
        lines = self.render(plan)
        header = next(line for line in lines if line[0] == "ACT")
        self.assertEqual(header[1], "0")
        self.assertEqual(header[2], "write_file")
        self.assertEqual(header[3], "1")  # sudo
        self.assertEqual(decode(header[6]), "Write udev rule")
        self.assertEqual(header[7], "0")

        params = {line[2]: decode(line[3]) for line in lines if line[0] == "PARAM"}
        self.assertEqual(params["path"], "/etc/udev/rules.d/x")
        self.assertEqual(params["content"], 'KERNEL=="kfd"\n')
        self.assertEqual(params["mode"], "644")

    def test_list_params_use_unit_separator(self) -> None:
        plan = Plan(step="system")
        plan.add(Action(id="apt", kind="apt_install", summary="apt", params={"packages": ["git", "curl"]}))
        lines = self.render(plan)
        params = {line[2]: decode(line[3]) for line in lines if line[0] == "PARAM"}
        self.assertEqual(params["packages"].split("\x1f"), ["git", "curl"])

    def test_bool_params_are_one_or_zero(self) -> None:
        plan = Plan(step="x")
        plan.add(Action(id="e", kind="exec", summary="e", params={"allow_fail": True, "argv": ["true"]}))
        lines = self.render(plan)
        params = {line[2]: decode(line[3]) for line in lines if line[0] == "PARAM"}
        self.assertEqual(params["allow_fail"], "1")

    def test_notes_are_encoded(self) -> None:
        plan = Plan(step="models")
        plan.note("no --set given")
        lines = self.render(plan)
        notes = [decode(line[1]) for line in lines if line[0] == "NOTE"]
        self.assertEqual(notes, ["no --set given"])

    def test_blocked_status_carries_exit_code(self) -> None:
        plan = Plan(step="gpu").block("no /dev/kfd")
        lines = self.render(plan)
        self.assertEqual(lines[0][1], STATUS_BLOCKED)
        self.assertEqual(lines[0][2], "4")


class PlanStateTests(unittest.TestCase):
    def test_pending_from_actions(self) -> None:
        plan = Plan(step="system")
        plan.add(Action(id="a", kind="mkdir", summary="dir"))
        plan.finalize()
        self.assertEqual(plan.status, STATUS_PENDING)
        self.assertTrue(plan.changed)

    def test_ok_when_all_actions_satisfied(self) -> None:
        plan = Plan(step="system")
        plan.add(Action(id="a", kind="mkdir", summary="dir", satisfied=True))
        plan.finalize()
        self.assertEqual(plan.status, STATUS_OK)
        self.assertFalse(plan.changed)

    def test_reboot_status_survives_finalize(self) -> None:
        plan = Plan(step="gpu")
        plan.need_reboot("reboot")
        plan.finalize()
        self.assertEqual(plan.status, STATUS_REBOOT)
        self.assertEqual(plan.exit_code, 3)

    def test_merge_keeps_the_most_severe_status(self) -> None:
        pending = Plan(step="a")
        pending.add(Action(id="x", kind="mkdir", summary="x"))
        blocked = Plan(step="b").block("missing")
        combined = merge([pending, blocked])
        self.assertEqual(combined.status, STATUS_BLOCKED)

    def test_merge_of_ok_plans(self) -> None:
        self.assertEqual(merge([Plan(step="a"), Plan(step="b")]).status, STATUS_OK)


if __name__ == "__main__":
    unittest.main()
