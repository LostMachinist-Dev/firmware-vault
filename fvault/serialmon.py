"""Live serial terminals, held by the web service.

A Monitor owns one USB serial port: a thread reads it into a byte buffer
that any number of browser tabs (and Claude Code, through the service) read
from by position. Several monitors can run at once, one per port.

Flashing and serial captures need the port, so the hub can pause a monitor
(close the port), and resume it afterwards. `released(port)` does that from
any process: inside the web service it calls the hub directly, elsewhere
(CLI, MCP) it asks the service over HTTP on localhost, and if the service is
not running there is nothing to release.
"""
import base64
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager

BUF_MAX = 512 * 1024          # bytes kept per port
TAIL_ON_ATTACH = 16 * 1024    # what a new viewer gets first
IDLE_CLOSE_S = 15 * 60        # close a monitor nobody has read for this long
BAUDS = [300, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 74880, 115200, 230400, 250000, 460800, 500000, 921600, 1000000, 2000000]


class Monitor:
    def __init__(self, port: str, baud: int):
        self.port, self.baud = port, int(baud)
        self.buf = bytearray()
        self.base = 0                  # stream position of buf[0]
        self.events: list[dict] = []   # {pos, kind, text} shown inline by viewers
        self.state = "opening"         # opening | open | paused | lost | closed
        self.error = None
        self.rx = self.tx = 0
        self.opened = time.time()
        self.last_read = time.time()
        self._pause = 0
        self._ser = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"serial {port}")
        self._thread.start()

    # ---------------------------------------------------------------- stream
    @property
    def pos(self) -> int:
        return self.base + len(self.buf)

    def _append(self, data: bytes):
        with self._lock:
            self.buf += data
            self.rx += len(data)
            if len(self.buf) > BUF_MAX:
                cut = len(self.buf) - BUF_MAX
                del self.buf[:cut]
                self.base += cut
                self.events = [e for e in self.events if e["pos"] >= self.base]

    def event(self, text: str, kind: str = "info"):
        with self._lock:
            self.events.append({"pos": self.pos, "kind": kind, "text": text, "at": time.time()})
            self.events = self.events[-200:]

    def read(self, since: int | None) -> dict:
        with self._lock:
            self.last_read = time.time()
            if since is None or since < 0:
                since = max(self.base, self.pos - TAIL_ON_ATTACH)
            dropped = since < self.base
            since = max(since, self.base)
            data = bytes(self.buf[since - self.base:])
            return {"port": self.port, "baud": self.baud, "state": self.state, "error": self.error,
                    "from": since, "pos": self.pos, "dropped": dropped, "rx": self.rx, "tx": self.tx,
                    "data": base64.b64encode(data).decode(),
                    "events": [e for e in self.events if e["pos"] >= since]}

    def text_since(self, since: int) -> str:
        with self._lock:
            return bytes(self.buf[max(since, self.base) - self.base:]).decode(errors="replace")

    # ---------------------------------------------------------------- port
    def _open(self):
        import serial
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = self.port, self.baud, 0.1
        # Both lines released: ESP32 boards run instead of sitting in reset or
        # in the bootloader. (An Arduino still resets once as the port opens.)
        s.dtr = False
        s.rts = False
        s.exclusive = True
        s.open()
        return s

    def _run(self):
        import serial
        announced_lost = False
        while not self._stop.is_set():
            if self._pause:
                time.sleep(0.05)
                continue
            if self._ser is None:
                try:
                    ser = self._open()
                    with self._lock:
                        if self._pause or self._stop.is_set():
                            ser.close()
                            continue
                        self._ser = ser
                        was = self.state
                        self.state, self.error = "open", None
                    if was in ("lost", "paused"):
                        self.event(f"port back at {self.baud} baud", "ok")
                    announced_lost = False
                except (serial.SerialException, OSError) as e:
                    with self._lock:
                        self.state, self.error = "lost", str(e)
                    if not announced_lost:
                        self.event(f"cannot open {self.port}: {e}. Retrying every second.", "err")
                        announced_lost = True
                    self._stop.wait(1.0)
                    continue
            try:
                ser = self._ser
                if ser is None:
                    continue
                data = ser.read(ser.in_waiting or 1)
                if data:
                    self._append(data)
            except (serial.SerialException, OSError, TypeError, AttributeError) as e:
                with self._lock:
                    if self._pause or self._ser is None:
                        continue   # closed on purpose by pause()
                    self._close_port()
                    self.state, self.error = "lost", str(e)
                self.event("port lost (unplugged or reset). Reconnecting when it comes back.", "err")
                announced_lost = True
        with self._lock:
            self._close_port()
            self.state = "closed"

    def _close_port(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def write(self, data: bytes) -> int:
        with self._lock:
            if self._ser is None or self.state != "open":
                raise RuntimeError(f"{self.port} is {self.state}, not open")
            n = self._ser.write(data)
            self.tx += n
            return n

    def reset_board(self):
        """Pulse the reset lines: RTS (ESP32 EN), then DTR (Arduino auto-reset)."""
        with self._lock:
            s = self._ser
            if s is None:
                raise RuntimeError(f"{self.port} is {self.state}, not open")
            s.dtr = False
            s.rts = True
            time.sleep(0.1)
            s.rts = False
            s.dtr = True
            time.sleep(0.1)
            s.dtr = False
        self.event("reset pulse sent (RTS, then DTR)", "info")

    def set_baud(self, baud: int):
        with self._lock:
            self.baud = int(baud)
            if self._ser is not None:
                self._ser.baudrate = self.baud
        self.event(f"baud changed to {self.baud}", "info")

    def pause(self, why: str):
        with self._lock:
            self._pause += 1
            if self._pause == 1:
                self._close_port()
                self.state = "paused"
                self.event(f"port released: {why}", "warn")

    def resume(self):
        with self._lock:
            self._pause = max(0, self._pause - 1)
            # the reader thread reopens it (and retries while a board re-enumerates)

    def close(self):
        self._stop.set()
        with self._lock:
            self._close_port()
            self.state = "closed"

    def info(self) -> dict:
        return {"port": self.port, "baud": self.baud, "state": self.state, "error": self.error,
                "rx": self.rx, "tx": self.tx, "opened": self.opened}


class Hub:
    def __init__(self):
        self.monitors: dict[str, Monitor] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._reaper, daemon=True, name="serial reaper").start()

    def open(self, port: str, baud: int) -> Monitor:
        with self._lock:
            m = self.monitors.get(port)
            if m and m.state != "closed":
                if m.baud != int(baud):
                    m.set_baud(baud)
                return m
            m = Monitor(port, baud)
            self.monitors[port] = m
            return m

    def get(self, port: str) -> Monitor | None:
        m = self.monitors.get(port)
        return m if m and m.state != "closed" else None

    def require(self, port: str) -> Monitor:
        m = self.get(port)
        if not m:
            raise KeyError(f"No terminal is open on {port}")
        return m

    def close(self, port: str):
        with self._lock:
            m = self.monitors.pop(port, None)
        if m:
            m.close()

    def list(self) -> list[dict]:
        return [m.info() for m in self.monitors.values() if m.state != "closed"]

    def pause(self, port: str, why: str) -> bool:
        m = self.get(port)
        if not m:
            return False
        m.pause(why)
        # give the reader thread a moment to let go of the file descriptor
        time.sleep(0.15)
        return True

    def resume(self, port: str):
        m = self.get(port)
        if m:
            m.resume()

    def tap(self, port: str, seconds: float) -> str | None:
        """Text that arrives on an open terminal during the next `seconds`."""
        m = self.get(port)
        if not m or m.state not in ("open", "paused"):
            return None
        start = m.pos
        time.sleep(seconds)
        m.last_read = time.time()
        return m.text_since(start)

    def _reaper(self):
        while True:
            time.sleep(30)
            now = time.time()
            for port, m in list(self.monitors.items()):
                if m.state == "closed" or now - m.last_read > IDLE_CLOSE_S:
                    self.close(port)


# ------------------------------------------------------------- cross-process
HUB: Hub | None = None   # set by the web service


def _service_url() -> str:
    return os.environ.get("FV_SERVICE", "http://127.0.0.1:8200")


def _post(path: str, body: dict, timeout: float = 3.0):
    req = urllib.request.Request(_service_url() + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


@contextmanager
def released(port: str | None, why: str = "flashing"):
    """Free `port` from any open terminal for the duration of the block."""
    if not port:
        yield
        return
    if HUB is not None:
        paused = HUB.pause(port, why)
        try:
            yield
        finally:
            if paused:
                HUB.resume(port)
        return
    paused = False
    try:
        paused = bool(_post("/api/serial/pause", {"port": port, "why": why}).get("paused"))
    except Exception:
        paused = False   # no service running, nothing holds the port
    try:
        yield
    finally:
        if paused:
            try:
                _post("/api/serial/resume", {"port": port})
            except Exception:
                pass


def tap(port: str, seconds: float) -> str | None:
    """Read from an open terminal instead of opening the port (no board reset)."""
    if HUB is not None:
        return HUB.tap(port, seconds)
    try:
        r = _post("/api/serial/tap", {"port": port, "seconds": seconds}, timeout=seconds + 5)
        return r.get("text")
    except Exception:
        return None


def monitor_text(port: str | None = None, max_bytes: int = 8000) -> dict:
    """What the open terminals show right now (for Claude Code)."""
    try:
        with urllib.request.urlopen(_service_url() + "/api/serial/monitors", timeout=3) as r:
            mons = json.loads(r.read())
    except Exception as e:
        return {"error": f"The vault web service is not reachable ({e})", "terminals": []}
    out = []
    for m in mons:
        if port and m["port"] != port:
            continue
        try:
            q = urllib.parse.urlencode({"port": m["port"], "since": -1})
            with urllib.request.urlopen(_service_url() + "/api/serial/read?" + q, timeout=3) as r:
                d = json.loads(r.read())
            text = base64.b64decode(d["data"])[-max_bytes:].decode(errors="replace")
            out.append({**m, "text": text})
        except Exception as e:
            out.append({**m, "error": str(e)})
    return {"terminals": out}

