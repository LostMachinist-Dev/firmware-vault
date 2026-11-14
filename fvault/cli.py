"""fv - the Firmware Vault command line. Acts as Claude Code (not the human):
it cannot mark a version known good, caveats or do-not-deploy, and cannot
close hardware issues. Those are done on the web page.
"""
import argparse
import json
import sys

from .profiles import list_profiles
from .store import STATUSES, Vault
from .util import VaultError

ACTOR = "claude"


def _print(obj):
    print(json.dumps(obj, indent=2) if not isinstance(obj, str) else obj)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fv", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("projects", help="list projects")
    sub.add_parser("profiles", help="list board profiles")
    sub.add_parser("ports", help="list USB serial ports on this machine")
    a = sub.add_parser("new", help="create a project");
    a.add_argument("name"); a.add_argument("profile"); a.add_argument("--notes", default="")
    a.add_argument("--backend", choices=["platformio", "arduino-cli"], help="default: the profile's")
    a = sub.add_parser("set-backend", help="switch a project with no versions to another build tool")
    a.add_argument("project"); a.add_argument("backend", choices=["platformio", "arduino-cli"])
    a = sub.add_parser("show", help="project details and versions"); a.add_argument("project")
    a = sub.add_parser("version", help="one version: manifest, status, story"); a.add_argument("project"); a.add_argument("version")
    a = sub.add_parser("build", help="build the working source (no version made)"); a.add_argument("project")
    a = sub.add_parser("pin", help="rewrite floating versions to exact ones"); a.add_argument("project")
    a = sub.add_parser("snapshot", help="build and seal the working source as a new version")
    a.add_argument("project"); a.add_argument("note")
    a.add_argument("--summary", default="", help="plain-language summary")
    a.add_argument("--summary-json", help="JSON: {text, added:[{name,why}], ported:[...], not_used:[...]}")
    a.add_argument("--fixes", nargs="*", default=[], help="issue ids this version tries to fix")
    a.add_argument("--hw-rev")
    a = sub.add_parser("status", help="set draft/testing (human sets the rest on the web page)")
    a.add_argument("project"); a.add_argument("version"); a.add_argument("status", choices=list(STATUSES)); a.add_argument("reason")
    a = sub.add_parser("flash", help="flash a stored version"); a.add_argument("project"); a.add_argument("version")
    a.add_argument("--port"); a.add_argument("--dry-run", action="store_true")
    a = sub.add_parser("issues", help="open issues"); a.add_argument("project"); a.add_argument("--version"); a.add_argument("--all", action="store_true")
    a = sub.add_parser("note", help="add an issue to the on-board version"); a.add_argument("project"); a.add_argument("text")
    a.add_argument("--version", help="attach to this version instead of the one on the board")
    a.add_argument("--checkable", action="store_true", help="Claude can verify it (build, tests, serial)")
    a = sub.add_parser("fix-attempted"); a.add_argument("project"); a.add_argument("issue"); a.add_argument("version"); a.add_argument("note")
    a = sub.add_parser("close", help="close a Claude-checkable issue with evidence"); a.add_argument("project"); a.add_argument("issue"); a.add_argument("evidence")
    a = sub.add_parser("diff"); a.add_argument("project"); a.add_argument("a"); a.add_argument("b", help="version id or 'work'")
    a = sub.add_parser("story", help="append to a version's build story"); a.add_argument("project"); a.add_argument("version"); a.add_argument("text")
    a = sub.add_parser("serial", help="capture serial output as evidence"); a.add_argument("project")
    a.add_argument("--seconds", type=float, default=5); a.add_argument("--port"); a.add_argument("--baud", type=int)
    a = sub.add_parser("hw-notes", help="replace the project's hardware notes"); a.add_argument("project"); a.add_argument("notes")
    a = sub.add_parser("link-pcb", help="link a folder holding an EAGLE/Fusion schematic (.sch)"); a.add_argument("project"); a.add_argument("path")
    a.add_argument("--revision", default=""); a.add_argument("--part")
    a = sub.add_parser("pins", help="pin map from the linked schematic"); a.add_argument("project")
    a = sub.add_parser("serve", help="run the web page"); a.add_argument("--port", type=int, default=8200); a.add_argument("--host", default="0.0.0.0")
    a = sub.add_parser("terminals", help="what the vault page's serial terminals show"); a.add_argument("--port")
    sub.add_parser("mcp", help="run the MCP server on stdio")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        from .web import serve
        return serve(args.host, args.port)
    if args.cmd == "mcp":
        from .mcp import main as mcp_main
        return mcp_main()

    v = Vault()
    try:
        c = args.cmd
        if c == "projects":
            for p in v.list_projects():
                print(f"{p['slug']:<24} {p['board']:<36} versions {p['versions']:<3} known good {p['latest_good'] or '-':<5} on board {p['on_board'] or '-'}")
        elif c == "profiles":
            for p in list_profiles():
                print(f"{p['id']:<20} {p['name']:<48} {p['flash']['method']}{'' if p.get('tested_on_hardware') else '  (not yet tried on hardware)'}")
        elif c == "ports":
            _print(v.ports())
        elif c == "new":
            _print(v.create_project(args.name, args.profile, args.notes, backend=args.backend))
        elif c == "set-backend":
            _print(v.set_backend(args.project, args.backend))
        elif c == "show":
            _print({"project": v.project_summary(args.project), "work_dir": str(v.pdir(args.project) / "work"),
                    "versions": v.list_versions(args.project)})
        elif c == "version":
            _print(v.get_version(args.project, args.version))
        elif c == "build":
            r = v.build(args.project)
            print(r["log_tail"][-1500:])
            _print({k: r[k] for k in ("ok", "warnings", "memory", "unpinned")})
            return 0 if r["ok"] else 1
        elif c == "pin":
            ch = v.pin(args.project)
            print("\n".join(ch) if ch else "Everything was already pinned.")
        elif c == "snapshot":
            summary = json.loads(args.summary_json) if args.summary_json else {"text": args.summary}
            r = v.create_version(args.project, args.note, summary, ACTOR, args.hw_rev, args.fixes)
            print(f"Created {r['id']} ({r['status_label']}), binary sha256 {r['manifest']['binary']['sha256']}")
        elif c == "status":
            _print(v.set_status(args.project, args.version, args.status, args.reason, ACTOR))
        elif c == "flash":
            _print(v.flash(args.project, args.version, ACTOR, args.port, dry_run=args.dry_run, log=print))
        elif c == "issues":
            lst = v.issues(args.project, args.version) if args.all else v.open_issues(args.project, args.version)
            for i in lst:
                print(f"{i['id']} [{i['state']}] ({i['version']}, check by {i['verify']}) {i['text']}")
        elif c == "note":
            _print(v.add_issue(args.project, args.text, ACTOR, "claude", "claude" if args.checkable else "human", args.version))
        elif c == "hw-notes":
            _print(v.update_project(args.project, hardware_notes=args.notes))
        elif c == "fix-attempted":
            _print(v.mark_fix_attempted(args.project, args.issue, args.version, args.note, ACTOR))
        elif c == "close":
            _print(v.close_issue(args.project, args.issue, ACTOR, args.evidence))
        elif c == "diff":
            d = v.diff(args.project, args.a, args.b)
            if d["libraries"]:
                _print(d["libraries"])
            for f in d["files"]:
                print(f["diff"])
        elif c == "story":
            v.append_story(args.project, args.version, args.text, ACTOR)
        elif c == "serial":
            r = v.serial_capture(args.project, args.seconds, args.port, args.baud)
            print(r["text"]); print(f"(saved to {r['version']}/{r['file']})")
        elif c == "link-pcb":
            _print(v.update_project(args.project, pcb={"path": args.path, "revision": args.revision, "part": args.part}))
        elif c == "terminals":
            from .serialmon import monitor_text
            r = monitor_text(args.port)
            if r.get("error"):
                print(r["error"])
            for t in r["terminals"]:
                print(f"=== {t['port']} @ {t['baud']} ({t['state']}) ===")
                print(t.get("text", t.get("error", "")))
        elif c == "pins":
            _print(v.pin_map(args.project))
    except VaultError as e:
        print(f"fv: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
