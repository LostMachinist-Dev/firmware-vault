"""Fast tests with a fake build backend. Run: ./test.sh  (add --slow for real PlatformIO builds)."""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["FV_SERVICE"] = "http://127.0.0.1:9"   # never talk to the live service

from fvault import mcp
from fvault.backends.base import Backend, BuildResult
from fvault.backends.platformio import IniFile, _is_pinned
from fvault.flashers import parse_partitions
from fvault.store import Vault
from fvault.util import VaultError


class FakeBackend(Backend):
    """Builds by hashing main.cpp into a fake hex. Pinned unless UNPINNED is in the ini."""
    name = "platformio"

    def version(self):
        return "fake-1.0"

    def new_project(self, work, profile, name):
        (work / "src").mkdir(parents=True)
        (work / "src" / "main.cpp").write_text("void setup(){}\nvoid loop(){}\n")
        (work / "platformio.ini").write_text("[env:uno]\nplatform = platformio/atmelavr@5.3.0\n")
        (work / "libs.json").write_text(json.dumps({"ArduinoJson": "7.4.2"}))

    def check_pinned(self, work, project):
        return ["library 'X' is not an exact version"] if "UNPINNED" in (work / "platformio.ini").read_text() else []

    def pin(self, work, project):
        ini = work / "platformio.ini"
        ini.write_text(ini.read_text().replace("UNPINNED", "pinned"))
        return ["library X -> 1.0.0"]

    def build(self, work, project, profile):
        src = (work / "src" / "main.cpp").read_text()
        if "SYNTAX ERROR" in src:
            return BuildResult(ok=False, log="main.cpp:1: error: oops")
        out = work / ".pio" / "build" / "uno"
        out.mkdir(parents=True, exist_ok=True)
        (out / "firmware.hex").write_text(":00000001FF\n;" + str(hash(src)))
        (out / "firmware.elf").write_bytes(b"\x7fELF" + src.encode())
        return BuildResult(ok=True, log="RAM: used 10 bytes from 2048 bytes", memory={"ram": {"used": 10, "total": 2048}}, build_dir=out)

    def dependencies(self, work, project, profile):
        libs = json.loads((work / "libs.json").read_text())
        return {"backend": {"name": "platformio", "version": "fake-1.0"}, "board": "uno",
                "platform": {"name": "atmelavr", "version": "5.3.0"}, "framework": {"name": "framework-arduino-avr", "version": "5.4.0"},
                "packages": [], "libraries": [{"name": k, "version": v} for k, v in libs.items()],
                "compiler": "avr-gcc 7.3.0", "build_flags": [], "optimization": "-Os", "clock_hz": 16000000,
                "_ini": {}, "_board_json": {"bootloader": {"lfuse": "0xFF", "hfuse": "0xDE", "efuse": "0xFD", "file": "optiboot.hex"}}}


class VaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fv-test-"))
        self.v = Vault(self.tmp, backend_factory=lambda name: FakeBackend())
        self.v.create_project("Test Node", "arduino-uno", "LED on D13")
        self.s = "test-node"
        self.work = self.v.pdir(self.s) / "work"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def edit(self, text):
        (self.work / "src" / "main.cpp").write_text(text)

    def test_version_captures_everything(self):
        r = self.v.create_version(self.s, "first", {"text": "hello", "not_used": [{"name": "FastLED", "why": "one LED"}]})
        m = r["manifest"]
        self.assertEqual(r["id"], "v1")
        self.assertEqual(r["status"]["status"], "draft")
        self.assertEqual(m["profile_fields"]["fuses"], {"lfuse": "0xFF", "hfuse": "0xDE", "efuse": "0xFD"})
        self.assertEqual(m["build"]["libraries"], [{"name": "ArduinoJson", "version": "7.4.2"}])
        d = self.v.vdir(self.s, "v1")
        self.assertTrue((d / "artifacts" / "firmware.hex").exists())
        self.assertTrue((d / "source" / "src" / "main.cpp").exists())
        self.assertFalse((d / "source" / ".pio").exists(), "build output must not be in the snapshot")
        self.assertIn("FastLED", r["build_story"])
        self.assertIn("LED on D13", r["wiring"])
        log = subprocess.run(["git", "log", "--oneline"], cwd=self.v.pdir(self.s), capture_output=True, text=True).stdout
        self.assertIn("v1: first", log)

    def test_unpinned_build_is_refused(self):
        ini = self.work / "platformio.ini"
        ini.write_text(ini.read_text() + "lib_deps = UNPINNED\n")
        with self.assertRaises(VaultError) as e:
            self.v.create_version(self.s, "x")
        self.assertIn("not an exact version", str(e.exception))
        self.assertEqual(self.v.pin(self.s), ["library X -> 1.0.0"])
        self.v.create_version(self.s, "x")

    def test_failed_build_makes_no_version(self):
        self.edit("SYNTAX ERROR")
        with self.assertRaises(VaultError):
            self.v.create_version(self.s, "broken")
        self.assertEqual(self.v.version_ids(self.s), [])

    def test_status_rules(self):
        self.v.create_version(self.s, "first")
        with self.assertRaises(VaultError):
            self.v.set_status(self.s, "v1", "good", "works", "claude")
        self.v.set_status(self.s, "v1", "testing", "on the bench", "claude")
        with self.assertRaises(VaultError):
            self.v.set_status(self.s, "v1", "testing", "", "claude")  # reason required
        self.v.set_status(self.s, "v1", "good", "ran 2 hours", "human")
        with self.assertRaises(VaultError):
            self.v.set_status(self.s, "v1", "draft", "redo", "claude")  # cannot undo the human
        self.assertEqual(self.v.latest_good(self.s), "v1")
        self.assertEqual(len(self.v.status(self.s, "v1")["history"]), 2)
        self.assertIn("Latest known good: v1", (self.v.pdir(self.s) / "SUMMARY.md").read_text())

    def test_latest_good_is_newest_good(self):
        self.v.create_version(self.s, "a"); self.edit("b"); self.v.create_version(self.s, "b"); self.edit("c"); self.v.create_version(self.s, "c")
        self.v.set_status(self.s, "v1", "good", "ok", "human")
        self.v.set_status(self.s, "v2", "good", "ok", "human")
        self.v.set_status(self.s, "v3", "bad", "reboots", "human")
        self.assertEqual(self.v.latest_good(self.s), "v2")

    def test_issue_rules(self):
        self.v.create_version(self.s, "first")
        with self.assertRaises(VaultError):
            self.v.add_issue(self.s, "graph wrong")  # nothing on the board yet
        self.v.mark_on_board(self.s, "v1")
        hw = self.v.add_issue(self.s, "graph scales wrong", source="voice")
        sw = self.v.add_issue(self.s, "build warning in loop()", actor="claude", source="claude", verify="claude")
        self.assertEqual((hw["id"], sw["id"]), ("I-001", "I-002"))
        with self.assertRaises(VaultError):
            self.v.close_issue(self.s, "I-001", "claude", "looks fine")  # hardware issue
        with self.assertRaises(VaultError):
            self.v.close_issue(self.s, "I-002", "claude", "")  # no evidence
        c = self.v.close_issue(self.s, "I-002", "claude", "build.log: 0 warnings")
        self.assertEqual(c["closed"]["verified_by"], "claude")
        self.edit("fixed"); self.v.create_version(self.s, "fix graph", fixes=["I-001"])
        i = json.loads((self.v.vdir(self.s, "v1") / "issues" / "I-001.json").read_text())
        self.assertEqual(i["state"], "fix_attempted")
        self.assertEqual(i["fix_attempts"][0]["version"], "v2")
        self.v.close_issue(self.s, "I-001", "human")
        self.assertEqual(self.v.open_issues(self.s), [])
        self.v.reopen_issue(self.s, "I-001", "human", "came back")
        self.assertEqual([x["id"] for x in self.v.open_issues(self.s)], ["I-001"])
        self.assertIn("I-001", (self.v.vdir(self.s, "v1") / "debug-log.md").read_text())

    def test_flash_rules(self):
        self.v.create_version(self.s, "first")
        self.v.set_status(self.s, "v1", "bad", "reboots under load", "human")
        with self.assertRaises(VaultError) as e:
            self.v.flash(self.s, "v1", "claude", port="/dev/null", dry_run=True)
        self.assertIn("Only the human", str(e.exception))
        with self.assertRaises(VaultError):
            self.v.flash(self.s, "v1", "human", port="/dev/null", dry_run=True)
        r = self.v.flash(self.s, "v1", "human", port="/dev/ttyTEST", force=True, dry_run=True)
        self.assertIn("-U", r["command"])
        self.assertTrue(any(a.endswith("firmware.hex:i") for a in r["command"]))
        self.assertIn("/dev/ttyTEST", r["command"])

    def test_tampered_binary_is_not_flashed(self):
        self.v.create_version(self.s, "first")
        (self.v.vdir(self.s, "v1") / "artifacts" / "firmware.hex").write_text("evil")
        with self.assertRaises(VaultError) as e:
            self.v.flash(self.s, "v1", "human", port="/dev/null", dry_run=True)
        self.assertIn("does not match", str(e.exception))

    def test_diff(self):
        self.v.create_version(self.s, "a")
        self.edit("void setup(){ Serial.begin(9600); }\nvoid loop(){}\n")
        (self.work / "libs.json").write_text(json.dumps({"ArduinoJson": "7.4.3", "Bounce2": "2.72.0"}))
        self.v.create_version(self.s, "b")
        d = self.v.diff(self.s, "v1", "v2")
        self.assertIn("+void setup(){ Serial.begin(9600); }", d["files"][-1]["diff"] if d["files"][-1]["file"] == "src/main.cpp" else
                      next(f["diff"] for f in d["files"] if f["file"] == "src/main.cpp"))
        self.assertEqual(d["libraries"]["added"], [{"name": "Bounce2", "version": "2.72.0"}])
        self.assertEqual(d["libraries"]["changed"], [{"name": "ArduinoJson", "from": "7.4.2", "to": "7.4.3"}])
        self.edit("work in progress")
        self.assertTrue(self.v.diff(self.s, "v2", "work")["files"])
        self.assertIn("Bounce2", self.v.get_version(self.s, "v2")["build_story"])

    def test_flash_kit(self):
        self.v.create_version(self.s, "first")
        import io, zipfile
        name, data = self.v.flash_kit(self.s, "v1")
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertIn("firmware.hex", z.namelist())
        self.assertIn("avrdude", z.read("FLASH-STEPS.txt").decode())

    def test_dfu_flasher_and_kit(self):
        import io, zipfile
        self.v.create_project("Giga Node", "giga-r1-m7", "Giga R1")
        g = "giga-node"
        work = self.v.pdir(g) / "work"
        # Fake backend makes firmware.hex; the DFU flasher wants firmware.bin.
        orig = FakeBackend.build
        def build_bin(be, work, project, profile):
            r = orig(be, work, project, profile)
            (r.build_dir / "firmware.bin").write_bytes(b"\x00\x01binary")
            return r
        FakeBackend.build = build_bin
        try:
            self.v.create_version(g, "first")
        finally:
            FakeBackend.build = orig
        r = self.v.flash(g, "v1", "human", dry_run=True)
        cmd = r["command"]
        self.assertIn("0x2341:0x0366", cmd)
        self.assertIn("--dfuse-address=0x8040000:leave", cmd)
        self.assertTrue(cmd[-1].endswith("firmware.bin"))
        name, data = self.v.flash_kit(g, "v1")
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertIn("firmware.bin", z.namelist())
        bat = z.read("FLASH.bat").decode()
        self.assertIn("\r\n", bat)
        self.assertIn("--dfuse-address=0x08040000:leave", bat)
        self.assertIn("Double-tap", bat)

    def test_mcp_enforces_claude_rules(self):
        self.v.create_version(self.s, "first")
        init = mcp.handle(self.v, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        tools = mcp.handle(self.v, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        names = {t["name"] for t in tools}
        for want in ("list_projects", "get_project", "list_versions", "get_version", "create_version", "build", "flash_version",
                     "set_version_status", "list_open_issues", "add_issue", "mark_fix_attempted", "close_issue",
                     "diff_versions", "append_build_story"):
            self.assertIn(want, names)
        for t in tools:
            json.dumps(t)
            for prop in t["inputSchema"]["properties"].values():
                self.assertNotIn("optional", prop)
        r = mcp.handle(self.v, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "set_version_status", "arguments": {"project": self.s, "version": "v1", "status": "good", "reason": "x"}}})
        self.assertTrue(r["result"]["isError"])
        r = mcp.handle(self.v, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "list_projects", "arguments": {}}})
        self.assertFalse(r["result"]["isError"])
        self.assertIsNone(mcp.handle(self.v, {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_import_helpers(self):
        """What the vault skill uses when importing a project: known issues on a version, hardware notes."""
        self.v.create_version(self.s, "imported")
        call = lambda n, a: mcp.handle(self.v, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                                "params": {"name": n, "arguments": a}})["result"]
        r = call("add_issue", {"project": self.s, "text": "known: RTC drifts", "version": "v1"})
        self.assertFalse(r["isError"], r)
        self.assertTrue((self.v.vdir(self.s, "v1") / "issues" / "I-001.json").exists())
        self.assertIsNone(self.v.state(self.s)["on_board"])   # attaching an issue does not claim a flash
        r = call("set_hardware_notes", {"project": self.s, "notes": "IMU on I2C 0x4A"})
        self.assertFalse(r["isError"], r)
        self.assertEqual(self.v.project_file(self.s)["hardware_notes"], "IMU on I2C 0x4A")
        self.edit("next"); self.v.create_version(self.s, "second")
        self.assertIn("IMU on I2C 0x4A", (self.v.vdir(self.s, "v2") / "wiring.md").read_text())
        self.assertIn("LED on D13", (self.v.vdir(self.s, "v1") / "wiring.md").read_text())


class PieceTest(unittest.TestCase):
    def test_pinning_rules(self):
        self.assertTrue(_is_pinned("bblanchon/ArduinoJson@7.4.2"))
        self.assertTrue(_is_pinned("platformio/toolchain-xtensa-esp-elf@14.2.0+20260121"))
        self.assertTrue(_is_pinned("https://github.com/x/y.git#v1.2.0"))
        self.assertFalse(_is_pinned("bblanchon/ArduinoJson@^7.0.0"))
        self.assertFalse(_is_pinned("bblanchon/ArduinoJson"))
        self.assertFalse(_is_pinned("https://github.com/x/y.git"))
        self.assertFalse(_is_pinned("platformio/espressif32"))

    def test_ini_rewrite_keeps_comments(self):
        d = Path(tempfile.mkdtemp())
        f = d / "platformio.ini"
        f.write_text("; keep me\n[env:uno]\nplatform = atmelavr\nlib_deps =\n    a@^1\n    b@2.0.0\n\n[env:other]\nboard = x\n")
        ini = IniFile(f)
        ini.set("uno", "lib_deps", ["a@1.2.3", "b@2.0.0"])
        ini.set("uno", "platform_packages", ["platformio/toolchain-atmelavr@1.70300.191015"])
        t = f.read_text()
        self.assertIn("; keep me", t)
        self.assertIn("    a@1.2.3", t)
        self.assertNotIn("a@^1", t)
        self.assertLess(t.index("platform_packages"), t.index("[env:other]"))
        self.assertEqual(ini.get("other", "board"), "x")
        shutil.rmtree(d)

    def test_partition_table(self):
        def entry(t, s, off, size, label):
            return b"\xaa\x50" + struct.pack("<BBII", t, s, off, size) + label.encode().ljust(16, b"\0") + b"\0" * 4
        data = entry(1, 2, 0x9000, 0x5000, "nvs") + entry(1, 0, 0xE000, 0x2000, "otadata") + \
            entry(0, 0x10, 0x10000, 0x140000, "app0") + b"\xff" * 32
        t = parse_partitions(data)
        self.assertEqual([p["label"] for p in t], ["nvs", "otadata", "app0"])
        self.assertEqual(t[2]["offset"], 0x10000)

    def test_pin_map_from_pcb_project(self):
        pdir = Path(os.environ.get("FV_PCB_SAMPLE", "/nonexistent")).expanduser()   # a folder with an EAGLE .sch
        if not pdir.exists():
            self.skipTest("set FV_PCB_SAMPLE to a folder holding an EAGLE .sch")
        from fvault.pcb import pin_map
        pm = pin_map(pdir)
        self.assertEqual(pm["device"], "A000067")
        nets = {p["pin"]: p["net"] for p in pm["pins"]}
        self.assertEqual(nets.get("D20/SDA"), "SDA")


@unittest.skipUnless(os.environ.get("FV_SLOW"), "set FV_SLOW=1 for real PlatformIO builds")
class RealBuildTest(unittest.TestCase):
    def test_uno_and_esp32_real_builds(self):
        tmp = Path(tempfile.mkdtemp(prefix="fv-slow-"))
        try:
            v = Vault(tmp)
            for name, prof, art in (("Uno Real", "arduino-uno", "firmware.hex"), ("Esp Real", "esp32-devkit", "merged.bin")):
                p = v.create_project(name, prof)
                v.pin(p["slug"])
                self.assertEqual(v.backend_factory("platformio").check_pinned(v.pdir(p["slug"]) / "work", v.project_file(p["slug"])), [])
                r = v.create_version(p["slug"], "first")
                self.assertTrue(any(a["file"] == art for a in r["manifest"]["artifacts"]))
                self.assertTrue(r["manifest"]["build"]["compiler"])
                cmd = v.flash(p["slug"], "v1", "claude", port="/dev/ttyFAKE", dry_run=True)["command"]
                self.assertIn("/dev/ttyFAKE", cmd)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
