"""The project store and every rule about who may do what.

Layout of one project (each project folder is its own git repo):

    <root>/<slug>/
        project.json        name, board profile, backend, hardware notes, PCB link
        state.json          what is on the board now, flash history, debug session
        SUMMARY.md          latest known-good version first, then the timeline
        work/               the live source Claude edits (not a version)
        versions/v3/
            artifacts/      the binaries you flash, plus firmware.elf
            source/         full source snapshot
            manifest.json   everything needed to rebuild and reflash
            status.json     status + one-line reason + history
            build-story.md  Claude's plain-language story of the build
            debug-log.md    append-only notes and flash events
            wiring.md       pinout / wiring notes (from the PCB when linked)
            build.log       compiler output
            issues/I-007.json
            serial/         captured serial output used as evidence

Actors are "human" (the web page) and "claude" (MCP server and CLI).
"""
import difflib
import io
import json
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from . import pcb as pcblink
from . import serialmon
from .backends import get_backend
from .flashers import dfu_bat, get_flasher, list_ports, pick_port
from .paths import vault_root
from .profiles import get_profile, resolve_fields
from .util import (VaultError, append_text, iter_tree, now_iso, read_json, sha256_file,
                   slugify, tree_hash, write_json)

STATUSES = {
    "draft": "Draft",
    "testing": "Testing",
    "good": "Known good",
    "caveats": "Known good with caveats",
    "bad": "Do not deploy",
}
HUMAN_ONLY = {"good", "caveats", "bad"}
OPEN_STATES = {"open", "fix_attempted", "reopened"}
ACTOR_NAMES = {"human": "You", "claude": "Claude Code"}

_lock = threading.RLock()


def _vnum(vid: str) -> int:
    m = re.match(r"^v(\d+)$", vid)
    return int(m.group(1)) if m else -1


class Vault:
    def __init__(self, root: Path | None = None, backend_factory=get_backend):
        self.root = Path(root) if root else vault_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.backend_factory = backend_factory

    # ------------------------------------------------------------- paths
    def pdir(self, slug: str) -> Path:
        d = self.root / slug
        if not (d / "project.json").exists():
            raise VaultError(f"No project called '{slug}'")
        return d

    def vdir(self, slug: str, vid: str) -> Path:
        d = self.pdir(slug) / "versions" / vid
        if not (d / "manifest.json").exists():
            raise VaultError(f"Project '{slug}' has no version {vid}")
        return d

    def project_file(self, slug):
        return read_json(self.pdir(slug) / "project.json")

    def state(self, slug):
        return read_json(self.pdir(slug) / "state.json", {}) or {}

    def version_ids(self, slug) -> list[str]:
        vroot = self.pdir(slug) / "versions"
        ids = [d.name for d in vroot.iterdir() if (d / "manifest.json").exists()] if vroot.exists() else []
        return sorted(ids, key=_vnum, reverse=True)

    # ------------------------------------------------------------- git
    def _git(self, slug_or_dir, *args):
        d = slug_or_dir if isinstance(slug_or_dir, Path) else self.pdir(slug_or_dir)
        return subprocess.run(["git", *args], cwd=d, capture_output=True, text=True)

    def _commit(self, slug, message: str):
        self._write_summary(slug)
        self._git(slug, "add", "-A")
        r = self._git(slug, "commit", "-q", "-m", message)
        if r.returncode != 0 and "user.email" in (r.stderr + r.stdout):
            # git has no identity on this machine: the vault commits under its own name
            self._git(slug, "-c", "user.name=Firmware Vault", "-c", "user.email=vault@localhost",
                      "commit", "-q", "-m", message)

    # ------------------------------------------------------------- projects
    def list_projects(self) -> list[dict]:
        out = []
        for d in sorted(self.root.iterdir()):
            if (d / "project.json").exists():
                out.append(self.project_summary(d.name))
        return out

    def project_summary(self, slug) -> dict:
        p = self.project_file(slug)
        st = self.state(slug)
        vids = self.version_ids(slug)
        good = self.latest_good(slug)
        prof = {}
        try:
            prof = get_profile(p["profile"])
        except VaultError:
            pass
        return {"slug": slug, "name": p["name"], "profile": p["profile"],
                "board": prof.get("name", p["profile"]), "backend": p["backend"],
                "pcb": p.get("pcb"), "hardware_notes": p.get("hardware_notes", ""),
                "versions": len(vids), "latest": vids[0] if vids else None,
                "latest_good": good, "on_board": (st.get("on_board") or {}).get("version"),
                "debug_active": bool((st.get("debug") or {}).get("active"))}

    @staticmethod
    def _check_backend(profile: dict, backend: str) -> str:
        need = {"platformio": "platformio", "arduino-cli": "arduino"}
        if backend not in need:
            raise VaultError(f"No backend called '{backend}'. Known: {', '.join(need)}")
        if need[backend] not in profile:
            raise VaultError(f"Board profile {profile['id']} has no {backend} settings "
                             f"(add a \"{need[backend]}\" block to profiles/{profile['id']}.json)")
        return backend

    def create_project(self, name: str, profile_id: str, hardware_notes: str = "",
                       pcb: dict | None = None, slug: str | None = None, backend: str | None = None) -> dict:
        profile = get_profile(profile_id)
        backend_name = self._check_backend(profile, backend or profile["backend"])
        slug = slug or slugify(name)
        d = self.root / slug
        if d.exists():
            raise VaultError(f"A project called '{slug}' already exists")
        with _lock:
            d.mkdir(parents=True)
            backend = self.backend_factory(backend_name)
            backend.new_project(d / "work", profile, name)
            write_json(d / "project.json", {
                "name": name, "slug": slug, "profile": profile_id, "backend": backend_name,
                "env": profile.get("platformio", {}).get("board") if backend_name == "platformio" else None,
                "hardware_notes": hardware_notes, "pcb": pcb, "created": now_iso(), "next_issue": 1})
            write_json(d / "state.json", {"on_board": None, "flash_history": [], "debug": {"active": False}})
            (d / "versions").mkdir()
            (d / ".gitignore").write_text(".pio/\n.build/\n*.tmp\n")
            self._git(d, "init", "-q")
            self._commit(slug, f"New project {name} on {profile['name']}")
        return self.project_summary(slug)

    def update_project(self, slug, hardware_notes=None, pcb=None) -> dict:
        with _lock:
            p = self.project_file(slug)
            if hardware_notes is not None:
                p["hardware_notes"] = hardware_notes
            if pcb is not None:
                pcb = dict(pcb)
                pcb["path"] = str(Path(pcb["path"]).expanduser())
                pcblink.find_sch(Path(pcb["path"]))  # fail early if it is not a pcb project
                p["pcb"] = pcb
            write_json(self.pdir(slug) / "project.json", p)
            self._commit(slug, "Project details updated")
        return self.project_summary(slug)

    def set_backend(self, slug, backend: str) -> dict:
        """Switch a project's build tool. Only before its first version (versions never change)."""
        with _lock:
            p = self.project_file(slug)
            if self.version_ids(slug):
                raise VaultError(f"{slug} already has versions; make a new project for the other build tool")
            profile = get_profile(p["profile"])
            self._check_backend(profile, backend)
            work = self.pdir(slug) / "work"
            be = self.backend_factory(backend)
            moved = be.adopt(work, profile) if hasattr(be, "adopt") else []
            p["backend"] = backend
            p["env"] = profile.get("platformio", {}).get("board") if backend == "platformio" else None
            write_json(self.pdir(slug) / "project.json", p)
            gi = self.pdir(slug) / ".gitignore"
            if gi.exists() and ".build/" not in gi.read_text():
                gi.write_text(gi.read_text() + ".build/\n")
            self._commit(slug, f"Build tool is now {backend}" + (": " + "; ".join(moved) if moved else ""))
        return {"project": slug, "backend": backend, "moved": moved}

    def pin_map(self, slug) -> dict:
        p = self.project_file(slug)
        if not p.get("pcb"):
            raise VaultError("This project is not linked to a PCB project")
        return pcblink.pin_map(Path(p["pcb"]["path"]), p["pcb"].get("part"))

    # ------------------------------------------------------------- versions
    def latest_good(self, slug) -> str | None:
        for vid in self.version_ids(slug):
            if self.status(slug, vid)["status"] == "good":
                return vid
        return None

    def status(self, slug, vid) -> dict:
        return read_json(self.pdir(slug) / "versions" / vid / "status.json",
                         {"status": "draft", "reason": "", "history": []})

    def list_versions(self, slug) -> list[dict]:
        st = self.state(slug)
        out = []
        for vid in self.version_ids(slug):
            m = read_json(self.vdir(slug, vid) / "manifest.json")
            s = self.status(slug, vid)
            issues = self.issues(slug, vid)
            out.append({"id": vid, "status": s["status"], "label": STATUSES[s["status"]],
                        "reason": s.get("reason", ""), "created": m["created"],
                        "note": m["human"].get("note", ""),
                        "open_issues": sum(1 for i in issues if i["state"] in OPEN_STATES),
                        "on_board": (st.get("on_board") or {}).get("version") == vid,
                        "binary_sha256": m["binary"]["sha256"]})
        return out

    def get_version(self, slug, vid) -> dict:
        d = self.vdir(slug, vid)
        read = lambda n: (d / n).read_text() if (d / n).exists() else ""
        return {"id": vid, "manifest": read_json(d / "manifest.json"), "status": self.status(slug, vid),
                "status_label": STATUSES[self.status(slug, vid)["status"]],
                "build_story": read("build-story.md"), "debug_log": read("debug-log.md"),
                "wiring": read("wiring.md"), "issues": self.issues(slug, vid),
                "source_files": [f.relative_to(d / "source").as_posix() for f in iter_tree(d / "source")],
                "serial_logs": sorted(f.name for f in (d / "serial").glob("*.log")) if (d / "serial").exists() else []}

    def _backend_and_profile(self, slug):
        p = self.project_file(slug)
        return p, self.backend_factory(p["backend"]), get_profile(p["profile"])

    def build(self, slug) -> dict:
        p, backend, profile = self._backend_and_profile(slug)
        work = self.pdir(slug) / "work"
        r = backend.build(work, p, profile)
        return {"ok": r.ok, "warnings": r.warnings, "memory": r.memory,
                "unpinned": backend.check_pinned(work, p), "log_tail": r.log[-4000:]}

    def pin(self, slug) -> list[str]:
        p, backend, profile = self._backend_and_profile(slug)
        with _lock:
            changes = backend.pin(self.pdir(slug) / "work", p)
            if changes:
                self._commit(slug, "Pinned exact versions: " + "; ".join(changes))
        return changes

    def create_version(self, slug, note: str, summary: dict | None = None, actor: str = "claude",
                       hardware_revision: str | None = None, fixes: list[str] | None = None) -> dict:
        """Build the working source and seal it as the next version."""
        p, backend, profile = self._backend_and_profile(slug)
        work = self.pdir(slug) / "work"
        problems = backend.check_pinned(work, p)
        if problems:
            raise VaultError("Not every version is pinned, so this build could not be rebuilt later:\n- "
                             + "\n- ".join(problems) + "\nRun pin first (fv pin / pin_versions).")
        r = backend.build(work, p, profile)
        if not r.ok:
            raise VaultError("Build failed, no version created.\n" + r.log[-3000:])
        deps = backend.dependencies(work, p, profile)
        fields = resolve_fields(profile, deps.get("_ini", {}), deps.get("_board_json", {}))
        flasher = get_flasher(profile)
        arts = flasher.collect(r.build_dir, profile, deps, fields)

        with _lock:
            vids = self.version_ids(slug)
            parent = vids[0] if vids else None
            vid = f"v{(_vnum(parent) + 1) if parent else 1}"
            d = self.pdir(slug) / "versions" / vid
            (d / "artifacts").mkdir(parents=True)
            (d / "issues").mkdir()
            art_meta = []
            for a in arts:
                shutil.copy2(a.path, d / "artifacts" / a.name)
                art_meta.append({"file": a.name, "role": a.role, "offset": a.offset,
                                 "sha256": sha256_file(d / "artifacts" / a.name),
                                 "size": (d / "artifacts" / a.name).stat().st_size})
            for f in iter_tree(work):
                dst = d / "source" / f.relative_to(work)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
            app = next(a for a in art_meta if a["role"] == "app")
            pm = None
            if p.get("pcb"):
                try:
                    pm = pcblink.pin_map(Path(p["pcb"]["path"]), p["pcb"].get("part"))
                except VaultError:
                    pm = None
            clean = {k: v for k, v in deps.items() if not k.startswith("_")}
            parent_m = read_json(self.vdir(slug, parent) / "manifest.json") if parent else None
            manifest = {
                "schema": 1, "project": slug, "version": vid, "parent": parent, "created": now_iso(),
                "created_by": ACTOR_NAMES.get(actor, actor),
                "build": {"profile": profile["id"], "variant": deps.get("board"), **clean},
                "profile_fields": fields,
                "source_sha256": tree_hash(d / "source"),
                "binary": {"file": app["file"], "sha256": app["sha256"], "size": app["size"]},
                "artifacts": art_meta,
                "memory": r.memory, "warnings": r.warnings,
                "flash": {"method": profile["flash"]["method"],
                          **{k: v for k, v in profile["flash"].items() if k not in ("method", "usb_ids")},
                          "bootloader": fields.get("bootloader") or next(
                              (a["sha256"] for a in art_meta if a["role"] == "bootloader"), None)},
                "human": {"note": note, "hardware_revision": hardware_revision or (p.get("pcb") or {}).get("revision"),
                          "wiring": "wiring.md", "hardware_notes": p.get("hardware_notes", ""),
                          "pcb": p.get("pcb")},
                "summary": summary or {},
            }
            manifest["changes"] = self._lib_changes(parent_m, manifest) if parent_m else None
            write_json(d / "manifest.json", manifest)
            write_json(d / "status.json", {"status": "draft", "reason": "Just built, not tried on hardware.",
                                           "set_by": ACTOR_NAMES.get(actor, actor), "at": now_iso(), "history": []})
            (d / "build.log").write_text(r.log)
            (d / "build-story.md").write_text(self._story(p, profile, manifest, summary or {}))
            (d / "debug-log.md").write_text(f"# Debug log, {p['name']} {vid}\n\nAppend-only. Newest at the bottom.\n\n")
            if pm:
                wiring = f"# Wiring, {p['name']} {vid}\n\n" + pcblink.pin_map_markdown(pm)
            elif (parent and (self.vdir(slug, parent) / "wiring.md").exists()
                  and (parent_m or {}).get("human", {}).get("hardware_notes", "") == p.get("hardware_notes", "")):
                # notes unchanged: keep the parent's wiring (it may have been edited by hand)
                wiring = (self.vdir(slug, parent) / "wiring.md").read_text().replace(f" {parent}\n", f" {vid}\n", 1)
            else:
                wiring = f"# Wiring, {p['name']} {vid}\n\n{p.get('hardware_notes') or 'No wiring notes yet.'}\n"
            (d / "wiring.md").write_text(wiring)
            self._commit(slug, f"{vid}: {note}")
        for iid in fixes or []:
            self.mark_fix_attempted(slug, iid, vid, f"{vid} tries a fix", actor)
        return self.get_version(slug, vid)

    @staticmethod
    def _lib_changes(old: dict, new: dict) -> dict:
        def libs(m):
            return {l["name"]: l["version"] for l in m["build"].get("libraries", [])}
        a, b = libs(old), libs(new)
        ob, nb = old["build"], new["build"]
        tool = []
        for key in ("platform", "framework"):
            ov, nv = (ob.get(key) or {}).get("version"), (nb.get(key) or {}).get("version")
            if ov != nv:
                tool.append({"what": key, "from": ov, "to": nv})
        if ob.get("compiler") != nb.get("compiler"):
            tool.append({"what": "compiler", "from": ob.get("compiler"), "to": nb.get("compiler")})
        return {"added": [{"name": n, "version": b[n]} for n in sorted(set(b) - set(a))],
                "removed": [{"name": n, "version": a[n]} for n in sorted(set(a) - set(b))],
                "changed": [{"name": n, "from": a[n], "to": b[n]} for n in sorted(set(a) & set(b)) if a[n] != b[n]],
                "toolchain": tool}

    def _story(self, p, profile, m, summary) -> str:
        b = m["build"]
        L = [f"# {p['name']} {m['version']}: build story", "",
             f"*{m['created']} · {m['created_by']} · board profile {profile['name']}*", "",
             "## What changed", "", m["human"]["note"] or "(no note)", ""]
        if summary.get("text"):
            L += ["## Summary", "", summary["text"], ""]
        for key, title in (("added", "Libraries added, and why"), ("ported", "Ported or written from scratch"),
                           ("not_used", "Deliberately not used")):
            items = summary.get(key) or []
            if items:
                L += [f"## {title}", ""]
                for it in items:
                    L.append(f"- **{it.get('name', '')}**: {it.get('why', '')}" if isinstance(it, dict) else f"- {it}")
                L.append("")
        ch = m.get("changes")
        if ch and any(ch.values()):
            L += ["## Version changes from " + m["parent"], ""]
            L += [f"- added {x['name']} {x['version']}" for x in ch["added"]]
            L += [f"- removed {x['name']} {x['version']}" for x in ch["removed"]]
            L += [f"- {x['name']} {x['from']} -> {x['to']}" for x in ch["changed"]]
            L += [f"- {x['what']} {x['from']} -> {x['to']}" for x in ch["toolchain"]]
            L.append("")
        L += ["## Exact build", "",
              f"- Backend: {b['backend']['name']} {b['backend']['version']}",
              f"- Platform: {(b.get('platform') or {}).get('name')} {(b.get('platform') or {}).get('version')}",
              f"- Framework: {(b.get('framework') or {}).get('name')} {(b.get('framework') or {}).get('version')}",
              f"- Compiler: {b.get('compiler')}",
              f"- Optimisation: {b.get('optimization')}, clock {b.get('clock_hz')} Hz",
              "- Libraries: " + (", ".join(f"{l['name']} {l['version']}" for l in b.get("libraries", [])) or "none"),
              f"- Binary sha256: `{m['binary']['sha256']}`", "",
              "## Notes added during the work", ""]
        return "\n".join(L) + "\n"

    def append_story(self, slug, vid, text: str, actor="claude") -> None:
        with _lock:
            append_text(self.vdir(slug, vid) / "build-story.md", f"\n*{now_iso()} · {ACTOR_NAMES.get(actor, actor)}*\n\n{text.strip()}\n")
            self._commit(slug, f"{vid}: build story note")

    def set_status(self, slug, vid, status: str, reason: str, actor: str) -> dict:
        if status not in STATUSES:
            raise VaultError(f"Unknown status '{status}'. Use one of: {', '.join(STATUSES)}")
        if not reason or not reason.strip():
            raise VaultError("Every status needs a one-line reason")
        with _lock:
            cur = self.status(slug, vid)
            if actor != "human":
                if status in HUMAN_ONLY:
                    raise VaultError(f"Only the human can mark a version '{STATUSES[status]}'. "
                                     "That needs a try on real hardware. Claude Code may set draft or testing.")
                if cur["status"] in HUMAN_ONLY:
                    raise VaultError(f"{vid} is '{STATUSES[cur['status']]}', set by the human. Claude Code cannot change it.")
            hist = cur.get("history", [])
            hist.append({k: cur.get(k) for k in ("status", "reason", "set_by", "at")})
            new = {"status": status, "reason": reason.strip(), "set_by": ACTOR_NAMES.get(actor, actor),
                   "at": now_iso(), "history": hist}
            write_json(self.vdir(slug, vid) / "status.json", new)
            self._log(slug, vid, f"Status set to **{STATUSES[status]}** by {new['set_by']}: {new['reason']}")
            self._commit(slug, f"{vid}: {STATUSES[status]} - {new['reason']}")
        return new

    # ------------------------------------------------------------- flashing
    def verify_artifacts(self, slug, vid) -> list[str]:
        d = self.vdir(slug, vid)
        m = read_json(d / "manifest.json")
        bad = []
        for a in m["artifacts"]:
            f = d / "artifacts" / a["file"]
            if not f.exists():
                bad.append(f"{a['file']} is missing")
            elif sha256_file(f) != a["sha256"]:
                bad.append(f"{a['file']} does not match its recorded hash")
        return bad

    def flash(self, slug, vid, actor: str, port: str | None = None, force: bool = False,
              dry_run: bool = False, log=lambda line: None) -> dict:
        p, _, profile = self._backend_and_profile(slug)
        d = self.vdir(slug, vid)
        m = read_json(d / "manifest.json")
        st = self.status(slug, vid)
        if st["status"] == "bad":
            if actor != "human":
                raise VaultError(f"{vid} is marked Do not deploy ({st['reason']}). Only the human can flash it anyway.")
            if not force:
                raise VaultError(f"{vid} is marked Do not deploy: {st['reason']}")
        bad = self.verify_artifacts(slug, vid)
        if bad:
            raise VaultError("Refusing to flash, stored files do not match the manifest: " + "; ".join(bad))
        log(f"Hashes verified for {len(m['artifacts'])} stored files")
        flasher = get_flasher(profile)
        why = flasher.available()
        if why:
            raise VaultError(why)
        use_port = pick_port(profile, port) if flasher.needs_port else port
        cmd = flasher.command(d, m, profile, use_port)
        if dry_run:
            return {"ok": True, "dry_run": True, "command": cmd, "port": use_port}
        log(f"Writing {vid} to {use_port or 'the programmer'} with {flasher.method}")
        out = []
        with serialmon.released(use_port, f"flashing {slug} {vid}"):
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    out.append(line)
                    log(line)
            proc.wait()
        if proc.returncode != 0:
            with _lock:
                self._log(slug, vid, f"Flash FAILED on {use_port} ({flasher.method}, exit {proc.returncode})")
                self._commit(slug, f"{vid}: flash failed")
            raise VaultError(f"Flashing failed (exit {proc.returncode}). Last lines:\n" + "\n".join(out[-15:]))
        with _lock:
            state = self.state(slug)
            prev = (state.get("on_board") or {}).get("version")
            state["on_board"] = {"version": vid, "at": now_iso(), "port": use_port, "by": ACTOR_NAMES.get(actor, actor)}
            state.setdefault("flash_history", []).append(state["on_board"])
            write_json(self.pdir(slug) / "state.json", state)
            self._log(slug, vid, f"Flashed to {use_port or 'programmer'} by {ACTOR_NAMES.get(actor, actor)}"
                      + (" (over a Do not deploy warning)" if st["status"] == "bad" else ""))
            self._commit(slug, f"{vid}: flashed")
        carried = [i for i in self.issues(slug, prev) if i["state"] in OPEN_STATES] if prev and prev != vid else []
        return {"ok": True, "version": vid, "port": use_port, "previous": prev, "carried_issues": carried,
                "log_tail": out[-20:]}

    def mark_on_board(self, slug, vid, actor="human", where="laptop") -> dict:
        """Record a flash done elsewhere, e.g. from the laptop with the flash kit."""
        self.vdir(slug, vid)
        with _lock:
            state = self.state(slug)
            state["on_board"] = {"version": vid, "at": now_iso(), "port": where, "by": ACTOR_NAMES.get(actor, actor)}
            state.setdefault("flash_history", []).append(state["on_board"])
            write_json(self.pdir(slug) / "state.json", state)
            self._log(slug, vid, f"Marked as on the board (flashed from the {where})")
            self._commit(slug, f"{vid}: on the board ({where})")
        return state["on_board"]

    def flash_kit(self, slug, vid) -> tuple[str, bytes]:
        """A zip with the binaries and plain steps, for flashing from the laptop."""
        p, _, profile = self._backend_and_profile(slug)
        d = self.vdir(slug, vid)
        m = read_json(d / "manifest.json")
        method = profile["flash"]["method"]
        steps = [f"{p['name']} {vid} - flash kit", "=" * 40, "",
                 f"Status: {STATUSES[self.status(slug, vid)['status']]}",
                 f"Binary sha256: {m['binary']['sha256']}", ""]
        if method == "esptool":
            steps += ["Easiest, from Chrome or Edge on the laptop:",
                      "1. Plug the board into the laptop.",
                      "2. Open https://espressif.github.io/esptool-js/",
                      "3. Click Connect and pick the board's port.",
                      "4. Set Flash Address to 0x0 and choose merged.bin from this kit.",
                      "5. Click Program. Wait for it to finish, then press the board's reset button.",
                      "6. In the vault page, click 'Flashed from laptop' so notes attach to this version.", ""]
        elif method == "avrdude":
            f = profile["flash"]
            steps += ["From the laptop, with avrdude installed (it comes with the Arduino IDE):",
                      f"avrdude -p {f['mcu']} -c {f['protocol']} -P COM3 -b {f['baud']} -D -U flash:w:firmware.hex:i",
                      "Change COM3 to the board's port (Device Manager, Ports).",
                      "Then click 'Flashed from laptop' in the vault page.", ""]
        elif method == "dfu-util":
            steps += ["From the Windows laptop (needs the Arduino IDE with this board's core installed):",
                      "1. Plug the laptop into the board by USB.",
                      "2. Double-click FLASH.bat in this kit and follow what it says.",
                      f"   ({profile['flash'].get('bootloader_hint', '')})",
                      "3. Then click 'Flashed from laptop' in the vault page.", ""]
        elif method == "openocd":
            steps += ["From the laptop with STM32CubeProgrammer: connect over ST-LINK,",
                      f"open firmware.bin, address {profile['flash'].get('address', '0x08000000')}, Download.",
                      "Then click 'Flashed from laptop' in the vault page.", ""]
        steps += ["Files and flash addresses:"]
        steps += [f"  {a['file']:<16} {hex(a['offset']) if a.get('offset') is not None else '':<10} {a['sha256']}"
                  for a in m["artifacts"]]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("FLASH-STEPS.txt", "\r\n".join(steps) + "\r\n")
            z.write(d / "manifest.json", "manifest.json")
            if method == "dfu-util":
                app = next(a for a in m["artifacts"] if a["role"] == "app")
                z.writestr("FLASH.bat", dfu_bat(profile, app["file"], f"{p['name']} {vid}"))
            for a in m["artifacts"]:
                if a["role"] != "elf":
                    z.write(d / "artifacts" / a["file"], a["file"])
        return f"{slug}-{vid}-flash-kit.zip", buf.getvalue()

    def ports(self) -> list[dict]:
        return list_ports()

    # ------------------------------------------------------------- issues and debug notes
    def _log(self, slug, vid, text: str):
        t = time.strftime("%Y-%m-%d %H:%M:%S")
        append_text(self.vdir(slug, vid) / "debug-log.md", f"- `{t}` {text}\n")

    def issues(self, slug, vid=None) -> list[dict]:
        if vid is None:
            out = []
            for v in self.version_ids(slug):
                out += self.issues(slug, v)
            return out
        d = self.pdir(slug) / "versions" / vid / "issues"
        return [read_json(f) for f in sorted(d.glob("I-*.json"))] if d.exists() else []

    def open_issues(self, slug, vid=None) -> list[dict]:
        return [i for i in self.issues(slug, vid) if i["state"] in OPEN_STATES]

    def _issue_path(self, slug, iid) -> Path:
        hits = list((self.pdir(slug) / "versions").glob(f"*/issues/{iid}.json"))
        if not hits:
            raise VaultError(f"No issue {iid} in '{slug}'")
        return hits[0]

    def set_debug(self, slug, active: bool) -> dict:
        with _lock:
            st = self.state(slug)
            on = (st.get("on_board") or {}).get("version")
            if active and not on:
                raise VaultError("Flash a version first. Notes attach to whatever is on the board.")
            st["debug"] = {"active": active, "since": now_iso() if active else None, "version": on}
            write_json(self.pdir(slug) / "state.json", st)
            self._log(slug, on, "Debug notes started" if active else "Debug notes stopped") if on else None
            self._commit(slug, f"Debug notes {'started' if active else 'stopped'}")
        return st["debug"]

    def add_issue(self, slug, text: str, actor: str = "human", source: str = "typed",
                  verify: str | None = None, vid: str | None = None) -> dict:
        """A debug note becomes its own issue on the version currently on the board."""
        text = (text or "").strip()
        if not text:
            raise VaultError("The note is empty")
        with _lock:
            st = self.state(slug)
            vid = vid or (st.get("on_board") or {}).get("version")
            if not vid:
                raise VaultError("Nothing is on the board yet. Flash a version first, notes attach to it.")
            self.vdir(slug, vid)
            p = self.project_file(slug)
            n = p.get("next_issue", 1)
            p["next_issue"] = n + 1
            write_json(self.pdir(slug) / "project.json", p)
            iid = f"I-{n:03d}"
            issue = {"id": iid, "version": vid, "text": text, "created": now_iso(),
                     "source": source, "raised_by": ACTOR_NAMES.get(actor, actor),
                     "verify": verify or ("claude" if actor == "claude" else "human"),
                     "state": "open", "fix_attempts": [], "closed": None, "history": []}
            write_json(self.vdir(slug, vid) / "issues" / f"{iid}.json", issue)
            self._log(slug, vid, f"{iid} ({source}, {issue['raised_by']}): {text}")
            self._commit(slug, f"{vid}: {iid} {text[:60]}")
        return issue

    def mark_fix_attempted(self, slug, iid, vid, note: str, actor="claude") -> dict:
        with _lock:
            path = self._issue_path(slug, iid)
            self.vdir(slug, vid)
            i = read_json(path)
            if i["state"] == "verified":
                raise VaultError(f"{iid} is already verified fixed")
            i["history"].append({"state": i["state"], "at": now_iso()})
            i["state"] = "fix_attempted"
            i["fix_attempts"].append({"version": vid, "note": note, "at": now_iso(), "by": ACTOR_NAMES.get(actor, actor)})
            write_json(path, i)
            self._log(slug, i["version"], f"{iid}: fix attempted in {vid}. {note}")
            self._commit(slug, f"{iid}: fix attempted in {vid}")
        return i

    def close_issue(self, slug, iid, actor: str, evidence: str = "", note: str = "") -> dict:
        with _lock:
            path = self._issue_path(slug, iid)
            i = read_json(path)
            if actor != "human":
                if i.get("verify") != "claude":
                    raise VaultError(f"{iid} needs the physical hardware to check, so only the human can close it. "
                                     "Use mark_fix_attempted instead.")
                if not evidence.strip():
                    raise VaultError("Claude Code must store evidence when closing an issue "
                                     "(test output, build log line, or serial log line).")
            i["history"].append({"state": i["state"], "at": now_iso()})
            i["state"] = "verified"
            on = (self.state(slug).get("on_board") or {}).get("version")
            i["closed"] = {"by": ACTOR_NAMES.get(actor, actor), "verified_by": actor, "at": now_iso(),
                           "evidence": evidence.strip(), "note": note.strip(), "on_version": on}
            write_json(path, i)
            self._log(slug, i["version"], f"{iid} verified fixed by {i['closed']['by']}"
                      + (f" (on {on})" if on else "") + (f". Evidence: {evidence.strip()[:300]}" if evidence else ""))
            self._commit(slug, f"{iid}: verified by {i['closed']['by']}")
        return i

    def reopen_issue(self, slug, iid, actor: str, note: str = "") -> dict:
        with _lock:
            path = self._issue_path(slug, iid)
            i = read_json(path)
            i["history"].append({"state": i["state"], "at": now_iso(), "closed": i.get("closed")})
            i["state"] = "reopened"
            i["closed"] = None
            write_json(path, i)
            self._log(slug, i["version"], f"{iid} reopened by {ACTOR_NAMES.get(actor, actor)}. {note}")
            self._commit(slug, f"{iid}: reopened")
        return i

    # ------------------------------------------------------------- diff
    def _source_dir(self, slug, vid) -> Path:
        return self.pdir(slug) / "work" if vid == "work" else self.vdir(slug, vid) / "source"

    def diff(self, slug, a: str, b: str, context: int = 3) -> dict:
        da, db = self._source_dir(slug, a), self._source_dir(slug, b)
        fa = {f.relative_to(da).as_posix(): f for f in iter_tree(da)}
        fb = {f.relative_to(db).as_posix(): f for f in iter_tree(db)}
        files = []
        for rel in sorted(set(fa) | set(fb)):
            ta = fa[rel].read_text(errors="replace").splitlines() if rel in fa else []
            tb = fb[rel].read_text(errors="replace").splitlines() if rel in fb else []
            if ta == tb:
                continue
            status = "added" if rel not in fa else "removed" if rel not in fb else "changed"
            lines = list(difflib.unified_diff(ta, tb, f"{a}/{rel}", f"{b}/{rel}", n=context, lineterm=""))
            files.append({"file": rel, "status": status, "diff": "\n".join(lines)})
        libs = None
        if a != "work" and b != "work":
            ma = read_json(self.vdir(slug, a) / "manifest.json")
            mb = read_json(self.vdir(slug, b) / "manifest.json")
            libs = self._lib_changes(ma, mb)
        return {"from": a, "to": b, "files": files, "libraries": libs}

    # ------------------------------------------------------------- serial
    def serial_capture(self, slug, seconds: float = 5, port: str | None = None, baud: int | None = None) -> dict:
        """Read the board's serial output; saved with the on-board version as evidence."""
        import serial
        p, _, profile = self._backend_and_profile(slug)
        vid = (self.state(slug).get("on_board") or {}).get("version")
        if not vid:
            raise VaultError("Nothing is on the board yet")
        port = pick_port(profile, port)
        baud = baud or profile.get("serial_baud", 115200)
        # If a terminal on the vault page has the port open, read through it
        # (no reset, sees the board as it runs). Otherwise open the port.
        text = serialmon.tap(port, seconds)
        how = "from the open terminal"
        if text is None:
            how = "port opened (board reset)"
            data = b""
            with serial.Serial(port, baud, timeout=0.2) as s:
                end = time.time() + seconds
                while time.time() < end:
                    data += s.read(4096)
            text = data.decode(errors="replace")
        name = time.strftime("%Y%m%d-%H%M%S") + ".log"
        with _lock:
            f = self.vdir(slug, vid) / "serial" / name
            f.parent.mkdir(exist_ok=True)
            f.write_text(text)
            self._log(slug, vid, f"Captured {seconds}s of serial on {port}, {how} -> serial/{name}")
            self._commit(slug, f"{vid}: serial capture")
        return {"version": vid, "file": f"serial/{name}", "text": text}

    # ------------------------------------------------------------- summary
    def _write_summary(self, slug):
        p = self.project_file(slug)
        vids = self.version_ids(slug)
        good = self.latest_good(slug)
        on = (self.state(slug).get("on_board") or {}).get("version")
        L = [f"# {p['name']}", "", f"Board profile: {p['profile']} · backend: {p['backend']}", ""]
        if good:
            m = read_json(self.vdir(slug, good) / "manifest.json")
            L += [f"**Latest known good: {good}** - {self.status(slug, good)['reason']}",
                  f"Binary sha256 `{m['binary']['sha256']}`", ""]
        else:
            L += ["No known-good version yet.", ""]
        L += [f"On the board now: {on or 'nothing recorded'}", "", "| Version | Status | Reason | Open issues |", "|---|---|---|---|"]
        for v in vids:
            s = self.status(slug, v)
            n = len(self.open_issues(slug, v))
            L.append(f"| {v} | {STATUSES[s['status']]} | {s.get('reason', '')} | {n} |")
        (self.root / slug / "SUMMARY.md").write_text("\n".join(L) + "\n")
