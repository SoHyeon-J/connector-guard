"""재확인 화면(이용자 전용 웹페이지).

실제 서비스에서는 로그인한 이용자 본인의 화면(플랫폼이 직접 그리는 카드·대시보드)에 해당한다.
에이전트에게는 이 화면의 1회용 값(nonce)이 전달되지 않으므로 에이전트는 재확인을 대신할 수 없다.
프로토타입은 127.0.0.1에서만 열리며, 이용자 로그인 인증은 생략했다.
"""
from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import policy

STATE_LABEL = {"active": "활성", "notified": "사전 고지", "paused": "일시정지", "released": "해제"}

PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>커넥터 권한 대시보드</title>
<style>
body{{font-family:system-ui,'Malgun Gothic',sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color:#1f1f1e}}
h1{{font-size:20px}} .day{{color:#666;font-size:14px}}
table{{border-collapse:collapse;width:100%;font-size:14px;margin:12px 0}}
th,td{{border-bottom:1px solid #e3e2dc;padding:8px;text-align:left}}
.card{{border:1px solid #c9c7bd;border-radius:10px;padding:14px 16px;margin:14px 0;background:#faf9f5}}
.card h2{{font-size:16px;margin:0 0 6px}} .card p{{margin:4px 0;font-size:14px}}
.btns{{display:flex;gap:10px;margin-top:10px}}
.btns button{{flex:1;padding:10px 0;font-size:15px;border:1px solid #555;border-radius:8px;background:#fff;cursor:pointer}}
.msg{{background:#eef4ec;padding:8px 12px;border-radius:8px}}
</style></head><body>
<h1>커넥터 권한 대시보드</h1>
<div class="day">가상 날짜: {date} (시작 후 {day}일)</div>
{msg}
{cards}
<table><tr><th>커넥터</th><th>위험 등급</th><th>권한</th><th>마지막 사용</th><th>상태</th></tr>{rows}</table>
</body></html>"""

CARD = """<div class="card"><h2>{name} — 다시 사용하시겠어요?</h2>
<p>{label}</p>
<p>권한: {scopes} ({risk})</p>
<p>마지막 사용: {last} · 정지 사유: {reason}</p>
<form method="post" action="/decision" class="btns">
<input type="hidden" name="nonce" value="{nonce}">
<button name="decision" value="keep">계속 사용</button>
<button name="decision" value="disconnect">연결 해제</button>
</form></div>"""

REASON = {"inactive": "장기간 사용되지 않음", "absolute_cap": "12개월 절대 상한 도달"}


def render(guard, msg: str = "") -> str:
    s = guard.s
    rows, cards = [], []
    for r in guard.rows():
        scopes = ", ".join(json.loads(r["scopes"]))
        rows.append(f"<tr><td>{html.escape(r['name'])}</td><td>{policy.LABEL[r['risk']]}</td>"
                    f"<td>{html.escape(scopes)}</td><td>{r['last_activity']}</td>"
                    f"<td>{STATE_LABEL[r['state']]}</td></tr>")
        if r["state"] == "paused":
            pending = s.one("SELECT * FROM nonces WHERE connector_id=? AND used=0 AND request_label IS NOT NULL "
                            "ORDER BY rowid DESC", r["id"])
            label = pending["request_label"] if pending else "정지된 커넥터를 다시 사용할지 선택해 주세요."
            nonce = pending["nonce"] if pending else guard.issue_nonce(r["id"])
            cards.append(CARD.format(name=html.escape(r["name"]), label=html.escape(label),
                                     scopes=html.escape(scopes), risk=policy.LABEL[r["risk"]],
                                     last=r["last_activity"], reason=REASON.get(r["pause_reason"], "-"),
                                     nonce=nonce))
    return PAGE.format(date=s.today().isoformat(), day=s.day_no(),
                       msg=f'<p class="msg">{html.escape(msg)}</p>' if msg else "",
                       cards="".join(cards), rows="".join(rows))


def start_ui(guard, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _html(self, body: str, code: int = 200):
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            self._html(render(guard, unquote(q.get("msg", [""])[0])))

        def do_POST(self):
            if urlparse(self.path).path != "/decision":
                return self._html("not found", 404)
            n = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8")).items()}
            ok, message = guard.decide(form.get("nonce", ""), form.get("decision", ""), actor="user")
            if ok:
                self.send_response(303)
                self.send_header("Location", "/?msg=" + quote(message))
                self.end_headers()
            else:
                # 거부 사유를 화면에 보여 준다(빈 403 페이지 대신)
                self._html(render(guard, f"처리하지 않았습니다: {message} "
                                         "화면이 열린 뒤 상태가 바뀌었을 수 있으니 새로고침 후 다시 시도해 주세요."),
                           403)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
