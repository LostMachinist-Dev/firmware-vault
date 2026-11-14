"""arduino-cli backend: Arduino sketches, built the way the Arduino IDE builds them.

Layout of a project's work/ folder:

    work/<Sketch>/<Sketch>.ino   the sketch, its tabs (.h/.cpp) and sketch.yaml
    work/libraries/<Lib>/        libraries kept with the source (local or modified copies)
    work/docs/                   anything else worth keeping

Pinning uses arduino-cli's own build profiles: sketch.yaml has a profile called
`vault` naming the FQBN, the core (platform) at an exact version, and every
library at an exact version. A core version fixes its compiler and tools too.

Two ways to build:
- profile mode (no work/libraries): `arduino-cli compile --profile vault`.
  arduino-cli installs exactly what the profile names into its own folder, so a
  library update elsewhere can never change the build.
- checked mode (work/libraries present; arduino-cli 1.2 cannot mix profiles
  with local libraries): build with the installed core and libraries plus
  work/libraries, then refuse the build if any version differs from sketch.yaml.
"""
import json
import re
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

from ..util import VaultError
from .base import Backend, BuildResult

PROFILE = "vault"
EXACT = re.compile(r"^\d+(\.\d+)*([+-][0-9A-Za-z.+-]+)?$")
ENTRY = re.compile(r"^(.+?)\s*\((.+)\)\s*$")     # "FastLED (3.10.3)" -> name, version
BUILD_DIR = ".build"
NOT_SKETCH = {"libraries", "docs", "lib", "src", "include", "test", "data"}


def _yaml():
    try:
        import yaml
    except ImportError:
        raise VaultError("PyYAML is missing from the vault's Python (.venv/bin/pip install pyyaml)")
    return yaml


def _cli(*args, cwd=None, timeout=3600):
    exe = shutil.which("arduino-cli")
    if not exe:
        raise VaultError("arduino-cli is not installed (sudo apt install arduino-cli)")
    p = subprocess.run([exe, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def sketch_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "Sketch"
    return s if s[0].isalpha() else "S" + s


def find_sketch(work: Path) -> Path:
    """The one folder in work/ that holds <name>/<name>.ino."""
    hits = [d for d in sorted(Path(work).iterdir())
            if d.is_dir() and not d.name.startswith(".") and d.name not in NOT_SKETCH
            and (d / f"{d.name}.ino").exists()]
    if not hits:
        raise VaultError(f"No sketch in {work}: expected a folder <Name>/ holding <Name>.ino")
    if len(hits) > 1:
        raise VaultError("More than one sketch in work/: " + ", ".join(d.name for d in hits))
    return hits[0]


def local_libraries(work: Path) -> list[Path]:
    d = Path(work) / "libraries"
    return sorted(p for p in d.iterdir() if p.is_dir()) if d.is_dir() else []


def _lib_name(lib_dir: Path) -> str:
    props = lib_dir / "library.properties"
    if props.exists():
        for line in props.read_text(errors="replace").splitlines():
            if line.startswith("name="):
                return line[5:].strip()
    return lib_dir.name


def read_profile(sketch: Path) -> dict | None:
    """The vault profile from sketch.yaml, normalised: {fqbn, platforms:[{name,version,index_url}], libraries:[...]}."""
    f = sketch / "sketch.yaml"
    if not f.exists():
        return None
    doc = _yaml().safe_load(f.read_text()) or {}
    prof = (doc.get("profiles") or {}).get(PROFILE)
    if prof is None:
        return None

    def split(text):
        m = ENTRY.match(str(text).strip())
        return (m.group(1).strip(), m.group(2).strip()) if m else (str(text).strip(), None)

    plats = []
    for p in prof.get("platforms") or []:
        name, ver = split(p.get("platform", ""))
        plats.append({"name": name, "version": ver, "index_url": p.get("platform_index_url")})
    libs = []
    for l in prof.get("libraries") or []:
        if isinstance(l, str):
            name, ver = split(l)
            libs.append({"name": name, "version": ver})
        else:
            libs.append({"name": str(l), "version": None, "unsupported": True})
    return {"fqbn": prof.get("fqbn"), "platforms": plats, "libraries": libs}


def write_profile(sketch: Path, prof: dict) -> None:
    """Replace (or add) the vault profile in sketch.yaml, keeping any other profiles."""
    yaml = _yaml()
    f = sketch / "sketch.yaml"
    doc = (yaml.safe_load(f.read_text()) or {}) if f.exists() else {}
    plats = []
    for p in prof["platforms"]:
        e = {"platform": f"{p['name']} ({p['version']})" if p.get("version") else p["name"]}
        if p.get("index_url"):
            e["platform_index_url"] = p["index_url"]
        plats.append(e)
    out = {"fqbn": prof["fqbn"], "platforms": plats}
    if prof.get("libraries"):
        out["libraries"] = [f"{l['name']} ({l['version']})" if l.get("version") else l["name"] for l in prof["libraries"]]
    doc.setdefault("profiles", {})[PROFILE] = out
    f.write_text("# Written by Firmware Vault. The 'vault' profile pins the core and every library.\n"
                 + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))


@contextmanager
def without_profiles(sketch: Path):
    """arduino-cli applies a sketch.yaml default_profile even when --fqbn is given.
    Builds that must use the installed core and libraries hide the file meanwhile."""
    f, hidden = sketch / "sketch.yaml", sketch / ".sketch.yaml.vault-hidden"
    moved = f.exists()
    if moved:
        f.rename(hidden)
    try:
        yield
    finally:
        if moved:
            hidden.rename(f)


def tool_from_path(path: str) -> tuple[str, str] | None:
    """A tool's name and version from its install folder (global or profile-internal)."""
    p = Path(path)
    if p.parent.parent.name == "tools":                       # .../packages/<pk>/tools/<name>/<ver>
        return p.parent.name, p.name
    if p.parent.name == "internal":                           # .../internal/<pk>_<name>_<ver>_<hash>
        rest = p.name.split("_", 1)[1] if "_" in p.name else p.name
        parts = rest.rsplit("_", 2)
        if len(parts) == 3:
            return parts[0], parts[1]
    return None


class ArduinoCli(Backend):
    name = "arduino-cli"

    def version(self) -> str:
        code, out, err = _cli("version", "--json", timeout=60)
        try:
            return json.loads(out).get("VersionString", out.strip())
        except ValueError:
            m = re.search(r"Version:\s*(\S+)", out)
            return m.group(1) if m else out.strip()

    # -- project ------------------------------------------------------------
    def new_project(self, work: Path, profile: dict, name: str) -> None:
        a = profile.get("arduino")
        if not a:
            raise VaultError(f"Board profile {profile['id']} has no arduino-cli settings")
        sk = Path(work) / sketch_name(name)
        sk.mkdir(parents=True, exist_ok=True)
        baud = profile.get("serial_baud", 115200)
        (sk / f"{sk.name}.ino").write_text(
            "// Starting point. Replace with the real sketch.\n"
            "#ifndef LED_BUILTIN\n"
            "#define LED_BUILTIN 2  // not every board defines one; check yours\n"
            "#endif\n\n"
            "void setup() {\n"
            f"  Serial.begin({baud});\n"
            "  pinMode(LED_BUILTIN, OUTPUT);\n"
            f"  Serial.println(\"{name} booted\");\n"
            "}\n\n"
            "void loop() {\n"
            "  digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));\n"
            "  delay(500);\n"
            "}\n")
        self.adopt(work, profile)

    def adopt(self, work: Path, profile: dict) -> list[str]:
        """Bring a folder into this layout: a PlatformIO-style src/*.ino becomes the sketch,
        lib/* becomes libraries/*, and sketch.yaml gets a vault profile. Returns what moved."""
        work = Path(work)
        moved = []
        src = work / "src"
        inos = sorted(src.glob("*.ino")) if src.is_dir() else []
        if inos:
            sk = work / inos[0].stem
            sk.mkdir(exist_ok=True)
            for f in sorted(src.iterdir()):
                shutil.move(str(f), str(sk / f.name))
            inc = work / "include"
            if inc.is_dir():
                for f in sorted(inc.iterdir()):
                    shutil.move(str(f), str(sk / f.name))
                inc.rmdir()
            src.rmdir()
            moved.append(f"src/ -> {sk.name}/")
        lib = work / "lib"
        if lib.is_dir():
            libs = [d for d in lib.iterdir() if d.is_dir()]
            if libs:
                (work / "libraries").mkdir(exist_ok=True)
                for d in libs:
                    shutil.move(str(d), str(work / "libraries" / d.name))
                moved.append("lib/ -> libraries/")
            shutil.rmtree(lib, ignore_errors=True)
        ini = work / "platformio.ini"
        if ini.exists():
            ini.unlink()
            moved.append("platformio.ini removed (sketch.yaml replaces it)")
        sk = find_sketch(work)
        if read_profile(sk) is None:
            a = profile["arduino"]
            name, _, ver = a["platform"].partition("@")
            write_profile(sk, {"fqbn": a["fqbn"],
                               "platforms": [{"name": name, "version": ver or None, "index_url": a.get("index_url")}]})
            moved.append(f"{sk.name}/sketch.yaml written (fqbn {a['fqbn']})")
        return moved

    # -- pinning ------------------------------------------------------------
    def check_pinned(self, work: Path, project: dict) -> list[str]:
        try:
            sk = find_sketch(work)
        except VaultError as e:
            return [str(e)]
        prof = read_profile(sk)
        if prof is None:
            return [f"{sk.name}/sketch.yaml has no '{PROFILE}' profile"]
        problems = []
        if not prof["fqbn"]:
            problems.append("the vault profile has no fqbn (board)")
        if not prof["platforms"]:
            problems.append("the vault profile names no core (platform)")
        for p in prof["platforms"]:
            if not (p["version"] and EXACT.match(p["version"])):
                problems.append(f"core '{p['name']}' is not an exact version")
        for l in prof["libraries"]:
            if l.get("unsupported"):
                problems.append(f"library entry '{l['name']}' is not supported by this arduino-cli; use work/libraries/")
            elif not (l["version"] and EXACT.match(l["version"])):
                problems.append(f"library '{l['name']}' is not an exact version")
        return problems

    def _fqbn(self, sk: Path, project: dict) -> str:
        prof = read_profile(sk)
        if prof and prof.get("fqbn"):
            return prof["fqbn"]
        from ..profiles import get_profile
        return get_profile(project["profile"])["arduino"]["fqbn"]

    def pin(self, work: Path, project: dict) -> list[str]:
        """Fill the vault profile with the exact core and library versions installed now."""
        sk = find_sketch(work)
        old = read_profile(sk) or {"fqbn": None, "platforms": [], "libraries": []}
        fqbn = self._fqbn(sk, project)
        for p in old["platforms"]:          # make sure a pinned core is installed globally
            if p.get("version"):
                self._ensure_core(p["name"], p["version"], p.get("index_url"))
        args = ["compile", "--fqbn", fqbn, "--dump-profile", "--build-path", str(Path(work) / BUILD_DIR)]
        for lib in local_libraries(work):
            args += ["--library", str(lib)]
        with without_profiles(sk):
            code, out, err = _cli(*args, str(sk))
        if code != 0:
            raise VaultError("Could not build to find the exact versions:\n" + (err or out)[-3000:]
                             + "\nA missing library: arduino-cli lib install \"Name@version\"")
        dumped = _yaml().safe_load(out[out.index("profiles:"):]) if "profiles:" in out else {}
        d = next(iter((dumped.get("profiles") or {}).values()), {})
        local = {_lib_name(l).lower() for l in local_libraries(work)} | {l.name.lower() for l in local_libraries(work)}
        new_plats = []
        for e in d.get("platforms") or []:
            m = ENTRY.match(e["platform"])
            new_plats.append({"name": m.group(1), "version": m.group(2), "index_url": e.get("platform_index_url")})
        found = {}
        for e in d.get("libraries") or []:
            m = ENTRY.match(str(e))
            if m and m.group(1).lower() not in local:
                found[m.group(1).lower()] = {"name": m.group(1), "version": m.group(2)}
        changes = []
        # Versions already written by hand win; the build must then match them.
        plats = []
        for p in new_plats:
            have = next((o for o in old["platforms"] if o["name"] == p["name"] and o.get("version")), None)
            if have and have["version"] != p["version"]:
                raise VaultError(f"sketch.yaml pins {p['name']} {have['version']} but {p['version']} is installed. "
                                 f"Install it: arduino-cli core install {p['name']}@{have['version']}")
            plats.append(have or p)
            if not have:
                changes.append(f"core {p['name']} -> {p['version']}")
        libs = []
        for key, l in found.items():
            have = next((o for o in old["libraries"] if o["name"].lower() == key and o.get("version")), None)
            if have and have["version"] != l["version"]:
                raise VaultError(f"sketch.yaml pins {l['name']} {have['version']} but {l['version']} is installed. "
                                 f"Install it: arduino-cli lib install \"{l['name']}@{have['version']}\"")
            libs.append(have or l)
            if not have:
                changes.append(f"library {l['name']} -> {l['version']}")
        if changes or old.get("fqbn") != fqbn or len(libs) != len(old["libraries"]):
            write_profile(sk, {"fqbn": fqbn, "platforms": plats, "libraries": libs})
        return changes

    def _ensure_core(self, name: str, version: str, index_url: str | None):
        code, out, err = _cli("core", "list", "--json", timeout=120)
        try:
            data = json.loads(out)
            items = data.get("platforms", data) if isinstance(data, dict) else data
            for p in items or []:
                pid = p.get("id") or (p.get("metadata") or {}).get("id")
                inst = p.get("installed_version") or p.get("installed")
                if pid == name and inst == version:
                    return
                if pid == name and inst:
                    # One version per core is installed globally; swapping it would
                    # change every other sketch on this machine. Leave that to the human.
                    raise VaultError(f"sketch.yaml pins {name} {version} but {inst} is installed on this machine. "
                                     f"To switch: arduino-cli core install {name}@{version}")
        except ValueError:
            pass
        extra = ["--additional-urls", index_url] if index_url else []
        code, out, err = _cli("core", "install", f"{name}@{version}", *extra, timeout=3600)
        if code != 0:
            raise VaultError(f"Could not install core {name}@{version}:\n{(err or out)[-1500:]}")

    # -- build --------------------------------------------------------------
    def build(self, work: Path, project: dict, profile: dict) -> BuildResult:
        work = Path(work)
        sk = find_sketch(work)
        bdir = work / BUILD_DIR
        locals_ = local_libraries(work)
        args = ["compile", "--build-path", str(bdir), "--warnings", "default", "--json"]
        if locals_:
            args += ["--fqbn", self._fqbn(sk, project)]
            for lib in locals_:
                args += ["--library", str(lib)]
            mode = "checked"
        else:
            args += ["--profile", PROFILE]
            mode = "profile"
        if mode == "checked":
            with without_profiles(sk):
                code, out, err = _cli(*args, str(sk))
        else:
            code, out, err = _cli(*args, str(sk))
        try:
            res = json.loads(out)
        except ValueError:
            return BuildResult(ok=False, log=out + err)
        log = (res.get("compiler_out") or "") + (res.get("compiler_err") or "") + (res.get("error") or "") + err
        ok = code == 0 and res.get("success", False)
        br = res.get("builder_result") or {}
        if ok and mode == "checked":
            bad = self._check_versions(sk, br, locals_)
            if bad:
                ok = False
                log += "\nThe installed versions differ from sketch.yaml, so this would not be the pinned build:\n- " + "\n- ".join(bad)
        warnings = sorted({l.strip() for l in log.splitlines() if re.search(r"\bwarning:", l)})
        memory = {}
        for s in br.get("executable_sections_size") or []:
            kind = {"text": "flash", "data": "ram"}.get(s.get("name"))
            if kind:
                memory[kind] = {"used": s.get("size"), "total": s.get("max_size")}
        if ok:
            self._normalise(bdir, sk.name)
            (bdir / "vault-build.json").write_text(json.dumps({"mode": mode, "builder_result": br}))
        return BuildResult(ok=ok, log=f"[arduino-cli, {mode} mode]\n" + log, warnings=warnings,
                           memory=memory, build_dir=bdir)

    def _check_versions(self, sk: Path, br: dict, locals_: list[Path]) -> list[str]:
        prof = read_profile(sk) or {"platforms": [], "libraries": []}
        bad = []
        want_p = {p["name"]: p["version"] for p in prof["platforms"]}
        bp = br.get("build_platform") or {}
        if bp.get("id") in want_p and want_p[bp["id"]] != bp.get("version"):
            bad.append(f"core {bp['id']}: installed {bp.get('version')}, pinned {want_p[bp['id']]}")
        want_l = {l["name"].lower(): l["version"] for l in prof["libraries"]}
        local_dirs = {str(l.resolve()) for l in locals_}
        for l in br.get("used_libraries") or []:
            if l.get("location") in ("platform", "ref-platform") or str(Path(l.get("install_dir", "")).resolve()) in local_dirs:
                continue
            want = want_l.get(l["name"].lower())
            if want is None:
                bad.append(f"library {l['name']} {l.get('version')} is used but not in sketch.yaml (run pin)")
            elif want != l.get("version"):
                bad.append(f"library {l['name']}: installed {l.get('version')}, pinned {want}")
        return bad

    @staticmethod
    def _normalise(bdir: Path, name: str):
        """Give the outputs the names the flashers expect (firmware.hex/.bin/.elf ...)."""
        for src, dst in ((f"{name}.ino.hex", "firmware.hex"), (f"{name}.ino.bin", "firmware.bin"),
                         (f"{name}.ino.elf", "firmware.elf"), (f"{name}.ino.bootloader.bin", "bootloader.bin"),
                         (f"{name}.ino.partitions.bin", "partitions.bin")):
            if (bdir / src).exists():
                shutil.copy2(bdir / src, bdir / dst)

    # -- what went into it ----------------------------------------------------
    def dependencies(self, work: Path, project: dict, profile: dict) -> dict:
        work = Path(work)
        sk = find_sketch(work)
        f = work / BUILD_DIR / "vault-build.json"
        if not f.exists():
            raise VaultError("Build first: no build record in work/.build")
        rec = json.loads(f.read_text())
        br = rec["builder_result"]
        props = {}
        for line in br.get("build_properties") or []:
            k, _, v = line.partition("=")
            props[k] = v
        bp = br.get("build_platform") or br.get("board_platform") or {}
        platform = {"name": bp.get("id"), "version": bp.get("version")}
        tools, seen = [], set()
        for k, v in props.items():
            if k.startswith("runtime.tools.") and k.endswith(".path"):
                t = tool_from_path(v)
                if t and t not in seen:
                    seen.add(t)
                    tools.append({"name": t[0], "version": t[1]})
        local_dirs = {str(l.resolve()) for l in local_libraries(work)}
        libs = []
        for l in br.get("used_libraries") or []:
            item = {"name": l["name"], "version": l.get("version")}
            if str(Path(l.get("install_dir", "")).resolve()) in local_dirs:
                item["local"] = "work/libraries/" + Path(l["install_dir"]).name
            elif l.get("location") in ("platform", "ref-platform"):
                item["bundled_with_core"] = True
            libs.append(item)
        compiler = None
        cpath, ccmd = props.get("compiler.path", ""), props.get("compiler.cpp.cmd", "")
        if ccmd:
            exe = Path(cpath) / ccmd if cpath else Path(ccmd)
            try:
                p = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=60)
                compiler = (p.stdout or p.stderr).splitlines()[0].strip() or None
            except (OSError, IndexError):
                compiler = None
        flags_text = " ".join(props.get(k, "") for k in ("compiler.cpp.flags", "compiler.optimization_flags", "build.extra_flags"))
        for at in re.findall(r'"?@([^"\s]+)"?', flags_text):      # @cxxflags.txt files (mbed cores)
            try:
                flags_text += " " + Path(at).read_text(errors="replace")
            except OSError:
                pass
        opt = next(iter(re.findall(r"(?<!\S)(-O[0-3sgz]?)(?!\S)", flags_text)), None)
        clock = int(re.sub(r"[^0-9]", "", props["build.f_cpu"]) or 0) if props.get("build.f_cpu") else None
        prof = read_profile(sk) or {}
        return {
            "backend": {"name": self.name, "version": self.version()},
            "board": props.get("build.fqbn") or prof.get("fqbn"),
            "fqbn": props.get("build.fqbn") or prof.get("fqbn"),
            "sketch": sk.name,
            "build_mode": rec.get("mode"),
            "platform": platform,
            "framework": platform,            # the Arduino core is the framework
            "packages": tools,
            "libraries": libs,
            "compiler": compiler,
            "build_flags": [],
            "optimization": opt,
            "clock_hz": clock or None,
            "_board_json": props,
            "_ini": {},
            "_framework_dir": bp.get("install_dir"),
        }
