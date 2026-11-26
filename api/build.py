"""Vercel Python function: /api/build – firmware builder for bhf.pp.ua.

Arduino Uno / Nano: compiles a sketch with avr-gcc (official Arduino toolchain + ArduinoCore-avr, downloaded once
per instance into /tmp, or taken from the bundle if present).
Raspberry Pi Pico, ESP8266, ESP32-CAM: a template firmware compiled once with arduino-cli (templates/) gets its
config block (pin, interval, signature, job id, message) patched; image checksums are recomputed.
Raspberry Pi Zero: a Python program + systemd service + install.sh.
Returns a zip: firmware file(s), source, flashing instructions for Windows / macOS / Ubuntu (+ Raspberry Pi).

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
import struct
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

AVR_GCC_URL = "https://downloads.arduino.cc/tools/avr-gcc-7.3.0-atmel3.6.1-arduino7-x86_64-pc-linux-gnu.tar.bz2"
CORE_URL = "https://downloads.arduino.cc/cores/staging/avr-1.8.6.tar.bz2"
TMP = "/tmp/bhf-avr-2"                  # bump when KEEP changes
BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "toolchain")
BHF_URL = os.environ.get("BHF_URL", "https://bhf.pp.ua").rstrip("/")
ORIGINS = {"https://bhf.pp.ua", "https://www.bhf.pp.ua", "https://bhf.pages.dev"}
MAX_TEXT = 4000
BAUD = 115200

AVR_PINS = list(range(2, 20))                     # 0/1 = Serial RX/TX, 14-19 = A0-A5
DEVICES = {
    "arduino_uno": {"kind": "avr", "name": "Arduino Uno", "mcu": "atmega328p", "f_cpu": "16000000L", "board": "AVR_UNO",
                    "variant": "standard", "avrdude_part": "m328p", "fqbn": "arduino:avr:uno", "pins": AVR_PINS, "led": 13},
    "arduino_nano": {"kind": "avr", "name": "Arduino Nano", "mcu": "atmega328p", "f_cpu": "16000000L", "board": "AVR_NANO",
                     "variant": "eightanaloginputs", "avrdude_part": "m328p", "fqbn": "arduino:avr:nano", "pins": AVR_PINS, "led": 13},
    "rpi_pico": {"kind": "uf2", "name": "Raspberry Pi Pico", "template": "rpi_pico.uf2", "fqbn": "rp2040:rp2040:rpipico",
                 "pins": list(range(0, 23)) + [25, 26, 27, 28], "led": 25, "active_low": []},
    "esp8266": {"kind": "esp8266", "name": "ESP8266 (ESP-12E/F)", "template": "esp8266.bin", "fqbn": "esp8266:esp8266:nodemcuv2",
                "pins": [2, 4, 5, 12, 13, 14, 16], "led": 2, "active_low": [2]},       # 0/15 boot straps, 1/3 Serial, 6-11 flash
    "esp32_cam": {"kind": "esp32", "name": "ESP32-CAM (AI Thinker)", "template": "esp32_cam/app.bin", "fqbn": "esp32:esp32:esp32cam",
                  "pins": [33, 4, 2, 12, 13, 14, 15], "led": 33, "active_low": [33]},  # 33 red LED (active low), 4 = flash LED
    "rpi_zero": {"kind": "linux", "name": "Raspberry Pi Zero / Zero 2 W", "pins": [99] + list(range(2, 28)), "led": 99},
}
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
    r"^avr-1\.8\.6/(cores/arduino/|variants/standard/|variants/eightanaloginputs/)",
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
    out = os.path.join("/tmp/bhf-avr-core", dev["mcu"] + "-" + dev["variant"])
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


def sketch(firmware, text, job_id, pin=13, signature=False, dev=None):
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
    return f"""// bhf.pp.ua firmware: {fw['name']} for {(dev or DEVICES['arduino_uno'])['name']}
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
  Serial.println(F("bhf {firmware} job {job_id or '-'}: LED pin {pin} toggles every {fw['ms']} ms"));
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
ls /dev/ttyACM* /dev/ttyUSB*            # port (original Uno: ttyACM0, Nano / clones with CH340: ttyUSB0)
avrdude -c arduino -p {p} -P /dev/ttyACM0 -b 115200 -U flash:w:{hexname}:i
#   "Permission denied" on the port = not in dialout yet: log out/in, or put sudo in front
screen -U /dev/ttyACM0 {BAUD}           # output; exit: Ctrl+A, then K, then Y
#   Port disappears right after plugging in (CH340 clones)? sudo apt remove brltty

Notes / Заметки
- Close Arduino IDE / Serial Monitor before flashing (port busy). Закройте Arduino IDE / монитор порта перед прошивкой.
- "not in sync" / "programmer is not responding": old bootloader (most Nano and clones) -> use -b 57600
  (Arduino IDE: "ATmega328P (Old Bootloader)").
- Opening the serial port resets the board, so the message is printed right after connecting; then every 10 blinks.
- Cyrillic and other characters are sent as UTF-8, no transliteration needed; the terminal must use UTF-8.
- Source: sketch/bhf_blink/bhf_blink.ino (Arduino IDE / arduino-cli: board {dev['fqbn']}).
"""


# ---------------------------------------------------------------- template boards (ESP32, ESP8266, RP2040)
# The template sketch (templates/src) is compiled once per board with arduino-cli (templates/build.sh) and
# committed; a build only patches the BhfCfg data block and recomputes the image checksums.
TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates")
CFG_MAGIC = b"BHFCFG01"
CFG_MSG = 16384
CFG_HEAD = struct.Struct("<8sIiIBBBB16s44s")                 # matches struct BhfCfg up to msg
CFG_SIZE = CFG_HEAD.size + CFG_MSG


def cfg_block(firmware, text, job_id, pin, signature, active_low):
    msg = text.encode("utf-8")[:CFG_MSG - 1]
    return CFG_HEAD.pack(CFG_MAGIC, CFG_SIZE, pin, FIRMWARES[firmware]["ms"], int(bool(signature)), int(bool(active_low)),
                         0, 0, firmware.encode()[:15], (job_id or "-").encode()[:43]) + msg + b"\0" * (CFG_MSG - len(msg))


def patch_cfg(img, block):
    """Replace the single BhfCfg block in a flat image (bytearray), in place."""
    i = img.find(CFG_MAGIC)
    if i < 0 or img.find(CFG_MAGIC, i + 1) >= 0:
        raise RuntimeError("template: config block not found exactly once")
    if struct.unpack_from("<I", img, i + 8)[0] != CFG_SIZE:
        raise RuntimeError("template: config block size mismatch")
    img[i:i + CFG_SIZE] = block
    return i


def esp_fix(img, chip):
    """Recompute checksum (+ SHA-256 on ESP32) of an ESP app image (bytearray, starts with 0xE9), in place."""
    if img[0] != 0xE9:
        raise RuntimeError("not an ESP image")
    nseg = img[1]
    pos = 24 if chip == "esp32" else 8                        # ESP32: 8-byte header + 16-byte extended header
    cs = 0xEF
    for _ in range(nseg):
        _addr, ln = struct.unpack_from("<II", img, pos)
        pos += 8
        for b in img[pos:pos + ln]:
            cs ^= b
        pos += ln
    pos += 15 - pos % 16                                      # checksum = last byte of the 16-byte aligned padding
    img[pos] = cs
    pos += 1
    if chip == "esp32" and img[23] == 1:                      # hash_appended
        img[pos:pos + 32] = hashlib.sha256(bytes(img[:pos])).digest()
        pos += 32
    return pos


def uf2_patch(data, block):
    """UF2 -> flat image -> patch -> UF2 (same blocks)."""
    blocks = [bytearray(data[i:i + 512]) for i in range(0, len(data), 512)]
    addrs = [struct.unpack_from("<I", b, 12)[0] for b in blocks]
    base = min(addrs)
    flat = bytearray(max(addrs) + 256 - base)
    for b, a in zip(blocks, addrs):
        flat[a - base:a - base + 256] = b[32:32 + 256]
    patch_cfg(flat, block)
    for b, a in zip(blocks, addrs):
        b[32:32 + 256] = flat[a - base:a - base + 256]
    return b"".join(blocks)


_CRC_TABLE = []
for _i in range(256):
    _c = _i << 24
    for _ in range(8):
        _c = ((_c << 1) ^ 0x04C11DB7) if _c & 0x80000000 else (_c << 1)
    _CRC_TABLE.append(_c & 0xFFFFFFFF)


def crc8266(data):
    """CRC-32/MPEG-2 – the eboot image CRC of the ESP8266 Arduino core (tools/elf2bin.py)."""
    crc = 0xFFFFFFFF
    t = _CRC_TABLE
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ t[(crc >> 24) ^ b]
    return crc


def esp8266_patch(data, block):
    """ESP8266 Arduino .bin = eboot (padded to 4 KB) + app image; CRC of the whole file at 0x1010/0x1014."""
    img = bytearray(data)
    patch_cfg(img, block)
    struct.pack_into("<II", img, 0x1010, 0, 0)               # the CRC slots were 0 when elf2bin computed the checksum
    app = img[0x1000:]
    esp_fix(app, "esp8266")
    img[0x1000:] = app
    struct.pack_into("<II", img, 0x1010, len(img), crc8266(img))
    return bytes(img)


def esp32_patch(data, block):
    img = bytearray(data)
    patch_cfg(img, block)
    esp_fix(img, "esp32")
    return bytes(img)


# ---------------------------------------------------------------- Raspberry Pi Zero (Linux): a Python program + systemd
PI_ACT = 99                                                   # "pin" value for the on-board ACT LED

PI_SCRIPT = r'''#!/usr/bin/env python3
"""bhf.pp.ua firmware for Raspberry Pi Zero: {fw_name}, job {job}.
LED {led_txt} toggles every {ms} ms{sig_note}. The message goes to stdout (journalctl -u bhf-blink)
at start and every 10 blinks. Runs as a systemd service (install.sh), needs root for the LED.
"""
import glob, os, shutil, signal, subprocess, sys, time

JOB = {job!r}
FIRMWARE = {fw!r}
PIN = {pin}                     # BCM GPIO number; 99 = on-board ACT LED
HALF_MS = {ms}
SIGNATURE = {signature}
MESSAGE = {text!r}


class ActLed:
    """On-board LED via /sys/class/leds (ACT on Bookworm, led0 on older images)."""
    def __init__(self):
        self.d = next((d for d in ("/sys/class/leds/ACT", "/sys/class/leds/led0") if os.path.isdir(d)), None)
        if not self.d:
            sys.exit("no on-board LED in /sys/class/leds")
        self.trigger = open(self.d + "/trigger").read().split("[")[-1].split("]")[0]
        open(self.d + "/trigger", "w").write("none")
    def set(self, on):
        open(self.d + "/brightness", "w").write("1" if on else "0")
    def close(self):
        open(self.d + "/trigger", "w").write(self.trigger or "mmc0")


class GpioLed:
    """External LED on a BCM GPIO: gpiozero if installed, else the pinctrl / raspi-gpio tools."""
    def __init__(self, pin):
        self.pin, self.z = pin, None
        try:
            from gpiozero import LED
            self.z = LED(pin)
        except Exception:
            self.tool = shutil.which("pinctrl") or shutil.which("raspi-gpio")
            if not self.tool:
                sys.exit("install gpiozero: sudo apt install python3-gpiozero")
    def set(self, on):
        if self.z:
            self.z.value = on
        elif self.tool.endswith("pinctrl"):
            subprocess.run([self.tool, "set", str(self.pin), "op", "dh" if on else "dl"], check=False)
        else:
            subprocess.run([self.tool, "set", str(self.pin), "op", "dh" if on else "dl"], check=False)
    def close(self):
        self.set(False)


def say(s):
    print(s, flush=True)


def main():
    led = ActLed() if PIN == 99 else GpioLed(PIN)
    stop = lambda *a: (led.close(), sys.exit(0))
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    say(f"bhf {{FIRMWARE}} job {{JOB}}: LED pin {{'ACT' if PIN == 99 else PIN}} toggles every {{HALF_MS}} ms")
    say(MESSAGE)
    if SIGNATURE:
        for _ in range(5):
            led.set(True); time.sleep(0.1); led.set(False); time.sleep(0.1)
        time.sleep(0.5)
    n = 0
    while True:
        led.set(True); time.sleep(HALF_MS / 1000)
        led.set(False); time.sleep(HALF_MS / 1000)
        n += 1
        if n % 10 == 0:
            say(f"blink {{n}}")
            say(MESSAGE)


if __name__ == "__main__":
    main()
'''

PI_SERVICE = """[Unit]
Description=bhf.pp.ua blink firmware
After=multi-user.target

[Service]
ExecStart=/usr/bin/python3 -u /opt/bhf/bhf_blink.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

PI_INSTALL = """#!/bin/sh
# Install / update the bhf firmware on this Raspberry Pi (run: sudo ./install.sh)
set -e
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "run with sudo: sudo ./install.sh"; exit 1; }
install -d /opt/bhf
install -m 755 bhf_blink.py /opt/bhf/bhf_blink.py
install -m 644 bhf-blink.service /etc/systemd/system/bhf-blink.service
systemctl daemon-reload
systemctl enable bhf-blink >/dev/null 2>&1
systemctl restart bhf-blink
sleep 1
journalctl -u bhf-blink -n 5 --no-pager
echo "installed. Output: journalctl -u bhf-blink -f   Remove: sudo ./install.sh remove"
"""
PI_INSTALL = PI_INSTALL.replace('set -e\n', 'set -e\nif [ "$1" = remove ]; then systemctl disable --now bhf-blink; rm -f /etc/systemd/system/bhf-blink.service; rm -rf /opt/bhf; echo removed; exit 0; fi\n', 1)


def pi_files(firmware, text, job_id, pin, signature):
    fw = FIRMWARES[firmware]
    led_txt = "ACT (on-board)" if pin == PI_ACT else f"on GPIO{pin}"
    script = PI_SCRIPT.format(fw_name=fw["name"], job=job_id or "-", fw=firmware, pin=pin, ms=fw["ms"],
                              signature=bool(signature), text=text, led_txt=led_txt,
                              sig_note="; 5 quick blinks at start" if signature else "")
    compile(script, "bhf_blink.py", "exec")                   # the message is a repr() literal – must always compile
    return script


# ---------------------------------------------------------------- instructions for the non-AVR boards
def _ps_serial(port, reset_lines=True):
    dtr = "$true" if reset_lines else "$false"
    return (f"[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
            f"$s = New-Object System.IO.Ports.SerialPort {port},{BAUD}; $s.Encoding = [Text.Encoding]::UTF8; "
            f"$s.DtrEnable = {dtr}; $s.RtsEnable = $false; $s.Open(); while ($true) {{ $s.ReadLine() }}")


def _wiring(dev, pin):
    if pin == dev["led"] or (dev["kind"] == "esp32" and pin == 4):
        return ""
    return (f"\nWiring: LED + resistor 220-330 Ohm between GPIO{pin} and GND (long leg = +).\n"
            f"Подключение: светодиод + резистор 220-330 Ом между GPIO{pin} и GND (длинная нога = +).\n")


def uf2_flash_text(name, dev, pin):
    return f"""Flash {name} to a {dev['name']}, then watch the debug output (USB serial, {BAUD} baud, UTF-8).
Прошивка {name} в {dev['name']} и просмотр отладочного вывода (USB serial, UTF-8).
{_wiring(dev, pin)}
1. Hold the BOOTSEL button, plug the Pico in (USB), release: a drive RPI-RP2 appears.
   Зажмите BOOTSEL, подключите Pico по USB, отпустите: появится диск RPI-RP2.
   Already running a bhf / Arduino firmware? The 1200-baud command below reboots it into RPI-RP2 without the button.
2. Copy the .uf2 onto the drive: the Pico reboots into the new firmware. Скопируйте .uf2 на диск – Pico перезагрузится.

=== Windows (PowerShell, no admin rights needed) ===
$p = New-Object System.IO.Ports.SerialPort COM5,1200; $p.Open(); $p.Close()     # optional: instead of BOOTSEL
Copy-Item {name} ((Get-Volume -FileSystemLabel RPI-RP2).DriveLetter + ":\\")
[System.IO.Ports.SerialPort]::GetPortNames()                                     # port after reboot
{_ps_serial("COM5")}

=== macOS ===
stty -f /dev/cu.usbmodem1101 1200        # optional: instead of BOOTSEL
cp {name} /Volumes/RPI-RP2/
screen -U /dev/cu.usbmodem1101 {BAUD}    # exit: Ctrl+A, K, Y

=== Ubuntu / Debian / Raspberry Pi OS ===
stty -F /dev/ttyACM0 1200                # optional: instead of BOOTSEL
cp {name} /media/$USER/RPI-RP2/          # desktop auto-mount
#   no desktop (Raspberry Pi Lite): sudo mount /dev/sda1 /mnt && sudo cp {name} /mnt && sudo umount /mnt
screen -U /dev/ttyACM0 {BAUD}            # sudo apt install screen; port access: sudo usermod -aG dialout $USER

Notes / Заметки
- Pico W / Pico 2 W: the on-board LED is on the WiFi chip, not GPIO25 – use an external LED on another pin.
- The Pico waits up to 3 s for a terminal after start, prints the message, then repeats it every 10 blinks
  (opening the port does not reset the Pico).
- picotool alternative: picotool load -x {name}  (board in BOOTSEL mode).
- Source: sketch/bhf_template/bhf_template.ino (arduino-pico, board {dev['fqbn']}); this build's values: config.txt.
"""


def esp_flash_text(name, dev, pin):
    if dev["kind"] == "esp32":
        chip = "esp32"
        args = f"-z 0x1000 bootloader.bin 0x8000 partitions.bin 0xe000 boot_app0.bin 0x10000 {name}"
        board = """ESP32-CAM has no USB port: use the ESP32-CAM-MB adapter board (USB, automatic boot mode), or a 3.3 V
USB-UART adapter (TX->U0R, RX->U0T, 5V, GND): connect IO0 to GND, press RST, flash, then remove IO0-GND and press RST.
У ESP32-CAM нет USB: нужна плата ESP32-CAM-MB или USB-UART (3,3 В): IO0 на GND, RST, прошить, убрать IO0-GND, RST.
LED: 33 = small red LED on the back (lit when LOW); 4 = very bright white flash LED."""
        later = f"\n- Updating a board that already runs a bhf firmware: 0x10000 {name} alone is enough."
    else:
        chip = "esp8266"
        args = f"0x0 {name}"
        board = """Boards with USB (NodeMCU, Wemos D1 mini) enter boot mode automatically. A bare ESP-12 module needs a 3.3 V
USB-UART adapter: GPIO0 to GND during reset, EN and GPIO2 high, GPIO15 low.
NodeMCU / D1 mini прошиваются по USB сами; голый модуль ESP-12 – через USB-UART 3,3 В, GPIO0 на GND при сбросе.
LED: GPIO2 = blue LED on the ESP-12 module (lit when LOW)."""
        later = ""
    return f"""Flash {name} to an {dev['name']} with esptool, then watch the debug output (Serial, {BAUD} baud, UTF-8).
Прошивка {name} в {dev['name']} через esptool и просмотр отладочного вывода (Serial, {BAUD} бод, UTF-8).
{_wiring(dev, pin)}
{board}

=== Windows (PowerShell, no admin rights needed) ===
winget install -e --id Python.Python.3.12          # once, if there is no Python
py -m pip install esptool                          # once
[System.IO.Ports.SerialPort]::GetPortNames()       # port, e.g. COM3 (CH340/CP210x adapters need their driver)
py -m esptool --chip {chip} --port COM3 --baud 460800 write_flash {args}
py -m serial.tools.miniterm --raw --dtr 0 --rts 0 COM3 {BAUD}     # output; exit: Ctrl+]

=== macOS ===
python3 -m venv ~/esptool && ~/esptool/bin/pip install esptool      # once
ls /dev/cu.usbserial* /dev/cu.wchusbserial* /dev/cu.SLAB*            # port
~/esptool/bin/python -m esptool --chip {chip} --port /dev/cu.usbserial-110 --baud 460800 write_flash {args}
~/esptool/bin/python -m serial.tools.miniterm --raw --dtr 0 --rts 0 /dev/cu.usbserial-110 {BAUD}   # exit: Ctrl+]

=== Ubuntu / Debian / Raspberry Pi OS ===
sudo apt install -y python3-venv && python3 -m venv ~/esptool && ~/esptool/bin/pip install esptool   # once
sudo usermod -aG dialout $USER                     # once: port without sudo, then log out and in
ls /dev/ttyUSB* /dev/ttyACM*
~/esptool/bin/python -m esptool --chip {chip} --port /dev/ttyUSB0 --baud 460800 write_flash {args}
~/esptool/bin/python -m serial.tools.miniterm --raw --dtr 0 --rts 0 /dev/ttyUSB0 {BAUD}   # exit: Ctrl+]
#   port disappears right after plugging in (CH340)? sudo apt remove brltty

Notes / Заметки
- "Failed to connect" / timeout: board not in boot mode (see above) or a charge-only cable; try --baud 115200.
- --dtr 0 --rts 0: otherwise the terminal holds the board in reset / boot mode (ESP32-CAM-MB, NodeMCU).
- The message is printed at start (press RST after opening the terminal) and every 10 blinks.{later}
- Source: sketch/bhf_template/bhf_template.ino (board {dev['fqbn']}); this build's values: config.txt.
"""


def pi_flash_text(dev, pin):
    led = "ACT (green on-board LED)" if pin == PI_ACT else f"GPIO{pin} (BCM numbering)"
    return f"""Install on a Raspberry Pi Zero / Zero 2 W (Raspberry Pi OS with ssh): LED {led}, output in the journal.
Установка на Raspberry Pi Zero / Zero 2 W (Raspberry Pi OS с ssh): светодиод {led}, вывод в журнал.
For the Pi the "firmware" is a program: bhf_blink.py + systemd service bhf-blink (starts at boot) + install.sh.
{"" if pin == PI_ACT else _wiring(dev, pin)}
=== From your computer (Windows PowerShell, macOS, Ubuntu – ssh/scp are built in) ===
# Windows: Expand-Archive <this zip> bhf     macOS / Ubuntu: unzip <this zip> -d bhf
scp -r bhf pi@raspberrypi.local:               # user / host name as set in Raspberry Pi Imager
ssh pi@raspberrypi.local "cd bhf && sudo sh install.sh"
ssh pi@raspberrypi.local journalctl -u bhf-blink -f      # output; Ctrl+C to stop watching

=== On the Pi itself ===
unzip <this zip> -d bhf && cd bhf && sudo sh install.sh
journalctl -u bhf-blink -f

Remove / удалить: sudo sh install.sh remove   (the ACT LED returns to showing SD card activity)
Needs sudo: the LED and the service need root. Нужен sudo: светодиод и служба требуют root.
"""


def template_config_txt(dev, firmware, text, job_id, pin, signature, active_low):
    return (f"board: {dev['name']} ({dev['fqbn']})\nfirmware: {firmware} (LED toggles every {FIRMWARES[firmware]['ms']} ms)\n"
            f"pin: GPIO{pin}{' (LED lit when LOW)' if active_low else ''}\nsignature: {'yes' if signature else 'no'}\n"
            f"job: {job_id or '-'}\nmessage:\n{text}\n")


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            z.writestr(name, data)
    return buf.getvalue()


def _template(dev):
    with open(os.path.join(TEMPLATES, dev["template"]), "rb") as f:
        return f.read()


def build(device, firmware, text, job_id="", pin=None, signature=False):
    if device not in DEVICES:
        raise ValueError(f"platform '{device}' is not supported yet (supported: {', '.join(DEVICES)})")
    if firmware not in FIRMWARES:
        raise ValueError(f"unknown firmware '{firmware}'")
    text = str(text or "")[:MAX_TEXT]
    dev = DEVICES[device]
    try:
        pin = int(dev["led"] if pin in (None, "") else pin)
    except (TypeError, ValueError):
        raise ValueError("pin must be a number")
    if pin not in dev["pins"]:
        raise ValueError(f"pin {pin} is not allowed for {dev['name']} (allowed: {', '.join(map(str, dev['pins']))})")
    signature = signature is True or signature in ("1", "on", "true")
    base = f"{device}-{firmware}" + (f"-{job_id[-8:]}" if job_id else "")
    t0 = time.time()
    kind = dev["kind"]
    if kind == "avr":
        return _build_avr(dev, device, firmware, text, job_id, pin, signature, base, t0)
    if kind == "linux":
        script = pi_files(firmware, text, job_id, pin, signature)
        files = [("bhf_blink.py", script), ("bhf-blink.service", PI_SERVICE), ("install.sh", PI_INSTALL),
                 ("FLASH.txt", pi_flash_text(dev, pin))]
        info = {"kind": kind, "total_s": round(time.time() - t0, 2)}
        _state["builds"] += 1
        return base + ".zip", _zip(files), info
    active_low = pin in dev["active_low"]
    block = cfg_block(firmware, text, job_id, pin, signature, active_low)
    src_path = os.path.join(TEMPLATES, "src/bhf_template/bhf_template.ino")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    common = [("sketch/bhf_template/bhf_template.ino", src),
              ("config.txt", template_config_txt(dev, firmware, text, job_id, pin, signature, active_low))]
    if kind == "uf2":
        name = base + ".uf2"
        fw = uf2_patch(_template(dev), block)
        files = [(name, fw), ("FLASH.txt", uf2_flash_text(name, dev, pin))]
    elif kind == "esp8266":
        name = base + ".bin"
        fw = esp8266_patch(_template(dev), block)
        files = [(name, fw), ("FLASH.txt", esp_flash_text(name, dev, pin))]
    elif kind == "esp32":
        name = base + ".bin"
        fw = esp32_patch(_template(dev), block)
        d = os.path.dirname(os.path.join(TEMPLATES, dev["template"]))
        files = [(name, fw), ("FLASH.txt", esp_flash_text(name, dev, pin))]
        for extra in ("bootloader.bin", "partitions.bin", "boot_app0.bin"):
            with open(os.path.join(d, extra), "rb") as f:
                files.append((extra, f.read()))
    else:
        raise ValueError(f"unknown board kind {kind}")
    _state["builds"] += 1
    info = {"kind": kind, "image_bytes": len(fw), "sha256": hashlib.sha256(fw).hexdigest()[:16],
            "total_s": round(time.time() - t0, 2)}
    return base + ".zip", _zip(files + common), info


def _build_avr(dev, device, firmware, text, job_id, pin, signature, base, t0):
    tc = toolchain()
    t1 = time.time()
    lib = core_lib(tc, dev)
    t2 = time.time()
    work = f"/tmp/bhf-build-{os.getpid()}-{threading.get_ident()}-{int(t0 * 1000)}"
    os.makedirs(work)
    try:
        src = sketch(firmware, text, job_id, pin, signature, dev)
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
        limit = 32256 if device == "arduino_uno" else 30720            # Nano: old 2 KB bootloader
        if flash > limit:
            raise RuntimeError(f"firmware too large: {flash} bytes > {limit} (flash minus bootloader)")
        with open(work + "/fw.hex", "rb") as f:
            hexdata = f.read()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    hexname = base + ".hex"
    data = _zip([(hexname, hexdata), ("FLASH.txt", flash_text(hexname, dev, pin)), ("sketch/bhf_blink/bhf_blink.ino", src)])
    _state["builds"] += 1
    info = {"kind": "avr", "flash_bytes": flash, "ram_bytes": ram, "hex_sha256": hashlib.sha256(hexdata).hexdigest()[:16],
            "toolchain_s": round(t1 - t0, 1), "core_s": round(t2 - t1, 1), "total_s": round(time.time() - t0, 1)}
    return base + ".zip", data, info


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
                                 job.get("pin"), job.get("signature", False))
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
                         "pins": {k: {"pins": v["pins"], "default": v["led"]} for k, v in DEVICES.items()},
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
                                     pin=b.get("pin"), signature=b.get("signature", False))
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
