"""Vercel Python function: /api/pysys

GET /api/pysys          -> system info (CPU, GPU, RAM, disk, OS) as JSON
GET /api/pysys?bench=1  -> the same + a small benchmark (~1-3 s): sha256, prime sieve, memory copy, /tmp write/read
"""
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import glob
import hashlib
import json
import os
import platform
import shutil
import time

MB = 2**20


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def kv(path):
    out = {}
    for line in read(path).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out.setdefault(k.strip(), v.strip())
    return out


def mb(kb_str):
    try:
        return int(kb_str.split()[0]) // 1024
    except (ValueError, IndexError):
        return None


def cpu():
    info = kv("/proc/cpuinfo")
    flags = info.get("flags", "").split()
    try:
        affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = None
    quota = read("/sys/fs/cgroup/cpu.max")
    return {
        "model": info.get("model name") or platform.processor() or "?",
        "vendor": info.get("vendor_id"),
        "mhz": info.get("cpu MHz"),
        "logical_cpus": os.cpu_count(),
        "usable_cpus": affinity,
        "cgroup_cpu_max": quota or None,
        "hypervisor": "hypervisor" in flags,
        "features": [f for f in ("sse4_2", "avx", "avx2", "avx512f", "aes", "sha_ni", "vaes") if f in flags],
        "arch": platform.machine(),
    }


def ram():
    m = kv("/proc/meminfo")
    st = kv("/proc/self/status")
    limit = read("/sys/fs/cgroup/memory.max")
    cur = read("/sys/fs/cgroup/memory.current")
    return {
        "total_mb": mb(m.get("MemTotal", "")),
        "available_mb": mb(m.get("MemAvailable", "")),
        "cgroup_limit_mb": int(limit) // MB if limit.isdigit() else (limit or None),
        "cgroup_used_mb": int(cur) // MB if cur.isdigit() else None,
        "process_rss_mb": mb(st.get("VmRSS", "")),
        "lambda_memory_mb": os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE"),
    }


def gpu():
    found = glob.glob("/dev/nvidia*") + glob.glob("/proc/driver/nvidia/gpus/*") + glob.glob("/dev/dri/*")
    return {"found": bool(found), "devices": found[:10],
            "note": "GPU не обнаружено (serverless-функции работают только на CPU)" if not found else ""}


def disks():
    out = []
    for p in ("/", "/tmp"):
        try:
            u = shutil.disk_usage(p)
            out.append({"path": p, "total_mb": u.total // MB, "free_mb": u.free // MB})
        except OSError as e:
            out.append({"path": p, "error": str(e)})
    return out


def system():
    up = read("/proc/uptime").split()
    osr = {}
    for line in read("/etc/os-release").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            osr[k] = v.strip('"')
    return {
        "runtime": f"Python {platform.python_version()} ({platform.python_implementation()})",
        "os": osr.get("PRETTY_NAME", platform.system()),
        "kernel": platform.release(),
        "uptime_s": round(float(up[0])) if up else None,
        "region": os.environ.get("VERCEL_REGION", "?"),
        "aws_region": os.environ.get("AWS_REGION"),
        "machine": read("/sys/class/dmi/id/product_name") or None,
        "loadavg": [round(x, 2) for x in os.getloadavg()],
    }


def timed(fn):
    t = time.perf_counter()
    r = fn()
    return r, time.perf_counter() - t


def bench():
    res = {}
    block = os.urandom(MB)

    # 1. SHA-256 over 128 MB (C code inside hashlib: shows raw CPU speed)
    def sha():
        h = hashlib.sha256()
        for _ in range(128):
            h.update(block)
        return h.hexdigest()[:8]
    _, s = timed(sha)
    res["sha256_128mb"] = {"ms": round(s * 1000), "mb_s": round(128 / s)}

    # 2. Prime sieve up to 2 000 000 in pure interpreted code (shows language speed)
    def sieve(n=2_000_000):
        a = bytearray([1]) * (n + 1)
        a[0] = a[1] = 0
        i = 2
        while i * i <= n:
            if a[i]:
                j = i * i
                while j <= n:
                    a[j] = 0
                    j += i
            i += 1
        return sum(a)
    cnt, s = timed(sieve)
    res["sieve_2m"] = {"ms": round(s * 1000), "primes": cnt}

    # 3. Memory: allocate 256 MB and copy it
    def memcopy():
        a = bytearray(256 * MB)
        b = bytes(a)
        return len(b)
    _, s = timed(memcopy)
    res["mem_alloc_copy_256mb"] = {"ms": round(s * 1000), "gb_s": round(0.25 / s, 2)}

    # 4. Disk: write 64 MB to /tmp with fsync, then read back
    path = f"/tmp/bench-{os.getpid()}.bin"
    try:
        def write():
            with open(path, "wb") as f:
                for _ in range(64):
                    f.write(block)
                f.flush()
                os.fsync(f.fileno())
        _, s = timed(write)
        res["disk_write_64mb"] = {"ms": round(s * 1000), "mb_s": round(64 / s)}

        def readback():
            n = 0
            with open(path, "rb") as f:
                while chunk := f.read(MB):
                    n += len(chunk)
            return n
        _, s = timed(readback)
        res["disk_read_64mb"] = {"ms": round(s * 1000), "mb_s": round(64 / s), "note": "из кеша ОС"}
    except OSError as e:
        res["disk"] = {"error": str(e)}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return res


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        t = time.perf_counter()
        data = {"lang": "python", "system": system(), "cpu": cpu(), "ram": ram(), "gpu": gpu(), "disk": disks()}
        if q.get("bench", [""])[0] == "1":
            data["bench"] = bench()
        data["total_ms"] = round((time.perf_counter() - t) * 1000)
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":  # local test: python3 api/pysys.py -> http://127.0.0.1:8002/?bench=1
    from http.server import HTTPServer
    HTTPServer(("127.0.0.1", 8002), handler).serve_forever()
