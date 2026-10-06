"""MCP stdio 전송(줄 단위 JSON-RPC 2.0)의 최소 구현.

MCP 규격의 stdio 전송은 한 줄에 JSON-RPC 메시지 하나를 주고받는다.
외부 패키지 없이 동작하도록 표준 라이브러리만 사용한다.
"""
from __future__ import annotations

import itertools
import json
import subprocess
import sys
import threading
from typing import Any, Callable

PROTOCOL_VERSION = "2025-06-18"


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.data is not None:
            d["data"] = self.data
        return d


class StdioClient:
    """하위 프로세스를 띄우고 stdio로 JSON-RPC 요청을 보낸다."""

    def __init__(self, cmd: list[str], env: dict | None = None, cwd: str | None = None):
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, cwd=cwd,
            text=True, encoding="utf-8", bufsize=1,
        )
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> Any:
        with self._lock:
            mid = next(self._ids)
            self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise RpcError(-32000, "peer closed")
                msg = json.loads(line)
                if msg.get("id") != mid:
                    continue  # 알림 등은 무시
                if "error" in msg:
                    e = msg["error"]
                    raise RpcError(e.get("code", -32000), e.get("message", ""), e.get("data"))
                return msg.get("result")

    def notify(self, method: str, params: dict | None = None) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self, name: str) -> dict:
        res = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": name, "version": "0.1"},
        })
        self.notify("notifications/initialized")
        return res

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def serve_stdio(handler: Callable[[str, dict], Any]) -> None:
    """stdin에서 요청을 읽어 handler(method, params)의 결과를 stdout으로 돌려준다."""
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid = msg.get("id")
        try:
            result = handler(msg.get("method", ""), msg.get("params") or {})
            reply = {"jsonrpc": "2.0", "id": mid, "result": result}
        except RpcError as e:
            reply = {"jsonrpc": "2.0", "id": mid, "error": e.to_dict()}
        except Exception as e:  # noqa: BLE001 - 예기치 않은 오류도 응답으로 돌려준다
            reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": repr(e)}}
        if mid is not None:  # 알림(notification)에는 응답하지 않는다
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def text_result(text: str, is_error: bool = False, **extra) -> dict:
    """MCP tools/call 결과 형식."""
    res = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if extra:
        res["structuredContent"] = extra
    return res
