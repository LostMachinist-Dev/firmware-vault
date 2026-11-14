"""The web page and its JSON API. Everything done here is done by the human."""
import base64
import socket
import json
import mimetypes
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import serialmon
from .paths import WEB_DIR
from .profiles import list_profiles
from .store import STATUSES, Vault
from .util import VaultError, now_iso

ACTOR = "human"
JOBS: dict[str, dict] = {}


def _job(fn):
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "lines": [], "done": False, "ok": None, "result": None, "error": None, "started": now_iso()}
    JOBS[jid] = job

    def run():
        try:
            job["result"] = fn(job["lines"].append)
            job["ok"] = True
        except VaultError as e:
            job["error"], job["ok"] = str(e), False
        except Exception as e:
            job["error"], job["ok"] = f"{type(e).__name__}: {e}", False
        job["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return job


class Handler(BaseHTTPRequestHandler):
    vault: Vault = None
    server_version = "FirmwareVault"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else (json.dumps(body, default=str) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        u = urlparse(self.path)
        parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if not parts or parts[0] != "api":
                return self._static(u.path)
            r = self._api(method, parts[1:], q)
            if isinstance(r, tuple):
                return self._send(200, r[1], r[0], r[2] if len(r) > 2 else None)
            return self._send(200, r)
        except VaultError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _static(self, path):
        f = (WEB_DIR / (path.strip("/") or "index.html")).resolve()
        if WEB_DIR.resolve() not in f.parents and f != WEB_DIR.resolve() or not f.is_file():
            f = WEB_DIR / "index.html"
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        return self._send(200, f.read_bytes(), ctype)

    def _api(self, method, p, q):
        v = self.vault
        b = self._body() if method == "POST" else {}
        if p == ["meta"]:
            return {"statuses": STATUSES, "profiles": list_profiles(), "ports": v.ports(), "bauds": serialmon.BAUDS,
                    "host": socket.gethostname()}
        if p[:1] == ["serial"]:
            return self._serial(method, p[1:], q, b)
        if p == ["projects"]:
            if method == "POST":
                return v.create_project(b["name"], b["profile"], b.get("hardware_notes", ""))
            return v.list_projects()
        if p == ["notes"] and method == "POST":
            # For the voice notepad: no project needed, the note goes to the
            # project whose debug session is live, on the version on its board.
            live = [x for x in v.list_projects() if x["debug_active"]]
            if len(live) != 1:
                raise VaultError("Start debug notes on exactly one project first" if not live
                                 else "More than one project is listening; stop the others")
            return v.add_issue(live[0]["slug"], b.get("text", ""), ACTOR, b.get("source", "voice"), "human")
        if p[:1] == ["jobs"] and len(p) == 2:
            job = JOBS.get(p[1])
            if not job:
                raise VaultError("No such job")
            return job
        if p[:1] != ["projects"] or len(p) < 2:
            raise VaultError("Not found")
        slug, rest = p[1], p[2:]
        if rest == []:
            return {"project": v.project_summary(slug), "versions": v.list_versions(slug),
                    "state": v.state(slug), "work_dir": str(v.pdir(slug) / "work"),
                    "open_issues": v.open_issues(slug)}
        if rest == ["notes"] and method == "POST":
            # Also the hook for the voice notepad: POST {"text": "...", "source": "voice"}
            return v.add_issue(slug, b.get("text", ""), ACTOR, b.get("source", "typed"), "human")
        if rest == ["debug"] and method == "POST":
            return v.set_debug(slug, bool(b.get("active")))
        if rest == ["diff"]:
            return v.diff(slug, q["a"], q["b"])
        if rest == ["pins"]:
            return v.pin_map(slug)
        if rest == ["settings"] and method == "POST":
            return v.update_project(slug, hardware_notes=b.get("hardware_notes"), pcb=b.get("pcb") or None)
        if rest[:1] == ["issues"] and len(rest) == 3 and method == "POST":
            iid, act = rest[1], rest[2]
            if act == "close":
                return v.close_issue(slug, iid, ACTOR, b.get("evidence", ""), b.get("note", ""))
            if act == "reopen":
                return v.reopen_issue(slug, iid, ACTOR, b.get("note", ""))
        if rest[:1] == ["versions"] and len(rest) >= 2:
            vid, act = rest[1], rest[2:]
            if act == []:
                return v.get_version(slug, vid)
            if act == ["status"] and method == "POST":
                return v.set_status(slug, vid, b["status"], b.get("reason", ""), ACTOR)
            if act == ["flash"] and method == "POST":
                return _job(lambda log: v.flash(slug, vid, ACTOR, b.get("port") or None,
                                                force=bool(b.get("force")), log=log))
            if act == ["on-board"] and method == "POST":
                return v.mark_on_board(slug, vid, ACTOR, b.get("where", "laptop"))
            if act == ["kit"]:
                name, data = v.flash_kit(slug, vid)
                return ("application/zip", data, {"Content-Disposition": f'attachment; filename="{name}"'})
            if len(act) >= 2 and act[0] == "file":
                d = v.vdir(slug, vid)
                f = (d / "/".join(act[1:])).resolve()
                if d.resolve() not in f.parents or not f.is_file():
                    raise VaultError("No such file")
                return ("text/plain; charset=utf-8", f.read_bytes())
        raise VaultError("Not found")


    def _serial(self, method, p, q, b):
        hub = serialmon.HUB
        port = b.get("port") or q.get("port")
        try:
            if p == ["ports"]:
                return ports_view(self.vault, hub)
            if p == ["monitors"]:
                return hub.list()
            if p == ["read"]:
                m = hub.get(port)
                if not m:
                    return {"port": port, "state": "closed", "data": "", "events": [], "pos": 0, "from": 0}
                return m.read(int(q["since"]) if q.get("since") not in (None, "") else None)
            if method != "POST":
                raise VaultError("Not found")
            if not port:
                raise VaultError("Which port?")
            if p == ["open"]:
                known = {x["port"] for x in self.vault.ports()}
                if port not in known:
                    raise VaultError(f"{port} is not a USB serial port on this machine")
                return hub.open(port, int(b.get("baud") or 115200)).info()
            if p == ["close"]:
                hub.close(port)
                return {"ok": True}
            if p == ["send"]:
                data = base64.b64decode(b["b64"]) if b.get("b64") else str(b.get("text", "")).encode()
                return {"sent": hub.require(port).write(data)}
            if p == ["baud"]:
                hub.require(port).set_baud(int(b["baud"]))
                return {"ok": True}
            if p == ["reset"]:
                hub.require(port).reset_board()
                return {"ok": True}
            if p == ["pause"]:      # used by the CLI / MCP before flashing
                return {"paused": hub.pause(port, b.get("why", "flashing"))}
            if p == ["resume"]:
                hub.resume(port)
                return {"ok": True}
            if p == ["tap"]:
                return {"text": hub.tap(port, min(float(b.get("seconds", 5)), 120))}
        except (KeyError, RuntimeError) as e:
            raise VaultError(str(e).strip("'"))
        raise VaultError("Not found")


def ports_view(v: Vault, hub) -> list[dict]:
    """USB ports with what is known about each: open terminal, which project's
    board is on it, and which board profiles match its USB id."""
    projects = v.list_projects()
    on_port = {}
    for pr in projects:
        ob = v.state(pr["slug"]).get("on_board") or {}
        if ob.get("port"):
            on_port.setdefault(ob["port"], []).append({"project": pr["slug"], "name": pr["name"], "version": ob.get("version")})
    profiles = list_profiles()
    out = []
    for x in v.ports():
        usb = (x.get("usb") or "").lower()
        boards = [pf["name"] for pf in profiles if usb in {u.lower() for u in pf.get("flash", {}).get("usb_ids", [])}]
        m = hub.get(x["port"]) if hub else None
        out.append({**x, "boards": boards, "on_board": on_port.get(x["port"], []),
                    "monitor": m.info() if m else None})
    return out


def serve(host="0.0.0.0", port=8200):
    Handler.vault = Vault()
    serialmon.HUB = serialmon.Hub()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Firmware Vault on http://{host}:{port}  (vault at {Handler.vault.root})", flush=True)
    httpd.serve_forever()
