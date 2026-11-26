# bhf firmware builder + backend tests on Vercel

## Firmware builder – `api/build.py`
Builds Arduino firmware for [bhf.pp.ua](https://bhf.pp.ua) (repo dpodyachev/bhf): official Arduino avr-gcc 7.3 + ArduinoCore-avr 1.8.6,
downloaded once per function instance into `/tmp` (only the parts needed for ATmega328P, ~85 MB, ~8 s cold), core compiled once
per instance, then a build takes ~0.1–1 s. Result: zip with `<board>-<firmware>.hex`, `FLASH.txt` (avrdude commands for
Windows / macOS / Ubuntu) and the sketch source. The user text is embedded as a byte array (never as code).

| | |
|---|---|
| `GET /api/build` | state |
| `POST /api/build {"id"}` | job from the bhf queue: `GET bhf/api/builder/jobs/<id>` → build → `PUT …/artifact` (R2). Needs env **`BHF_BUILDER_TOKEN`** (= Pages secret `VERCEL_BUILDER_TOKEN` of project bhf); optional `BHF_URL` (default https://bhf.pp.ua) |
| `POST /api/build {"device","firmware","text"}` | direct: zip in the response (CORS for bhf.pp.ua) |

Boards: `arduino_uno`. Firmware: `blink` (LED toggles every 2 s), `blink_fast` (0.5 s); text ≤ 4000 chars → Serial at 115200 baud.

## Backend tests (`/test/`)

The same small backend in several languages, as Vercel Functions (zero config):

| Language | File | URL |
|---|---|---|
| Python | `api/py.py` (class `handler`, BaseHTTPRequestHandler) | `/api/py` |
| Python | `api/pysys.py` – system info + benchmark (JSON) | `/api/pysys`, `?bench=1` |
| Go | `api/gosys.go` (`func Sysinfo`) – same | `/api/gosys`, `?bench=1` |
| Go | `api/go.go` (`func Handler`, package `handler`, `go.mod` in root) | `/api/go` |

Each endpoint:
- `GET` – HTML page with a text form and server info;
- `POST` form field `text` – the same page plus the processed text (length, words, upper, reversed);
- `?format=json` or `POST` with `Content-Type: application/json` `{"text": "..."}` – JSON;
- server info: runtime version, region (`VERCEL_REGION`), hostname, loadavg, CPUs, free disk space of `/` and `/tmp`,
  network interfaces with addresses, plus the output of `df -h` and `ifconfig`/`ip addr` if these binaries exist
  in the runtime (on Vercel they usually do not – values are read natively instead).

The request text is never executed, only escaped and echoed; commands run with fixed arguments only.

`test/system.html` shows both side by side and runs the same benchmark in the browser (JS) for comparison:
SHA-256 over 128 MB, prime sieve to 2 000 000, 256 MB memory alloc+copy, 64 MB write/read in `/tmp` (server only).

## Deploy
vercel.com → Add New → Project → import `DmitryPodyachev/vercel.com` → Framework preset **Other** → Deploy.
Every push to `main` redeploys. `index.html` = builder page, `test/` = language tests.

## Local test
- Python: `python3 api/py.py` → http://127.0.0.1:8000/
- All together with the Vercel CLI: `npx vercel dev`
