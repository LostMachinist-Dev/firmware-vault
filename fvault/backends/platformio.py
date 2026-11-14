"""PlatformIO backend: builds, reports exact versions, and pins floating ones."""
import configparser
import json
import re
import subprocess
from pathlib import Path

from ..paths import pio_home, tool
from ..util import VaultError
from .base import Backend, BuildResult

EXACT = re.compile(r"^=?\s*\d+(\.\d+)*([+-][0-9A-Za-z.+-]+)?$")
PKG_LINE = re.compile(r"^[\s│├└─]*(?:Platform\s+)?(\S+) @ (\S+) \(required: (.+?)\)\s*$")


def _run(args, cwd=None, timeout=1800):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def _pio(*args, cwd=None, timeout=1800):
    return _run([tool("pio"), *args], cwd=cwd, timeout=timeout)


def _spec_version(spec: str) -> str | None:
    """'owner/Name @ ^1.2' -> '^1.2'; 'Name' -> None."""
    if "://" in spec or spec.startswith(("file:", "symlink:")):
        return None
    if "@" not in spec:
        return None
    return spec.rsplit("@", 1)[1].strip()


def _is_pinned(spec: str) -> bool:
    spec = spec.strip()
    if spec.startswith(("file://", "symlink://")):
        return True
    if "://" in spec or spec.startswith("git@"):
        return "#" in spec  # a URL is only pinned when it names a tag or commit
    v = _spec_version(spec)
    return bool(v) and bool(EXACT.match(v))


def _multi(value: str) -> list[str]:
    out = []
    for line in value.replace(",", "\n").splitlines():
        line = line.split(";", 1)[0].strip()
        if line:
            out.append(line)
    return out


class IniFile:
    """Read platformio.ini and rewrite single keys without losing comments."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",))
        self.cp.read(self.path)

    def envs(self) -> list[str]:
        return [s.split(":", 1)[1] for s in self.cp.sections() if s.startswith("env:")]

    def get(self, env: str, key: str, default=None):
        for sec in (f"env:{env}", "env"):
            if self.cp.has_option(sec, key):
                return self.cp.get(sec, key)
        return default

    def set(self, env: str, key: str, values: list[str]) -> None:
        lines = self.path.read_text().splitlines()
        sec = f"[env:{env}]"
        try:
            start = next(i for i, l in enumerate(lines) if l.strip() == sec)
        except StopIteration:
            raise VaultError(f"platformio.ini has no {sec} section")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
        new = [f"{key} = {values[0]}"] if len(values) == 1 else [f"{key} ="] + [f"    {v}" for v in values]
        k = start + 1
        while k < end:
            m = re.match(r"^(\w[\w.]*)\s*=", lines[k])
            if m and m.group(1) == key:
                j = k + 1
                while j < end and lines[j][:1] in (" ", "\t") and lines[j].strip():
                    j += 1
                lines[k:j] = new
                break
            k += 1
        else:
            ins = end
            while ins > start + 1 and not lines[ins - 1].strip():
                ins -= 1
            lines[ins:ins] = new
        self.path.write_text("\n".join(lines) + "\n")
        self.cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",))
        self.cp.read(self.path)


class PlatformIO(Backend):
    name = "platformio"

    # -- helpers -----------------------------------------------------------
    def env(self, work: Path, project: dict) -> str:
        ini = IniFile(work / "platformio.ini")
        envs = ini.envs()
        want = project.get("env")
        if want and want in envs:
            return want
        if not envs:
            raise VaultError("platformio.ini has no [env:...] section")
        return envs[0]

    def version(self) -> str:
        code, out = _pio("--version", timeout=60)
        m = re.search(r"version\s+(\S+)", out)
        return m.group(1) if m else out.strip()

    def package_tree(self, work: Path, env: str) -> dict:
        code, out = _pio("pkg", "list", "-d", str(work), "-e", env, timeout=600)
        if code != 0:
            raise VaultError("Could not list packages:\n" + out[-2000:])
        tree = {"platform": None, "packages": [], "libraries": []}
        section = None
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Platform "):
                section = "platform"
            elif s == "Libraries":
                section = "libraries"
                continue
            m = PKG_LINE.match(line)
            if not m:
                continue
            name, ver, req = m.groups()
            owner = req.split("/", 1)[0].strip() if "/" in req and "://" not in req else None
            item = {"name": name, "version": ver, "required": req.strip(), "owner": owner}
            if s.startswith("Platform "):
                tree["platform"] = item
            elif section == "libraries":
                if line.lstrip("│ ").startswith(("├", "└")) and line.index(name) > 4:
                    item["dependency_of_library"] = True
                tree["libraries"].append(item)
            else:
                tree["packages"].append(item)
        return tree

    def board_json(self, work: Path, ini: IniFile, env: str) -> dict:
        board = ini.get(env, "board")
        if not board:
            return {}
        local = work / "boards" / f"{board}.json"
        if local.exists():
            return json.loads(local.read_text())
        plat = (ini.get(env, "platform") or "").split("@")[0].split("/")[-1].strip()
        for cand in [pio_home() / "platforms" / plat / "boards" / f"{board}.json",
                     *(pio_home() / "platforms").glob(f"*/boards/{board}.json")]:
            if cand.exists():
                return json.loads(cand.read_text())
        return {}

    # -- interface ---------------------------------------------------------
    def new_project(self, work: Path, profile: dict, name: str) -> None:
        pio = profile["platformio"]
        env = pio["board"]
        work.mkdir(parents=True, exist_ok=True)
        (work / "src").mkdir(exist_ok=True)
        (work / "lib").mkdir(exist_ok=True)
        (work / "platformio.ini").write_text(
            f"; {name} - board profile {profile['id']}\n"
            f"; Every version here is exact. Run `fv pin` after adding a library.\n\n"
            f"[env:{env}]\n"
            f"platform = {pio['platform']}\n"
            f"board = {pio['board']}\n"
            f"framework = {pio['framework']}\n"
            f"monitor_speed = {profile.get('serial_baud', 115200)}\n"
        )
        (work / "src" / "main.cpp").write_text(
            "#include <Arduino.h>\n\n"
            "// Starting point. Replace with the real project.\n"
            "#ifndef LED_BUILTIN\n"
            "#define LED_BUILTIN 2  // not every board defines one; check yours\n"
            "#endif\n\n"
            "void setup() {\n"
            f"  Serial.begin({profile.get('serial_baud', 115200)});\n"
            "  pinMode(LED_BUILTIN, OUTPUT);\n"
            f"  Serial.println(\"{name} booted\");\n"
            "}\n\n"
            "void loop() {\n"
            "  digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));\n"
            "  delay(500);\n"
            "}\n"
        )

    def check_pinned(self, work: Path, project: dict) -> list[str]:
        ini_path = work / "platformio.ini"
        if not ini_path.exists():
            return ["There is no platformio.ini in the working source."]
        ini = IniFile(ini_path)
        env = self.env(work, project)
        problems = []
        plat = ini.get(env, "platform", "")
        if not _is_pinned(plat):
            problems.append(f"platform '{plat}' is not an exact version")
        for spec in _multi(ini.get(env, "lib_deps", "")):
            if not _is_pinned(spec):
                problems.append(f"library '{spec}' is not an exact version")
        pkgs = _multi(ini.get(env, "platform_packages", ""))
        for spec in pkgs:
            if not _is_pinned(spec):
                problems.append(f"package '{spec}' is not an exact version")
        names = {s.split("@")[0].split("/")[-1].strip() for s in pkgs}
        if not any(n.startswith("framework-") for n in names):
            problems.append("framework package is not pinned (platform_packages)")
        if not any(n.startswith("toolchain-") for n in names):
            problems.append("toolchain package is not pinned (platform_packages)")
        return problems

    def pin(self, work: Path, project: dict) -> list[str]:
        env = self.env(work, project)
        code, out = _pio("pkg", "install", "-d", str(work), "-e", env, timeout=1800)
        if code != 0:
            raise VaultError("Could not install packages:\n" + out[-3000:])
        tree = self.package_tree(work, env)
        ini = IniFile(work / "platformio.ini")
        changes = []
        p = tree["platform"]
        new_plat = f"{p['owner'] or 'platformio'}/{p['name']}@{p['version']}"
        if ini.get(env, "platform", "").replace(" ", "") != new_plat:
            changes.append(f"platform -> {new_plat}")
            ini.set(env, "platform", [new_plat])
        old_libs = _multi(ini.get(env, "lib_deps", ""))
        if old_libs:
            by_name = {l["name"].lower(): l for l in tree["libraries"]}
            new_libs = []
            for spec in old_libs:
                if _is_pinned(spec):
                    new_libs.append(spec)
                    continue
                base = spec.rsplit("@", 1)[0].strip() if "@" in spec and "://" not in spec else spec
                lib = by_name.get(base.split("/")[-1].strip().lower())
                if lib and "://" not in base:
                    pinned = f"{base}@{lib['version']}"
                    changes.append(f"library {spec} -> {pinned}")
                    new_libs.append(pinned)
                else:
                    new_libs.append(spec)
            ini.set(env, "lib_deps", new_libs)
        wanted = [k for k in tree["packages"] if k["name"].startswith(("framework-", "toolchain-"))]
        existing = _multi(ini.get(env, "platform_packages", ""))
        have = {s.split("@")[0].split("/")[-1].strip(): s for s in existing}
        new_pkgs = list(existing)
        for k in wanted:
            spec = f"{k['owner'] or 'platformio'}/{k['name']}@{k['version']}"
            if k["name"] in have and _is_pinned(have[k["name"]]):
                continue
            if k["name"] in have:
                new_pkgs[new_pkgs.index(have[k["name"]])] = spec
            else:
                new_pkgs.append(spec)
            changes.append(f"package {k['name']} -> {k['version']}")
        if new_pkgs != existing:
            ini.set(env, "platform_packages", new_pkgs)
        return changes

    def build(self, work: Path, project: dict, profile: dict) -> BuildResult:
        env = self.env(work, project)
        code, out = _pio("run", "-d", str(work), "-e", env, timeout=3600)
        warnings = sorted({l.strip() for l in out.splitlines() if re.search(r"\bwarning:", l)})
        memory = {}
        for kind in ("RAM", "Flash"):
            m = re.search(rf"^{kind}:.*?used (\d+) bytes from (\d+) bytes", out, re.M)
            if m:
                memory[kind.lower()] = {"used": int(m.group(1)), "total": int(m.group(2))}
        return BuildResult(ok=code == 0, log=out, warnings=warnings, memory=memory,
                           build_dir=work / ".pio" / "build" / env)

    def dependencies(self, work: Path, project: dict, profile: dict) -> dict:
        env = self.env(work, project)
        ini = IniFile(work / "platformio.ini")
        tree = self.package_tree(work, env)
        code, out = _pio("project", "metadata", "-d", str(work), "-e", env, "--json-output", timeout=600)
        meta = {}
        if code == 0:
            try:
                meta = json.loads(out[out.index("{"):]).get(env, {})
            except ValueError:
                meta = {}
        compiler = None
        if meta.get("cc_path"):
            c, v = _run([meta["cc_path"], "--version"], timeout=60)
            compiler = v.splitlines()[0].strip() if v else None
        flags = list(meta.get("cxx_flags") or [])
        defines = list(meta.get("defines") or [])
        opt = next((f for f in flags if re.match(r"^-O[0-3sgz]?$", f)), None)
        clock = None
        for d in defines:
            if d.startswith("F_CPU="):
                clock = int(re.sub(r"[^0-9]", "", d.split("=", 1)[1]) or 0) or None
        board = self.board_json(work, ini, env)
        if clock is None and board.get("build", {}).get("f_cpu"):
            clock = int(re.sub(r"[^0-9]", "", board["build"]["f_cpu"]))
        framework = next((k for k in tree["packages"] if k["name"].startswith("framework-")), None)
        return {
            "backend": {"name": self.name, "version": self.version()},
            "env": env,
            "board": ini.get(env, "board"),
            "platform": tree["platform"],
            "framework": framework,
            "packages": tree["packages"],
            "libraries": tree["libraries"],
            "compiler": compiler,
            "build_flags": _multi(ini.get(env, "build_flags", "")),
            "compiler_flags": flags,
            "defines": defines,
            "optimization": opt,
            "clock_hz": clock,
            "_board_json": board,
            "_ini": {k: v for k, v in (ini.cp.items(f"env:{env}") if ini.cp.has_section(f"env:{env}") else [])},
            "_framework_dir": self._package_dir(framework["name"]) if framework else None,
        }

    def _package_dir(self, name: str) -> str | None:
        root = pio_home() / "packages"
        d = root / name
        if d.exists():
            return str(d)
        hits = sorted(root.glob(f"{name}@*"))
        return str(hits[-1]) if hits else None
