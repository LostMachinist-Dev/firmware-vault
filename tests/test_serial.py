"""Serial terminals, tested against a pseudo-terminal instead of a real board."""
import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["FV_SERVICE"] = "http://127.0.0.1:9"   # never talk to the live service

from fvault import serialmon


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class SerialHubTest(unittest.TestCase):
    def setUp(self):
        self.master, slave = os.openpty()
        self.port = os.ttyname(slave)
        os.close(slave)
        self.hub = serialmon.Hub()

    def tearDown(self):
        for p in list(self.hub.monitors):
            self.hub.close(p)
        serialmon.HUB = None
        os.close(self.master)

    def test_read_write_and_positions(self):
        m = self.hub.open(self.port, 115200)
        self.assertTrue(wait_for(lambda: m.state == "open"), m.error)
        os.write(self.master, b"hello\r\n")
        self.assertTrue(wait_for(lambda: m.pos >= 7))
        r = m.read(0)
        self.assertEqual(r["pos"], 7)
        self.assertIn("aGVsbG8", r["data"])          # base64 of "hello"
        self.assertEqual(m.read(r["pos"])["data"], "")
        m.write(b"ping\n")
        self.assertEqual(os.read(self.master, 100), b"ping\n")
        self.assertEqual(m.tx, 5)

    def test_same_port_reuses_the_monitor_and_changes_baud(self):
        a = self.hub.open(self.port, 115200)
        b = self.hub.open(self.port, 9600)
        self.assertIs(a, b)
        self.assertEqual(a.baud, 9600)

    def test_buffer_is_bounded(self):
        m = serialmon.Monitor.__new__(serialmon.Monitor)
        m.buf, m.base, m.events, m.rx, m._lock = bytearray(), 0, [], 0, threading.RLock()
        m.last_read = time.time()
        m.state, m.port, m.baud, m.error, m.tx = "open", "x", 1, None, 0
        m._append(b"a" * (serialmon.BUF_MAX + 10))
        self.assertEqual(len(m.buf), serialmon.BUF_MAX)
        self.assertEqual(m.base, 10)
        self.assertTrue(m.read(0)["dropped"])

    def test_released_pauses_and_resumes(self):
        serialmon.HUB = self.hub
        m = self.hub.open(self.port, 115200)
        self.assertTrue(wait_for(lambda: m.state == "open"))
        with serialmon.released(self.port, "flashing test"):
            self.assertEqual(m.state, "paused")
            self.assertIsNone(m._ser)
        self.assertTrue(wait_for(lambda: m.state == "open"))
        kinds = [e["text"] for e in m.events]
        self.assertTrue(any("flashing test" in t for t in kinds))
        self.assertTrue(any("back" in t for t in kinds))

    def test_released_without_service_is_a_no_op(self):
        serialmon.HUB = None
        with serialmon.released("/dev/ttyNOPE"):
            pass
        self.assertIsNone(serialmon.tap("/dev/ttyNOPE", 0.1))

    def test_tap_reads_from_open_terminal(self):
        serialmon.HUB = self.hub
        m = self.hub.open(self.port, 115200)
        self.assertTrue(wait_for(lambda: m.state == "open"))
        threading.Timer(0.1, lambda: os.write(self.master, b"heartbeat 7\n")).start()
        self.assertEqual(serialmon.tap(self.port, 0.4), "heartbeat 7\n")
        self.assertIsNone(serialmon.tap("/dev/ttyNOPE", 0.1))

    def test_lost_port_reconnects(self):
        m = self.hub.open("/dev/ttyDOESNOTEXIST", 115200)
        self.assertTrue(wait_for(lambda: m.state == "lost"))
        self.assertTrue(any("cannot open" in e["text"] for e in m.events))


if __name__ == "__main__":
    unittest.main()
