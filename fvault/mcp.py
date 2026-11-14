"""MCP server (stdio, JSON-RPC 2.0). Claude Code works the vault through this.

Everything here acts as Claude Code. The store enforces the rules: Claude may
set draft or testing, may close only issues it can check itself and must give
evidence, and cannot flash a do-not-deploy version.
"""
import json
import sys
import traceback

from . import __version__, serialmon
from .profiles import list_profiles
from .store import Vault
from .util import VaultError

ACTOR = "claude"
PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = """Firmware Vault keeps every build of a microcontroller project.
Loop: list_open_issues -> edit code in the project's work/ folder (path from get_project) ->
build -> pin_versions if anything floats -> create_version with a plain-language summary
(libraries added and why, what was ported or written from scratch, what was deliberately
not used) and the issue ids it tries to fix -> flash_version -> ask the human to test.
Only the human marks known good / caveats / do not deploy. Close an issue yourself only
when you can check it (build, tests, serial output) and always pass the evidence.
Raise a choice to the human when there is a real tradeoff or you want to port logic
instead of using a library. Read the pin map from the linked PCB (get_pin_map) instead
of guessing pins."""

S = lambda **props: {"type": "object", "properties": props,
                     "required": [k for k, v in props.items() if not v.pop("optional", False)]}
STR = lambda d, optional=False: {"type": "string", "description": d, "optional": optional}

TOOLS = [
    ("list_projects", "List vault projects with latest known-good and on-board versions.", S()),
    ("get_project", "Project details, its work/ source folder path, and its version timeline.", S(project=STR("project slug"))),
    ("list_versions", "Versions newest first with status, reason and open issue counts.", S(project=STR("project slug"))),
    ("get_version", "One version: manifest (exact versions, hashes, flash plan), status, build story, debug log, wiring, issues.",
     S(project=STR("project slug"), version=STR("e.g. v3"))),
    ("list_profiles", "Board profiles available for new projects.", S()),
    ("create_project", "Create a project from a board profile with a pinned starting source. backend: 'platformio' or 'arduino-cli' (default: the profile's). Use arduino-cli for Arduino sketches whose core version matters.",
     S(name=STR("human name"), profile=STR("profile id"), hardware_notes=STR("wiring/hardware notes", True),
       backend=STR("platformio or arduino-cli", True))),
    ("set_backend", "Switch a project with no versions yet to another build tool (platformio or arduino-cli). Moves a PlatformIO layout (src/*.ino, lib/) into a sketch folder and writes sketch.yaml.",
     S(project=STR("project slug"), backend=STR("platformio or arduino-cli"))),
    ("build", "Build the project's working source without making a version. Reports warnings, memory and anything unpinned.",
     S(project=STR("project slug"))),
    ("pin_versions", "Rewrite floating platform, framework, toolchain and library versions to the exact installed ones.",
     S(project=STR("project slug"))),
    ("create_version", "Build the working source and seal it as the next version (binary, source snapshot, manifest, build story).",
     {"type": "object", "required": ["project", "note"], "properties": {
         "project": {"type": "string"}, "note": {"type": "string", "description": "what changed from the previous version"},
         "summary": {"type": "string", "description": "plain-language summary of the build"},
         "libraries_added": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "why": {"type": "string"}}}},
         "ported": {"type": "array", "items": {"type": "string"}, "description": "logic ported or written from scratch"},
         "not_used": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "why": {"type": "string"}}}},
         "fixes": {"type": "array", "items": {"type": "string"}, "description": "issue ids this version tries to fix"},
         "hardware_revision": {"type": "string"}}}),
    ("flash_version", "Flash a stored version to the board on the vault host's USB. Verifies hashes first. Refuses do-not-deploy versions.",
     {"type": "object", "required": ["project", "version"], "properties": {
         "project": {"type": "string"}, "version": {"type": "string"}, "port": {"type": "string"},
         "dry_run": {"type": "boolean", "description": "only show the command"}}}),
    ("set_version_status", "Set a version's status with a one-line reason. Claude Code may only set draft or testing; known good, caveats and do not deploy are human only.",
     S(project=STR("project slug"), version=STR("version"), status=STR("draft or testing"), reason=STR("one-line reason"))),
    ("list_open_issues", "Open issues (open, fix attempted, reopened), for one version or the whole project.",
     S(project=STR("project slug"), version=STR("version, omit for all", True))),
    ("add_issue", "Add an issue to the version on the board (or to `version`, e.g. a known bug in a build just imported). Set checkable=true only if you can verify it yourself.",
     {"type": "object", "required": ["project", "text"], "properties": {
         "project": {"type": "string"}, "text": {"type": "string"}, "checkable": {"type": "boolean"},
         "version": {"type": "string", "description": "attach to this version instead of the one on the board"}}}),
    ("mark_fix_attempted", "Link an issue to the version that tries to fix it.",
     S(project=STR("project slug"), issue=STR("e.g. I-004"), version=STR("version with the fix"), note=STR("what was changed"))),
    ("close_issue", "Close an issue you verified yourself. Evidence is required (test output, build log line, serial line). Hardware issues are human only.",
     S(project=STR("project slug"), issue=STR("issue id"), evidence=STR("the evidence"), note=STR("optional note", True))),
    ("diff_versions", "Source and library diff between two versions. Use 'work' as b to compare against the working source.",
     S(project=STR("project slug"), a=STR("older version"), b=STR("newer version or 'work'"))),
    ("append_build_story", "Append a paragraph to a version's build story (study-guide style: what and why).",
     S(project=STR("project slug"), version=STR("version"), text=STR("markdown text"))),
    ("serial_capture", "Read the board's serial output for a few seconds; saved with the on-board version as evidence.",
     {"type": "object", "required": ["project"], "properties": {
         "project": {"type": "string"}, "seconds": {"type": "number"}, "port": {"type": "string"}, "baud": {"type": "integer"}}}),
    ("get_pin_map", "Real pin-to-net map of the host board, read from the linked EAGLE/Fusion schematic.", S(project=STR("project slug"))),
    ("set_hardware_notes", "Replace the project's hardware notes (board, wiring, modules, addresses). New versions copy them into wiring.md and the manifest.",
     S(project=STR("project slug"), notes=STR("the full notes, plain text"))),
    ("link_pcb", "Link a folder holding an EAGLE/Fusion schematic (and board revision) to this project.",
     S(project=STR("project slug"), path=STR("pcb project folder"), revision=STR("board revision", True), part=STR("host part ref, e.g. M1", True))),
    ("list_ports", "USB serial ports on the vault host.", S()),
    ("read_terminals", "What the serial terminals open on the vault page are showing right now (the human may be watching several ports). Read-only; does not reset the boards.",
     {"type": "object", "properties": {"port": {"type": "string", "description": "only this port"},
                                       "max_bytes": {"type": "integer", "description": "tail size per terminal, default 8000"}}}),
]


def call(v: Vault, name: str, a: dict):
    if name == "list_projects":
        return v.list_projects()
    if name == "get_project":
        return {"project": v.project_summary(a["project"]), "work_dir": str(v.pdir(a["project"]) / "work"),
                "versions": v.list_versions(a["project"])}
    if name == "list_versions":
        return v.list_versions(a["project"])
    if name == "get_version":
        return v.get_version(a["project"], a["version"])
    if name == "list_profiles":
        return list_profiles()
    if name == "create_project":
        r = v.create_project(a["name"], a["profile"], a.get("hardware_notes", ""), backend=a.get("backend") or None)
        r["work_dir"] = str(v.pdir(r["slug"]) / "work")
        return r
    if name == "set_backend":
        return v.set_backend(a["project"], a["backend"])
    if name == "build":
        return v.build(a["project"])
    if name == "pin_versions":
        return {"changes": v.pin(a["project"])}
    if name == "create_version":
        summary = {"text": a.get("summary", ""), "added": a.get("libraries_added", []),
                   "ported": a.get("ported", []), "not_used": a.get("not_used", [])}
        r = v.create_version(a["project"], a["note"], summary, ACTOR, a.get("hardware_revision"), a.get("fixes", []))
        return {"version": r["id"], "status": r["status"], "binary": r["manifest"]["binary"],
                "changes": r["manifest"].get("changes"), "warnings": r["manifest"].get("warnings")}
    if name == "flash_version":
        log = []
        r = v.flash(a["project"], a["version"], ACTOR, a.get("port"), dry_run=a.get("dry_run", False), log=log.append)
        r["log"] = log[-40:]
        return r
    if name == "set_version_status":
        return v.set_status(a["project"], a["version"], a["status"], a["reason"], ACTOR)
    if name == "list_open_issues":
        return v.open_issues(a["project"], a.get("version"))
    if name == "add_issue":
        return v.add_issue(a["project"], a["text"], ACTOR, "claude", "claude" if a.get("checkable") else "human", a.get("version"))
    if name == "set_hardware_notes":
        return v.update_project(a["project"], hardware_notes=a["notes"])
    if name == "mark_fix_attempted":
        return v.mark_fix_attempted(a["project"], a["issue"], a["version"], a["note"], ACTOR)
    if name == "close_issue":
        return v.close_issue(a["project"], a["issue"], ACTOR, a["evidence"], a.get("note", ""))
    if name == "diff_versions":
        return v.diff(a["project"], a["a"], a["b"])
    if name == "append_build_story":
        v.append_story(a["project"], a["version"], a["text"], ACTOR)
        return {"ok": True}
    if name == "serial_capture":
        return v.serial_capture(a["project"], a.get("seconds", 5), a.get("port"), a.get("baud"))
    if name == "get_pin_map":
        return v.pin_map(a["project"])
    if name == "link_pcb":
        return v.update_project(a["project"], pcb={"path": a["path"], "revision": a.get("revision", ""), "part": a.get("part")})
    if name == "list_ports":
        return v.ports()
    if name == "read_terminals":
        return serialmon.monitor_text(a.get("port"), int(a.get("max_bytes", 8000)))
    raise VaultError(f"Unknown tool {name}")


def handle(v: Vault, msg: dict):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notification
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        result = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "firmware-vault", "version": __version__},
                  "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]}
    elif method == "tools/call":
        p = msg.get("params") or {}
        try:
            out = call(v, p.get("name"), p.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(out, indent=1, default=str)}], "isError": False}
        except VaultError as e:
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:  # report, do not crash the server
            result = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    v = Vault()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        resp = handle(v, msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
