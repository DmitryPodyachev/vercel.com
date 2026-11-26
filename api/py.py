"""Vercel Python function: /api/py

GET  /api/py              -> HTML page with a form + server info
POST /api/py (form text)  -> same page with the processed text
GET  /api/py?format=json  -> server info as JSON (also with ?text=...)

Nothing from the request is ever executed: the text is only escaped and echoed.
System commands (df, ifconfig) run with fixed arguments, if the binary exists.
"""
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import fcntl
import html
import json
import os
import platform
import shutil
import socket
import struct
import subprocess
import sys
import time

MAX_TEXT = 2000


def disk(path):
    try:
        u = shutil.disk_usage(path)
        return {"path": path, "total_mb": u.total // 2**20, "used_mb": u.used // 2**20,
                "free_mb": u.free // 2**20, "free_pct": round(u.free * 100 / u.total, 1) if u.total else None}
    except OSError as e:
        return {"path": path, "error": str(e)}


def loadavg():
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except OSError as e:
        return str(e)


def ipv4(name):
    # SIOCGIFADDR: IPv4 address of an interface (no external tools needed)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            r = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))
            return socket.inet_ntoa(r[20:24])
    except OSError:
        return None


def interfaces():
    out = []
    stats = {}
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                name, data = line.split(":", 1)
                v = data.split()
                stats[name.strip()] = {"rx_bytes": int(v[0]), "tx_bytes": int(v[8])}
    except OSError:
        pass
    try:
        names = [n for _, n in socket.if_nameindex()]
    except OSError:
        names = list(stats)
    for n in names:
        out.append({"name": n, "ipv4": ipv4(n), **stats.get(n, {})})
    return out


def run(cmd):
    if not shutil.which(cmd[0]):
        return f"({cmd[0]}: not available in this runtime)"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        return (p.stdout or p.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return f"({cmd[0]}: {e})"


def process_text(t):
    t = t[:MAX_TEXT]
    return {"text": t, "length": len(t), "words": len(t.split()),
            "upper": t.upper(), "reversed": t[::-1]}


def info():
    return {
        "runtime": f"Python {platform.python_version()}",
        "hostname": socket.gethostname(),
        "region": os.environ.get("VERCEL_REGION", "?"),
        "time_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "loadavg": loadavg(),
        "cpus": os.cpu_count(),
        "disk": [disk("/"), disk("/tmp")],
        "interfaces": interfaces(),
        "df": run(["df", "-h"]),
        "ifconfig": run(["ifconfig"]) if shutil.which("ifconfig") else run(["ip", "addr"]),
    }


def page(result, i):
    e = html.escape
    res = ""
    if result:
        res = (f"<h2>Ответ</h2><table><tr><th>Текст</th><td>{e(result['text'])}</td></tr>"
               f"<tr><th>Символов / слов</th><td>{result['length']} / {result['words']}</td></tr>"
               f"<tr><th>UPPER</th><td>{e(result['upper'])}</td></tr>"
               f"<tr><th>Наоборот</th><td>{e(result['reversed'])}</td></tr></table>")
    disks = "".join(
        f"<tr><td>{e(d['path'])}</td><td>{d.get('free_mb', '-')} MB</td><td>{d.get('total_mb', '-')} MB</td>"
        f"<td>{d.get('free_pct', d.get('error', ''))}%</td></tr>" for d in i["disk"])
    ifs = "".join(
        f"<tr><td>{e(x['name'])}</td><td>{e(x['ipv4'] or '-')}</td><td>{x.get('rx_bytes', '-')}</td>"
        f"<td>{x.get('tx_bytes', '-')}</td></tr>" for x in i["interfaces"])
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Python backend</title>
<link rel="stylesheet" href="/style.css"></head><body>
<p><a href="/test/">← все backend</a> · <a href="/api/go">Go</a> · <a href="?format=json">JSON</a></p>
<h1>🐍 {e(i['runtime'])}</h1>
<form method="post"><input name="text" maxlength="{MAX_TEXT}" placeholder="Введите текст" autofocus>
<button>Отправить</button></form>
{res}
<h2>Сервер</h2><table>
<tr><th>Host</th><td>{e(i['hostname'])}</td></tr><tr><th>Region</th><td>{e(i['region'])}</td></tr>
<tr><th>Время UTC</th><td>{e(i['time_utc'])}</td></tr>
<tr><th>loadavg</th><td>{e(str(i['loadavg']))}</td></tr><tr><th>CPU</th><td>{i['cpus']}</td></tr></table>
<h2>Диск</h2><table><tr><th>Путь</th><th>Свободно</th><th>Всего</th><th>Свободно %</th></tr>{disks}</table>
<h2>Сеть</h2><table><tr><th>Интерфейс</th><th>IPv4</th><th>RX байт</th><th>TX байт</th></tr>{ifs}</table>
<h2>df -h</h2><pre>{e(i['df'])}</pre><h2>ifconfig / ip addr</h2><pre>{e(i['ifconfig'])}</pre>
</body></html>"""


class handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _respond(self, text, fmt):
        result = process_text(text) if text else None
        i = info()
        if fmt == "json":
            self._send(200, json.dumps({"input": result, "server": i}, ensure_ascii=False, indent=2),
                       "application/json; charset=utf-8")
        else:
            self._send(200, page(result, i), "text/html; charset=utf-8")

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        self._respond(q.get("text", [""])[0], q.get("format", [""])[0])

    def do_POST(self):
        n = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        body = self.rfile.read(n).decode("utf-8", "replace")
        if (self.headers.get("Content-Type") or "").startswith("application/json"):
            try:
                text = str(json.loads(body).get("text", ""))
            except (ValueError, AttributeError):
                text = ""
            fmt = "json"
        else:
            text = parse_qs(body).get("text", [""])[0]
            fmt = parse_qs(urlparse(self.path).query).get("format", [""])[0]
        self._respond(text, fmt)


if __name__ == "__main__":  # local test: python3 api/py.py -> http://127.0.0.1:8000/
    from http.server import HTTPServer
    print(sys.version)
    HTTPServer(("127.0.0.1", 8000), handler).serve_forever()
