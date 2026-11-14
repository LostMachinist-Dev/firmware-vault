"""Loose link to a PCB project folder (EAGLE/Fusion .sch): read the real pin map from its schematic."""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .util import VaultError


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def find_sch(project_dir: Path) -> Path:
    project_dir = Path(project_dir).expanduser()
    if project_dir.is_file() and project_dir.suffix == ".sch":
        return project_dir
    hits = sorted(project_dir.glob("*.sch"))
    if not hits:
        raise VaultError(f"No .sch schematic in {project_dir}")
    return hits[0]


def host_part_number(project_dir: Path) -> str | None:
    """The part number on the plan's 'host' line, e.g. A000067."""
    for plan in sorted(Path(project_dir).expanduser().glob("*.plan")):
        for line in plan.read_text().splitlines():
            m = re.match(r"^\s*host\s+.*\(([^)]+)\)", line)
            if m:
                return m.group(1)
    return None


def pin_map(project_dir: Path, part: str | None = None) -> dict:
    """{'part': 'M1', 'device': 'A000067', 'pins': [{'pin': 'D20/SDA', 'net': 'SDA'}, ...]}"""
    sch = find_sch(project_dir)
    root = ET.parse(sch).getroot()
    devices = {p.get("name"): p.get("deviceset") for p in root.iter("part")}
    by_part: dict[str, list] = {}
    for net in root.iter("net"):
        for pr in net.iter("pinref"):
            by_part.setdefault(pr.get("part"), []).append({"pin": pr.get("pin"), "net": net.get("name")})
    if part is None:
        hpn = host_part_number(sch.parent)
        if hpn:
            part = next((n for n, d in devices.items() if d and _norm(d) == _norm(hpn)), None)
        if part is None and by_part:
            part = max(by_part, key=lambda n: len(by_part[n]))
    if part not in by_part:
        raise VaultError(f"Part '{part}' has no connected pins in {sch.name}")
    pins = sorted({(p["pin"], p["net"]) for p in by_part[part]})
    return {"schematic": str(sch), "part": part, "device": devices.get(part),
            "pins": [{"pin": p, "net": n} for p, n in pins]}


def pin_map_markdown(pm: dict) -> str:
    lines = [f"Pin map read from `{Path(pm['schematic']).name}`, part {pm['part']} ({pm['device']}).", "",
             "| Pin | Net |", "|---|---|"]
    lines += [f"| {p['pin']} | {p['net']} |" for p in pm["pins"]]
    return "\n".join(lines) + "\n"
