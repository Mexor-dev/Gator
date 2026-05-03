#!/home/user/Gator/venv/bin/python3
"""Gator UDS event-bus daemon and client helpers.

Socket path: /tmp/gator_event.bus
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

BUS_PATH = Path("/tmp/gator_event.bus")


class EventBusError(RuntimeError):
    pass


class EventBusClient:
    def __init__(self, bus_path: Path = BUS_PATH, timeout: float = 2.0) -> None:
        self.bus_path = bus_path
        self.timeout = timeout

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.bus_path.exists():
            raise EventBusError(f"event bus missing: {self.bus_path}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.bus_path))
            sock.sendall((json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8"))
            data = sock.recv(65536).decode("utf-8", errors="replace").strip()
            if not data:
                return {"ok": False, "error": "empty bus response"}
            return json.loads(data)
        finally:
            sock.close()

    def publish(self, packet: dict[str, Any]) -> dict[str, Any]:
        return self._request({"op": "publish", "packet": packet})

    def doctor_query(self) -> dict[str, Any]:
        return self._request({"op": "doctor_query"})

    def interrupt(self) -> dict[str, Any]:
        return self._request({"op": "interrupt_signal"})

    def consume_interrupt(self) -> dict[str, Any]:
        return self._request({"op": "consume_interrupt"})


class EventBusDaemon:
    def __init__(self, bus_path: Path = BUS_PATH) -> None:
        self.bus_path = bus_path
        self.state: dict[str, Any] = {
            "started_ts": time.time(),
            "heartbeat_ts": 0.0,
            "last_vram": "",
            "total_packets": 0,
            "final_packets": 0,
            "interrupt_pending": False,
            "interrupt_count": 0,
            "last_packet": None,
        }
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _handle_packet(self, packet: dict[str, Any]) -> None:
        with self._lock:
            self.state["total_packets"] += 1
            self.state["last_packet"] = packet
            self.state["heartbeat_ts"] = time.time()
            if "vram" in packet:
                self.state["last_vram"] = packet.get("vram", "")
            if bool(packet.get("final", False)):
                self.state["final_packets"] += 1

    def _doctor_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "bus_path": str(self.bus_path),
                "heartbeat_ts": self.state["heartbeat_ts"],
                "last_vram": self.state["last_vram"],
                "total_packets": self.state["total_packets"],
                "final_packets": self.state["final_packets"],
                "interrupt_pending": self.state["interrupt_pending"],
                "interrupt_count": self.state["interrupt_count"],
            }

    def _consume_interrupt(self) -> dict[str, Any]:
        with self._lock:
            pending = bool(self.state["interrupt_pending"])
            self.state["interrupt_pending"] = False
            return {"ok": True, "interrupt": pending}

    def _set_interrupt(self) -> dict[str, Any]:
        with self._lock:
            self.state["interrupt_pending"] = True
            self.state["interrupt_count"] += 1
            return {"ok": True, "interrupt_pending": True, "interrupt_count": self.state["interrupt_count"]}

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            raw = conn.recv(65536).decode("utf-8", errors="replace").strip()
            if not raw:
                conn.sendall(b'{"ok":false,"error":"empty"}\n')
                return
            req = json.loads(raw.splitlines()[0])
            op = str(req.get("op", ""))
            if op == "publish":
                packet = req.get("packet") if isinstance(req.get("packet"), dict) else {}
                self._handle_packet(packet)
                conn.sendall(b'{"ok":true}\n')
                return
            if op == "doctor_query":
                conn.sendall((json.dumps(self._doctor_snapshot(), ensure_ascii=True) + "\n").encode("utf-8"))
                return
            if op == "interrupt_signal":
                conn.sendall((json.dumps(self._set_interrupt(), ensure_ascii=True) + "\n").encode("utf-8"))
                return
            if op == "consume_interrupt":
                conn.sendall((json.dumps(self._consume_interrupt(), ensure_ascii=True) + "\n").encode("utf-8"))
                return
            conn.sendall(b'{"ok":false,"error":"unknown_op"}\n')
        except Exception as exc:
            conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode("utf-8"))
        finally:
            conn.close()

    def run(self) -> None:
        if self.bus_path.exists():
            self.bus_path.unlink()

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.bus_path))
        os.chmod(self.bus_path, 0o666)
        server.listen(64)
        server.settimeout(1.0)

        try:
            while not self._stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                t = threading.Thread(target=self._handle_conn, args=(conn,), daemon=True)
                t.start()
        finally:
            server.close()
            if self.bus_path.exists():
                self.bus_path.unlink()



def _main() -> None:
    parser = argparse.ArgumentParser(description="Gator UDS event bus")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--doctor-query", action="store_true")
    parser.add_argument("--interrupt", action="store_true")
    parser.add_argument("--consume-interrupt", action="store_true")
    parser.add_argument("--publish", type=str, help="JSON packet string")
    args = parser.parse_args()

    if args.daemon:
        EventBusDaemon().run()
        return

    client = EventBusClient()
    if args.doctor_query:
        print(json.dumps(client.doctor_query(), indent=2))
        return
    if args.interrupt:
        print(json.dumps(client.interrupt(), indent=2))
        return
    if args.consume_interrupt:
        print(json.dumps(client.consume_interrupt(), indent=2))
        return
    if args.publish:
        packet = json.loads(args.publish)
        print(json.dumps(client.publish(packet), indent=2))
        return

    parser.error("Provide an action")


if __name__ == "__main__":
    _main()
