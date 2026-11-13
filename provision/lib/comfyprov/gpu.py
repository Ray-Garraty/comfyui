"""AMD GPU facts: gfx target parsing and group/permission classification."""

from __future__ import annotations

from dataclasses import dataclass

from .probe import Probe

KFD_PATH = "/dev/kfd"
DRI_DIR = "/dev/dri"
TOPOLOGY_DIR = "/sys/class/kfd/kfd/topology/nodes"


@dataclass(frozen=True)
class GroupFact:
    """Membership of one required group."""

    name: str
    exists: bool
    members: tuple[str, ...]
    in_session: bool

    def in_db(self, user: str) -> bool:
        return user in self.members or self.in_session

    def pending(self, user: str) -> bool:
        """True when the database grants the group but this session does not."""
        return self.in_db(user) and not self.in_session


def parse_gfx_target(raw: str | None) -> str | None:
    """Convert KFD ``gfx_target_version`` to a gfx name.

    ``100301`` -> ``gfx1031``, ``110000`` -> ``gfx1100``, ``90010`` -> ``gfx90a``.
    """
    if not raw:
        return None
    try:
        value = int(raw.strip())
    except (AttributeError, ValueError):
        return None
    if value <= 0:
        return None
    if value == 90010:
        return "gfx90a"
    major, rest = divmod(value, 10000)
    minor, patch = divmod(rest, 100)
    return f"gfx{major}{minor}{patch}"


def gfx_from_topology(probe: Probe) -> str | None:
    for node in probe.listdir(TOPOLOGY_DIR):
        text = probe.read_text(f"{TOPOLOGY_DIR}/{node}/properties") or ""
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "gfx_target_version":
                gfx = parse_gfx_target(parts[1])
                if gfx:
                    return gfx
    return None


def render_nodes(probe: Probe) -> tuple[str, ...]:
    return tuple(f"{DRI_DIR}/{name}" for name in probe.listdir(DRI_DIR) if name.startswith("renderD"))


def card_nodes(probe: Probe) -> tuple[str, ...]:
    names = [name for name in probe.listdir(DRI_DIR) if name.startswith("card") and name[4:].isdigit()]
    return tuple(f"{DRI_DIR}/{name}" for name in names)


def node_ok(probe: Probe, path: str, group: str, mode: str = "660") -> bool:
    perms = probe.node_perms(path)
    return perms == (group, mode)


def reboot_required(kfd_accessible: bool) -> bool:
    """A fresh session is the only way to gain group-based access.

    Device nodes and udev rules are applied immediately, so the remaining
    difference between "configured" and "usable" is the process group list,
    which can only be refreshed by logging in again (or rebooting).
    """
    return not kfd_accessible
