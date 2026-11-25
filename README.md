# Backend test on Vercel

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

`system.html` shows both side by side and runs the same benchmark in the browser (JS) for comparison:
SHA-256 over 128 MB, prime sieve to 2 000 000, 256 MB memory alloc+copy, 64 MB write/read in `/tmp` (server only).

## Deploy
vercel.com → Add New → Project → import `DmitryPodyachev/vercel.com` → Framework preset **Other** → Deploy.
Every push to `main` redeploys. `index.html` at the root is the start page.

## Local test
- Python: `python3 api/py.py` → http://127.0.0.1:8000/
- All together with the Vercel CLI: `npx vercel dev`
