"""Flashers write stored artifacts to a board. They never rebuild.

The board profile's flash.method picks the flasher. Each flasher knows which
build outputs to keep (and at which flash address) and the exact command that
writes them. Adding a board family that uses a new tool means adding a class
here and naming it in the profile.
"""
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from .backends.base import Artifact
from .paths import pio_home, tool
from .util import VaultError


def _hex(v) -> int:
    return int(v, 0) if isinstance(v, str) else int(v)


# ---------------------------------------------------------------- serial ports
def list_ports() -> list[dict]:
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    by_id = {}
    d = Path("/dev/serial/by-id")
    if d.is_dir():
        for link in d.iterdir():
            try:
                by_id[str(link.resolve())] = link.name
            except OSError:
                pass
    out = []
    for p in lp.comports():
        vid = f"{p.vid:04x}" if p.vid is not None else None
        pid = f"{p.pid:04x}" if p.pid is not None else None
        out.append({"port": p.device, "usb": f"{vid}:{pid}" if vid else None,
                    "description": p.description, "serial": p.serial_number,
                    "manufacturer": p.manufacturer, "product": p.product,
                    "location": p.location, "by_id": by_id.get(p.device)})
    return sorted((p for p in out if p["usb"]), key=lambda p: p["port"])


def pick_port(profile: dict, port: str | None) -> str:
    if port:
        return port
    ports = list_ports()
    ids = {u.lower() for u in profile.get("flash", {}).get("usb_ids", [])}
    hits = [p for p in ports if p["usb"] and p["usb"].lower() in ids] or ports
    if not hits:
        raise VaultError("No board found on a USB port. Plug the board into this machine and try again.")
    if len(hits) > 1:
        names = ", ".join(f"{p['port']} ({p['description']})" for p in hits)
        raise VaultError(f"More than one board is plugged in: {names}. Choose the port.")
    return hits[0]["port"]


# ---------------------------------------------------------------- flashers
class Flasher:
    method = "base"
    needs_port = True

    def collect(self, build_dir: Path, profile: dict, deps: dict, fields: dict) -> list[Artifact]:
        raise NotImplementedError

    def command(self, vdir: Path, manifest: dict, profile: dict, port: str | None) -> list[str]:
        raise NotImplementedError

    def available(self) -> str | None:
        """None when the tool is present, else a plain-language reason."""
        return None


def _elf(build_dir: Path) -> list[Artifact]:
    elf = build_dir / "firmware.elf"
    return [Artifact(elf, "firmware.elf", "elf")] if elf.exists() else []


class Avrdude(Flasher):
    method = "avrdude"

    def _tool(self):
        d = pio_home() / "packages" / "tool-avrdude"
        exe, conf = d / "avrdude", d / "avrdude.conf"
        if not exe.exists():
            exe = d / "bin" / "avrdude"
        if exe.exists():
            return str(exe), str(conf) if conf.exists() else None
        sys_exe = shutil.which("avrdude")
        return (sys_exe, None) if sys_exe else (None, None)

    def available(self):
        return None if self._tool()[0] else "avrdude is not installed (build any AVR project once so PlatformIO fetches it, or: sudo apt install avrdude)"

    def collect(self, build_dir, profile, deps, fields):
        hexf = build_dir / "firmware.hex"
        if not hexf.exists():
            raise VaultError("The build did not produce firmware.hex")
        return [Artifact(hexf, "firmware.hex", "app")] + _elf(build_dir)

    def command(self, vdir, manifest, profile, port):
        f = profile["flash"]
        exe, conf = self._tool()
        cmd = [exe or "avrdude"]
        if conf:
            cmd += ["-C", conf]
        app = next(a for a in manifest["artifacts"] if a["role"] == "app")
        cmd += ["-p", f["mcu"], "-c", f["protocol"], "-P", port, "-b", str(f["baud"]), "-D",
                "-U", f"flash:w:{vdir / 'artifacts' / app['file']}:i"]
        return cmd


def parse_partitions(data: bytes) -> list[dict]:
    """ESP32 partition table entries: 32 bytes each, magic AA 50."""
    out = []
    for i in range(0, len(data) - 31, 32):
        e = data[i:i + 32]
        if e[:2] != b"\xaa\x50":
            break
        typ, sub, off, size = struct.unpack_from("<BBII", e, 2)
        label = e[12:28].split(b"\0", 1)[0].decode(errors="replace")
        out.append({"type": typ, "subtype": sub, "offset": off, "size": size, "label": label})
    return out


class Esptool(Flasher):
    method = "esptool"

    def _base(self):
        return [tool("python"), "-m", "esptool"] if Path(tool("python")).exists() else [sys.executable, "-m", "esptool"]

    def available(self):
        p = subprocess.run(self._base() + ["version"], capture_output=True, text=True)
        return None if p.returncode == 0 else "esptool is not installed in the vault's Python"

    def collect(self, build_dir, profile, deps, fields):
        f = profile["flash"]
        boot = build_dir / "bootloader.bin"
        parts = build_dir / "partitions.bin"
        app = build_dir / "firmware.bin"
        for p in (boot, parts, app):
            if not p.exists():
                raise VaultError(f"The build did not produce {p.name}")
        table = parse_partitions(parts.read_bytes())
        apps = [p for p in table if p["type"] == 0]
        factory = next((p for p in apps if p["subtype"] == 0x00), None)
        ota0 = next((p for p in apps if p["subtype"] == 0x10), None)
        app_part = factory or ota0 or (apps[0] if apps else None)
        if not app_part:
            raise VaultError("The partition table has no app partition")
        arts = [
            Artifact(boot, "bootloader.bin", "bootloader", _hex(f.get("bootloader_offset", "0x1000"))),
            Artifact(parts, "partitions.bin", "partitions", _hex(f.get("partition_offset", "0x8000"))),
        ]
        otadata = next((p for p in table if p["type"] == 1 and p["subtype"] == 0x00), None)
        fw_dir = deps.get("_framework_dir")
        ba0 = build_dir / "boot_app0.bin"         # arduino-cli puts it in the build folder
        if not ba0.exists():
            ba0 = Path(fw_dir) / "tools" / "partitions" / "boot_app0.bin" if fw_dir else None
        if otadata and ba0 and ba0.exists():
            arts.append(Artifact(ba0, "boot_app0.bin", "boot_app0", otadata["offset"]))
        arts.append(Artifact(app, "firmware.bin", "app", app_part["offset"]))
        # One merged image at 0x0 so the same build can be flashed from a laptop
        # browser flasher with a single file.
        merged = build_dir / "merged.bin"
        cmd = self._base() + ["--chip", f.get("chip", "esp32"), "merge-bin", "-o", str(merged)]
        cmd += self._mode_args(fields)
        for a in arts:
            cmd += [hex(a.offset), str(a.path)]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode == 0 and merged.exists():
            arts.append(Artifact(merged, "merged.bin", "merged", 0))
        return arts + _elf(build_dir)

    @staticmethod
    def _mode_args(fields: dict) -> list[str]:
        out = []
        if fields.get("flash_mode"):
            out += ["--flash-mode", str(fields["flash_mode"])]
        if fields.get("flash_freq"):
            hz = str(fields["flash_freq"]).rstrip("L")
            out += ["--flash-freq", f"{int(hz) // 1000000}m" if hz.isdigit() else hz]
        if fields.get("flash_size"):
            out += ["--flash-size", str(fields["flash_size"])]
        return out

    def command(self, vdir, manifest, profile, port):
        f = profile["flash"]
        cmd = self._base() + ["--chip", f.get("chip", "esp32"), "--port", port,
                              "--baud", str(f.get("baud", 460800)), "write-flash"]
        cmd += self._mode_args(manifest.get("profile_fields", {}))
        for a in manifest["artifacts"]:
            if a.get("offset") is not None and a["role"] not in ("merged", "elf"):
                cmd += [hex(a["offset"]), str(vdir / "artifacts" / a["file"])]
        return cmd


class OpenOCD(Flasher):
    method = "openocd"
    needs_port = False

    def _tool(self):
        d = pio_home() / "packages" / "tool-openocd"
        exe = d / "bin" / "openocd"
        if exe.exists():
            return str(exe), str(d / "openocd" / "scripts") if (d / "openocd" / "scripts").exists() else str(d / "scripts")
        sys_exe = shutil.which("openocd")
        return sys_exe, None

    def available(self):
        return None if self._tool()[0] else "openocd is not installed (build any STM32 project once so PlatformIO fetches it, or: sudo apt install openocd)"

    def collect(self, build_dir, profile, deps, fields):
        app = build_dir / "firmware.bin"
        if not app.exists():
            raise VaultError("The build did not produce firmware.bin")
        return [Artifact(app, "firmware.bin", "app", _hex(profile["flash"].get("address", "0x08000000")))] + _elf(build_dir)

    def command(self, vdir, manifest, profile, port):
        f = profile["flash"]
        exe, scripts = self._tool()
        app = next(a for a in manifest["artifacts"] if a["role"] == "app")
        cmd = [exe or "openocd"]
        if scripts:
            cmd += ["-s", scripts]
        cmd += ["-f", f["interface"], "-f", f["target"],
                "-c", f"program {vdir / 'artifacts' / app['file']} {hex(app['offset'])} verify reset exit"]
        return cmd


class DfuUtil(Flasher):
    """USB DFU (STM32 ROM/Arduino bootloader), e.g. the Arduino Giga R1.

    The board must already be in its bootloader (double-tap reset on the Giga:
    the green LED pulses), so no serial port is involved.
    """
    method = "dfu-util"
    needs_port = False

    def _tool(self):
        for d in sorted((pio_home() / "packages").glob("tool-dfuutil*"), reverse=True):
            for exe in (d / "dfu-util", d / "bin" / "dfu-util"):
                if exe.exists():
                    return str(exe)
        # The Arduino IDE / arduino-cli copy (newest version folder first).
        for exe in sorted((Path.home() / ".arduino15" / "packages" / "arduino" / "tools" / "dfu-util").glob("*/dfu-util"),
                          reverse=True):
            return str(exe)
        return shutil.which("dfu-util")

    def available(self):
        return None if self._tool() else "dfu-util is not installed (sudo apt install dfu-util)"

    def collect(self, build_dir, profile, deps, fields):
        app = build_dir / "firmware.bin"
        if not app.exists():
            raise VaultError("The build did not produce firmware.bin")
        return [Artifact(app, "firmware.bin", "app", _hex(profile["flash"]["address"]))] + _elf(build_dir)

    def command(self, vdir, manifest, profile, port):
        f = profile["flash"]
        app = next(a for a in manifest["artifacts"] if a["role"] == "app")
        return [self._tool() or "dfu-util", "--device", f["dfu_id"], "-a", str(f.get("alt", 0)),
                f"--dfuse-address={hex(app['offset'])}:leave", "-D", str(vdir / "artifacts" / app["file"])]


def dfu_bat(profile: dict, app_file: str, title: str) -> str:
    """A Windows .bat for the flash kit: finds the Arduino IDE's dfu-util and runs it."""
    f = profile["flash"]
    lines = [
        "@echo off", "setlocal", 'cd /d "%~dp0"',
        'set "DFU="',
        'for /f "delims=" %%F in (\'dir /b /s "%LOCALAPPDATA%\\Arduino15\\packages\\arduino\\tools\\dfu-util\\dfu-util.exe" 2^>nul\') do set "DFU=%%F"',
        "if not defined DFU (",
        "  echo Could not find dfu-util.exe under %LOCALAPPDATA%\\Arduino15. Is the Arduino IDE with this board's core installed?",
        "  pause", "  exit /b 1", ")",
        f"echo Flashing {title}.", "echo.",
        f"echo {f.get('bootloader_hint', 'Put the board in its bootloader, then press any key here.')}",
        "pause >nul",
        f'"%DFU%" --device {f["dfu_id"]} -a {f.get("alt", 0)} --dfuse-address={f["address"]}:leave -D "{app_file}"',
        "echo.",
        'echo If you see "File downloaded successfully" above, it worked. The board restarts by itself.',
        "echo Then click 'Flashed from laptop' in the vault page.",
        "pause",
    ]
    return "\r\n".join(lines) + "\r\n"


FLASHERS = {c.method: c for c in (Avrdude, Esptool, OpenOCD, DfuUtil)}


def get_flasher(profile: dict) -> Flasher:
    method = profile.get("flash", {}).get("method")
    if method not in FLASHERS:
        raise VaultError(f"No flasher called '{method}'. Known: {', '.join(FLASHERS)}")
    return FLASHERS[method]()
