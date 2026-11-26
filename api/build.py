"""Vercel Python function: /api/build – firmware builder for bhf.pp.ua.

Compiles an Arduino sketch with avr-gcc (official Arduino toolchain + ArduinoCore-avr, downloaded once per
instance into /tmp, or taken from the bundle if present) and returns a zip: firmware .hex, sketch source,
flashing instructions for Windows / macOS / Ubuntu.

GET  /api/build                      -> health: toolchain state, supported devices/firmwares
POST /api/build {"id": "<job id>"}   -> job mode: read the job from bhf (builder API, env BHF_BUILDER_TOKEN),
                                        build, upload the zip to bhf (R2) -> job JSON (status done/failed)
POST /api/build {"device","firmware","text"} -> direct mode: the zip itself in the response

The user text never reaches the compiler as code: it is embedded as a byte array.
"""
from http.server import BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
import datetime
import glob
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

AVR_GCC_URL = "https://downloads.arduino.cc/tools/avr-gcc-7.3.0-atmel3.6.1-arduino7-x86_64-pc-linux-gnu.tar.bz2"
CORE_URL = "https://downloads.arduino.cc/cores/staging/avr-1.8.6.tar.bz2"
TMP = "/tmp/bhf-avr"
BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "toolchain")
BHF_URL = os.environ.get("BHF_URL", "https://bhf.pp.ua").rstrip("/")
ORIGINS = {"https://bhf.pp.ua", "https://www.bhf.pp.ua", "https://bhf.pages.dev"}
MAX_TEXT = 4000
BAUD = 115200

DEVICES = {"arduino_uno": {"name": "Arduino Uno", "mcu": "atmega328p", "f_cpu": "16000000L",
                           "board": "AVR_UNO", "variant": "standard", "avrdude_part": "m328p",
                           "pin_min": 2, "pin_max": 19}}
FIRMWARES = {"blink": {"name": "Blink LED", "ms": 2000}, "blink_fast": {"name": "Blink LED fast", "ms": 500}}

# only the parts of the 215 MB toolchain needed for atmega328p (avr5), C/C++ without LTO
KEEP = [re.compile(p) for p in (
    r"^avr/bin/",                                   # whole dirs: tools are hardlinks of each other
    r"^avr/libexec/gcc/avr/7\.3\.0/(cc1|cc1plus|collect2)$",
    r"^avr/avr/bin/",
    r"^avr/avr/include/",
    r"^avr/avr/lib/avr5/(crtatmega328p\.o|libc\.a|libm\.a|libatmega328p\.a)$",
    r"^avr/avr/lib/ldscripts/avr5",
    r"^avr/lib/gcc/avr/7\.3\.0/(avr5/|include/|include-fixed/|device-specs/specs-atmega328p$|libgcc\.a$)",
    r"^avr-1\.8\.6/(cores/arduino/|variants/standard/)",
)]

_lock = threading.Lock()
_state = {"source": None, "prepare_s": None, "core_s": None, "builds": 0}


# ---------------------------------------------------------------- toolchain
def _fetch(url, dest):
    with urllib.request.urlopen(url, timeout=60) as r, tarfile.open(fileobj=r, mode="r|bz2") as tf:
        for m in tf:
            if any(k.match(m.name) for k in KEEP) and (m.isfile() or m.isdir() or m.issym() or m.islnk()):
                if hasattr(tarfile, "data_filter"):
                    tf.extract(m, dest, filter="tar")
                else:
                    tf.extract(m, dest)


def toolchain():
    """Directory with avr/ and avr-1.8.6/ – bundled (read-only) or downloaded into /tmp."""
    with _lock:
        for root in (os.path.realpath(BUNDLED), TMP):
            if os.access(os.path.join(root, "avr/bin/avr-gcc"), os.X_OK) and os.path.isfile(os.path.join(root, ".ok")):
                _state["source"] = _state["source"] or ("bundle" if root != TMP else "tmp (cached)")
                return root
        t = time.time()
        shutil.rmtree(TMP, ignore_errors=True)
        os.makedirs(TMP)
        with ThreadPoolExecutor(2) as ex:
            list(ex.map(lambda u: _fetch(u, TMP), (AVR_GCC_URL, CORE_URL)))
        open(os.path.join(TMP, ".ok"), "w").close()
        _state.update(source="downloaded", prepare_s=round(time.time() - t, 1))
        return TMP


def _run(cmd, cwd):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[-1500:])
    return p.stdout


def _flags(tc, dev):
    core = os.path.join(tc, "avr-1.8.6/cores/arduino")
    var = os.path.join(tc, "avr-1.8.6/variants", dev["variant"])
    return ["-mmcu=" + dev["mcu"], "-DF_CPU=" + dev["f_cpu"], "-DARDUINO=10819", "-DARDUINO_" + dev["board"],
            "-DARDUINO_ARCH_AVR", "-Os", "-w", "-ffunction-sections", "-fdata-sections", "-I" + core, "-I" + var]


def core_lib(tc, dev):
    """Arduino core compiled once per instance -> /tmp/bhf-avr-core/<mcu>/core.a"""
    out = os.path.join("/tmp/bhf-avr-core", dev["mcu"])
    lib = os.path.join(out, "core.a")
    with _lock:
        if os.path.isfile(lib):
            return lib
        t = time.time()
        os.makedirs(out, exist_ok=True)
        bin_ = os.path.join(tc, "avr/bin")
        core = os.path.join(tc, "avr-1.8.6/cores/arduino")
        fl = _flags(tc, dev)
        srcs = sorted(glob.glob(core + "/*.c") + glob.glob(core + "/*.cpp") + glob.glob(core + "/*.S"))

        def cc(src):
            obj = os.path.join(out, os.path.basename(src) + ".o")
            if src.endswith(".cpp"):
                cmd = [bin_ + "/avr-gcc", "-std=gnu++11", "-fpermissive", "-fno-exceptions", "-fno-threadsafe-statics"]
            elif src.endswith(".c"):
                cmd = [bin_ + "/avr-gcc", "-std=gnu11"]
            else:
                cmd = [bin_ + "/avr-gcc", "-x", "assembler-with-cpp"]
            _run(cmd + fl + ["-c", src, "-o", obj], out)
            return obj

        with ThreadPoolExecutor(4) as ex:
            objs = list(ex.map(cc, srcs))
        _run([bin_ + "/avr-ar", "rcs", lib + ".tmp"] + objs, out)
        os.replace(lib + ".tmp", lib)
        _state["core_s"] = round(time.time() - t, 1)
        return lib


# ---------------------------------------------------------------- sketch
PIN_NAMES = {14: "A0", 15: "A1", 16: "A2", 17: "A3", 18: "A4", 19: "A5"}

SIGNATURE_FN = """
// "signature": 5 quick blinks at power-on
void signatureBlink() {
  for (uint8_t i = 0; i < 5; i++) {
    digitalWrite(LED_PIN, HIGH);
    delay(100);
    digitalWrite(LED_PIN, LOW);
    delay(100);
  }
  delay(500);
}
"""


def comment_lines(text):
    """The message as readable // comments. Safe: one line per //, control chars replaced,
    no trailing backslash (would continue the comment onto the next code line)."""
    out = []
    for line in text.splitlines() or [""]:
        line = "".join(ch if ch >= " " or ch == "\t" else "?" for ch in line).rstrip()
        if line.endswith("\\"):
            line += " |"
        out.append("//   " + line)
    return "\n".join(out[:200]) + ("\n//   ..." if len(out) > 200 else "")


def sketch(firmware, text, job_id, pin=13, signature=False):
    fw = FIRMWARES[firmware]
    data = text.encode("utf-8")[:MAX_TEXT * 4]
    rows = [", ".join("0x%02x" % b for b in data[i:i + 16]) for i in range(0, len(data), 16)]
    arr = (",\n  ".join(rows) + ",\n  0x00") if rows else "0x00"
    if pin == 13:
        pin_txt = "13 (LED_BUILTIN, on-board LED)"
    else:
        pin_txt = f"{pin} ({PIN_NAMES[pin]})" if pin in PIN_NAMES else str(pin)
    sig_note = "; 5 quick blinks at start (signature)" if signature else ""
    sig_fn = SIGNATURE_FN if signature else ""
    sig_call = "\n  signatureBlink();" if signature else ""
    built = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    return f"""// bhf.pp.ua firmware: {fw['name']} for Arduino Uno
// job {job_id or '-'} built {built} UTC
// LED on pin {pin_txt} toggles every {fw['ms']} ms{sig_note}.
// The message is printed to Serial at {BAUD} baud (UTF-8) at start and every 10 blinks.
#include <Arduino.h>
#include <avr/pgmspace.h>

const uint8_t LED_PIN = {pin};
const unsigned long BLINK_MS = {fw['ms']}UL;

// Message (shown here as text; the code below uses its UTF-8 bytes, so any characters are safe):
{comment_lines(text)}
const uint8_t MESSAGE[] PROGMEM = {{
  {arr}
}};

void printMessage() {{
  for (uint16_t i = 0;; i++) {{
    uint8_t c = pgm_read_byte(&MESSAGE[i]);
    if (!c) break;
    Serial.write(c);
  }}
  Serial.println();
}}
{sig_fn}
void setup() {{
  pinMode(LED_PIN, OUTPUT);
  Serial.begin({BAUD});
  Serial.println(F("bhf {firmware}: LED pin {pin} toggles every {fw['ms']} ms"));
  printMessage();{sig_call}
}}

unsigned long n = 0;

void loop() {{
  digitalWrite(LED_PIN, HIGH);
  delay(BLINK_MS);
  digitalWrite(LED_PIN, LOW);
  delay(BLINK_MS);
  n++;
  if (n % 10 == 0) {{          // repeat the message every 10 blinks
    Serial.print(F("blink "));
    Serial.println(n);
    printMessage();
  }}
}}
"""


def flash_text(hexname, dev, pin=13):
    p = dev["avrdude_part"]
    wiring = ""
    if pin != 13:
        name = f" ({PIN_NAMES[pin]})" if pin in PIN_NAMES else ""
        wiring = (f"\nWiring: LED + resistor 220-330 Ohm between pin {pin}{name} and GND (long leg = +).\n"
                  f"Подключение: светодиод + резистор 220-330 Ом между пином {pin}{name} и GND (длинная нога = +).\n")
    return f"""Flash {hexname} to an {dev['name']} (USB cable), then watch the debug output (Serial, {BAUD} baud, UTF-8).
Прошивка {hexname} в {dev['name']} по USB и просмотр отладочного вывода (Serial, {BAUD} бод, UTF-8).
{wiring}
=== Windows (PowerShell; no admin rights needed / права администратора не нужны) ===
# 1. avrdude (once / один раз)
Invoke-WebRequest https://github.com/avrdudes/avrdude/releases/download/v8.3/avrdude-v8.3-windows-x64.zip -OutFile avrdude.zip
Expand-Archive avrdude.zip -DestinationPath avrdude
# 2. port / порт (e.g. COM3)
[System.IO.Ports.SerialPort]::GetPortNames()
# 3. flash / прошить
.\\avrdude\\avrdude.exe -c arduino -p {p} -P COM3 -b 115200 -U flash:w:{hexname}:i
# 4. debug output / вывод (Ctrl+C to stop); UTF-8 so Cyrillic is readable
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$s = New-Object System.IO.Ports.SerialPort COM3,{BAUD}; $s.Encoding = [Text.Encoding]::UTF8; $s.DtrEnable = $true; $s.Open(); while ($true) {{ $s.ReadLine() }}
# No COM port? Clones with a CH340 chip need a driver (installing it needs admin rights).

=== macOS (Terminal; no sudo needed / sudo не нужен) ===
brew install avrdude                    # once (Homebrew: https://brew.sh)
ls /dev/cu.usbmodem* /dev/cu.usbserial* # port
avrdude -c arduino -p {p} -P /dev/cu.usbmodem1101 -b 115200 -U flash:w:{hexname}:i
screen -U /dev/cu.usbmodem1101 {BAUD}   # output; exit: Ctrl+A, then K, then Y

=== Ubuntu / Debian (sudo for install and, once, for port access / нужен sudo) ===
sudo apt install -y avrdude screen      # once, needs sudo
sudo usermod -aG dialout $USER          # once: serial ports without sudo; then log out and in
ls /dev/ttyACM* /dev/ttyUSB*            # port (original Uno: ttyACM0, clones with CH340: ttyUSB0)
avrdude -c arduino -p {p} -P /dev/ttyACM0 -b 115200 -U flash:w:{hexname}:i
#   "Permission denied" on the port = not in dialout yet: log out/in, or put sudo in front
screen -U /dev/ttyACM0 {BAUD}           # output; exit: Ctrl+A, then K, then Y
#   Port disappears right after plugging in (CH340 clones)? sudo apt remove brltty

Notes / Заметки
- Close Arduino IDE / Serial Monitor before flashing (port busy). Закройте Arduino IDE / монитор порта перед прошивкой.
- "not in sync" / "programmer is not responding": old bootloader (clones, Nano) -> use -b 57600.
- Opening the serial port resets the Uno, so the message is printed right after connecting; then every 10 blinks.
- Cyrillic and other characters are sent as UTF-8, no transliteration needed; the terminal must use UTF-8.
- Source: sketch/bhf_blink/bhf_blink.ino (Arduino IDE / arduino-cli: board arduino:avr:uno).
"""


def build(device, firmware, text, job_id="", pin=13, signature=False):
    if device not in DEVICES:
        raise ValueError(f"platform '{device}' is not supported yet (supported: {', '.join(DEVICES)})")
    if firmware not in FIRMWARES:
        raise ValueError(f"unknown firmware '{firmware}'")
    text = str(text or "")[:MAX_TEXT]
    dev = DEVICES[device]
    try:
        pin = int(13 if pin in (None, "") else pin)
    except (TypeError, ValueError):
        raise ValueError("pin must be a number")
    if not dev["pin_min"] <= pin <= dev["pin_max"]:
        raise ValueError(f"pin must be {dev['pin_min']}..{dev['pin_max']} (0/1 are Serial RX/TX)")
    signature = signature is True or signature in ("1", "on", "true")
    t0 = time.time()
    tc = toolchain()
    t1 = time.time()
    lib = core_lib(tc, dev)
    t2 = time.time()
    work = f"/tmp/bhf-build-{os.getpid()}-{threading.get_ident()}-{int(t0 * 1000)}"
    os.makedirs(work)
    try:
        src = sketch(firmware, text, job_id, pin, signature)
        with open(work + "/sketch.cpp", "w") as f:
            f.write(src)
        bin_ = os.path.join(tc, "avr/bin")
        _run([bin_ + "/avr-gcc", "-std=gnu++11", "-fno-exceptions", "-fno-threadsafe-statics"] + _flags(tc, dev)
             + ["-c", "sketch.cpp", "-o", "sketch.o"], work)
        _run([bin_ + "/avr-gcc", "-mmcu=" + dev["mcu"], "-Os", "-fno-use-linker-plugin", "-Wl,--gc-sections", "-o", "fw.elf", "sketch.o", lib, "-lm"], work)
        _run([bin_ + "/avr-objcopy", "-O", "ihex", "-R", ".eeprom", "fw.elf", "fw.hex"], work)
        size = _run([bin_ + "/avr-size", "-A", "fw.elf"], work)
        flash = sum(int(m) for m in re.findall(r"^\.(?:text|data)\s+(\d+)", size, re.M))
        ram = sum(int(m) for m in re.findall(r"^\.(?:data|bss)\s+(\d+)", size, re.M))
        if flash > 32256:
            raise RuntimeError(f"firmware too large: {flash} bytes > 32256 (Uno flash minus bootloader)")
        with open(work + "/fw.hex", "rb") as f:
            hexdata = f.read()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    base = f"{device}-{firmware}" + (f"-{job_id[-8:]}" if job_id else "")
    hexname = base + ".hex"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(hexname, hexdata)
        z.writestr("FLASH.txt", flash_text(hexname, dev, pin))
        z.writestr("sketch/bhf_blink/bhf_blink.ino", src)
    _state["builds"] += 1
    info = {"flash_bytes": flash, "ram_bytes": ram, "hex_sha256": hashlib.sha256(hexdata).hexdigest()[:16],
            "toolchain_s": round(t1 - t0, 1), "core_s": round(t2 - t1, 1), "total_s": round(time.time() - t0, 1)}
    return base + ".zip", buf.getvalue(), info


# ---------------------------------------------------------------- bhf job API
def bhf(method, path, body=None, ctype="application/json"):
    tok = os.environ.get("BHF_BUILDER_TOKEN", "")
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    req = urllib.request.Request(BHF_URL + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": ctype,
                                          "User-Agent": "bhf-builder/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": str(e)}


def public(job):
    """Never echo the user text / internal fields of a job."""
    j = {k: v for k, v in (job or {}).items() if k not in ("text", "ip_country")}
    if isinstance(j.get("artifact"), dict):
        j["artifact"] = {k: j["artifact"].get(k) for k in ("filename", "size")}
    return j


def run_job(job_id):
    if not os.environ.get("BHF_BUILDER_TOKEN"):
        return 503, {"error": "builder token not configured (env BHF_BUILDER_TOKEN)", "code": "no_token"}
    if not re.fullmatch(r"[a-z0-9]{8,40}", job_id or ""):
        return 400, {"error": "bad job id"}
    st, job = bhf("GET", f"/api/builder/jobs/{job_id}")
    if st != 200:
        return 404 if st == 404 else 502, {"error": f"job lookup failed ({st})", "detail": job.get("error")}
    if job.get("status") == "done":
        return 200, public(job)
    if job.get("status") == "building" and job.get("updated", "") > (
            datetime.datetime.utcnow() - datetime.timedelta(minutes=3)).isoformat():
        return 409, {"error": "already building", "job": public(job)}
    bhf("POST", f"/api/builder/jobs/{job_id}/status", {"status": "building", "message": "vercel"})
    try:
        name, data, info = build(job.get("device"), job.get("firmware"), job.get("text", ""), job_id,
                                 job.get("pin", 13), job.get("signature", False))
    except Exception as e:  # noqa: BLE001 – report any build error to the job
        msg = str(e)[-480:]
        st, j = bhf("POST", f"/api/builder/jobs/{job_id}/status", {"status": "failed", "message": msg})
        return 200, public(j) if st == 200 else {"status": "failed", "message": msg}
    st, j = bhf("PUT", f"/api/builder/jobs/{job_id}/artifact?filename={name}", data, "application/zip")
    if st != 200:
        bhf("POST", f"/api/builder/jobs/{job_id}/status", {"status": "failed", "message": f"upload failed ({st})"})
        return 502, {"error": f"upload failed ({st})", "detail": j.get("error")}
    j = public(j)
    j["build"] = info
    return 200, j


class handler(BaseHTTPRequestHandler):
    def _cors(self):
        o = self.headers.get("Origin", "")
        if o in ORIGINS or re.fullmatch(r"https://[a-z0-9-]+\.bhf\.pages\.dev", o):
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Expose-Headers", "Content-Disposition, X-Build-Info")
            self.send_header("Access-Control-Max-Age", "86400")

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        self._json(200, {"service": "bhf firmware builder", "devices": {k: v["name"] for k, v in DEVICES.items()},
                         "pins": {k: [v["pin_min"], v["pin_max"], 13] for k, v in DEVICES.items()},
                         "firmwares": {k: v["name"] for k, v in FIRMWARES.items()}, "toolchain": _state,
                         "job_mode": bool(os.environ.get("BHF_BUILDER_TOKEN")), "bhf": BHF_URL})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            return self._json(413, {"error": "request too large"})
        try:
            b = json.loads(self.rfile.read(n) or b"{}")
            assert isinstance(b, dict)
        except (ValueError, AssertionError):
            return self._json(400, {"error": "invalid JSON"})
        if b.get("id"):
            code, data = run_job(str(b["id"]))
            return self._json(code, data)
        text = str(b.get("text", ""))
        if len(text) > MAX_TEXT:
            return self._json(400, {"error": f"text longer than {MAX_TEXT} characters"})
        try:
            name, data, info = build(str(b.get("device", "")), str(b.get("firmware", "")), text,
                                     pin=b.get("pin", 13), signature=b.get("signature", False))
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": "build failed", "detail": str(e)[-800:]})
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("X-Build-Info", json.dumps(info))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":  # local test: python3 api/build.py -> http://127.0.0.1:8004/
    from http.server import ThreadingHTTPServer
    ThreadingHTTPServer(("127.0.0.1", 8004), handler).serve_forever()
