"""arduino-cli backend. Fast tests need no arduino-cli; FV_SLOW=1 adds real Uno, ESP32 and Giga builds."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["FV_SERVICE"] = "http://127.0.0.1:9"

from fvault.backends.arduinocli import (ArduinoCli, find_sketch, read_profile, sketch_name, tool_from_path,
                                        write_profile)
from fvault.profiles import get_profile, resolve_fields
from fvault.store import Vault
from fvault.util import VaultError, iter_tree


class SketchFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fv-acli-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_project_layout_and_pin_check(self):
        work = self.tmp / "work"
        be = ArduinoCli()
        be.new_project(work, get_profile("giga-r1-m7"), "led arch")
        sk = find_sketch(work)
        self.assertEqual(sk.name, "led_arch")
        self.assertTrue((sk / "led_arch.ino").exists())
        prof = read_profile(sk)
        self.assertEqual(prof["platforms"][0], {"name": "arduino:mbed_giga", "version": "4.6.0", "index_url": None})
        self.assertTrue(prof["fqbn"].startswith("arduino:mbed_giga:giga"))
        self.assertEqual(be.check_pinned(work, {}), [])
        write_profile(sk, {**prof, "libraries": [{"name": "FastLED", "version": None},
                                                 {"name": "AM2302-Sensor", "version": "1.6.1"}]})
        self.assertEqual(be.check_pinned(work, {}), ["library 'FastLED' is not an exact version"])
        self.assertIn("FastLED", (sk / "sketch.yaml").read_text())

    def test_other_profiles_in_sketch_yaml_are_kept(self):
        sk = self.tmp / "work" / "Thing"
        sk.mkdir(parents=True)
        (sk / "Thing.ino").write_text("void setup(){}\nvoid loop(){}\n")
        (sk / "sketch.yaml").write_text("profiles:\n  mine:\n    fqbn: arduino:avr:uno\n    platforms:\n      - platform: arduino:avr (1.8.6)\n")
        write_profile(sk, {"fqbn": "arduino:avr:uno", "platforms": [{"name": "arduino:avr", "version": "1.8.8"}], "libraries": []})
        text = (sk / "sketch.yaml").read_text()
        self.assertIn("mine:", text)
        self.assertEqual(read_profile(sk)["platforms"][0]["version"], "1.8.8")

    def test_unpinned_core_and_missing_profile(self):
        sk = self.tmp / "work" / "Thing"
        sk.mkdir(parents=True)
        (sk / "Thing.ino").write_text("")
        be = ArduinoCli()
        self.assertIn("no 'vault' profile", be.check_pinned(self.tmp / "work", {})[0])
        write_profile(sk, {"fqbn": "esp32:esp32:esp32", "platforms": [{"name": "esp32:esp32", "version": None,
                           "index_url": "https://espressif.github.io/arduino-esp32/package_esp32_index.json"}]})
        self.assertEqual(be.check_pinned(self.tmp / "work", {}), ["core 'esp32:esp32' is not an exact version"])
        self.assertIn("platform_index_url", (sk / "sketch.yaml").read_text())

    def test_sketch_detection(self):
        work = self.tmp / "work"
        (work / "docs").mkdir(parents=True)
        with self.assertRaises(VaultError):
            find_sketch(work)
        for n in ("A", "B"):
            (work / n).mkdir()
            (work / n / f"{n}.ino").write_text("")
        with self.assertRaises(VaultError):
            find_sketch(work)

    def test_names(self):
        self.assertEqual(sketch_name("Bench Check!"), "Bench_Check")
        self.assertEqual(sketch_name("4x cam"), "S4x_cam")
        self.assertEqual(tool_from_path("/h/.arduino15/packages/arduino/tools/arm-none-eabi-gcc/7-2017q4"),
                         ("arm-none-eabi-gcc", "7-2017q4"))
        self.assertEqual(tool_from_path("/h/.arduino15/internal/arduino_arm-none-eabi-gcc_7-2017q4_7b7be9f526b2cb64"),
                         ("arm-none-eabi-gcc", "7-2017q4"))

    def test_flat_build_properties_fill_profile_fields(self):
        f = resolve_fields(get_profile("giga-r1-m7"), {}, {"upload.maximum_size": "1966080", "upload.protocol": "dfu"})
        self.assertEqual(f, {"flash_size": "1966080", "upload_protocol": "dfu"})


class SwitchBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fv-acli-"))
        self.v = Vault(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_platformio_layout_moves_into_a_sketch(self):
        # A project made the PlatformIO way, with an imported .ino sketch and a local library.
        self.v.create_project("Giga Thing", "giga-r1-m7", backend="platformio")
        work = self.v.pdir("giga-thing") / "work"
        (work / "src" / "main.cpp").unlink()
        (work / "src" / "GigaThing.ino").write_text("void setup(){}\nvoid loop(){}\n")
        (work / "src" / "Radar.h").write_text("")
        (work / "lib" / "MyLib").mkdir(parents=True)
        (work / "lib" / "MyLib" / "MyLib.h").write_text("")
        (work / "docs").mkdir()
        (work / "docs" / "notes.md").write_text("keep me")
        r = self.v.set_backend("giga-thing", "arduino-cli")
        self.assertEqual(self.v.project_file("giga-thing")["backend"], "arduino-cli")
        self.assertTrue((work / "GigaThing" / "GigaThing.ino").exists())
        self.assertTrue((work / "GigaThing" / "Radar.h").exists())
        self.assertTrue((work / "libraries" / "MyLib" / "MyLib.h").exists())
        self.assertTrue((work / "docs" / "notes.md").exists())
        self.assertFalse((work / "platformio.ini").exists())
        self.assertEqual(read_profile(work / "GigaThing")["platforms"][0]["version"], "4.6.0")
        self.assertTrue(r["moved"])
        self.assertIn(".build/", (self.v.pdir("giga-thing") / ".gitignore").read_text())

    def test_profile_must_support_the_backend(self):
        with self.assertRaises(VaultError):
            self.v.create_project("Nucleo", "nucleo-f401re", backend="arduino-cli")
        p = self.v.create_project("Giga", "giga-r1-m7")
        self.assertEqual(self.v.project_file(p["slug"])["backend"], "arduino-cli")

    def test_build_dir_is_not_snapshotted(self):
        work = self.tmp / "w"
        (work / ".build").mkdir(parents=True)
        (work / ".build" / "x.o").write_text("")
        (work / "S").mkdir()
        (work / "S" / "S.ino").write_text("")
        self.assertEqual([p.name for p in iter_tree(work)], ["S.ino"])


@unittest.skipUnless(os.environ.get("FV_SLOW") and shutil.which("arduino-cli"), "slow: set FV_SLOW=1 (./test.sh --slow)")
class RealArduinoCliBuilds(unittest.TestCase):
    def test_uno_esp32_giga(self):
        tmp = Path(tempfile.mkdtemp(prefix="fv-acli-slow-"))
        try:
            v = Vault(tmp)
            for name, prof, art in (("Uno Cli", "arduino-uno", "firmware.hex"),
                                    ("Esp Cli", "esp32-devkit", "merged.bin"),
                                    ("Giga Cli", "giga-r1-m7", "firmware.bin")):
                p = v.create_project(name, prof, backend="arduino-cli")
                v.pin(p["slug"])
                r = v.create_version(p["slug"], "first")
                m = r["manifest"]
                self.assertTrue(any(a["file"] == art for a in m["artifacts"]), m["artifacts"])
                self.assertTrue(m["build"]["compiler"])
                self.assertEqual(m["build"]["build_mode"], "profile")
                self.assertTrue(m["build"]["packages"])
                cmd = v.flash(p["slug"], "v1", "claude", port="/dev/ttyFAKE", dry_run=True)["command"]
                if prof == "giga-r1-m7":
                    self.assertIn("--dfuse-address=0x8040000:leave", cmd)
                    self.assertTrue(m["profile_fields"]["flash_size"])
                else:
                    self.assertIn("/dev/ttyFAKE", cmd)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
