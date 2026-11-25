// Vercel Go function: /api/gosys
//
// GET /api/gosys          -> system info (CPU, GPU, RAM, disk, OS) as JSON
// GET /api/gosys?bench=1  -> the same + a small benchmark: sha256, prime sieve, memory copy, /tmp write/read
package handler

import (
	"bufio"
	"crypto/rand"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const sysMB = 1 << 20

func sysRead(p string) string {
	b, err := os.ReadFile(p)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

func sysKV(p string) map[string]string {
	out := map[string]string{}
	f, err := os.Open(p)
	if err != nil {
		return out
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64<<10), 1<<20)
	for sc.Scan() {
		k, v, ok := strings.Cut(sc.Text(), ":")
		if !ok {
			continue
		}
		k = strings.TrimSpace(k)
		if _, seen := out[k]; !seen {
			out[k] = strings.TrimSpace(v)
		}
	}
	return out
}

func sysKBtoMB(s string) any {
	f := strings.Fields(s)
	if len(f) == 0 {
		return nil
	}
	n, err := strconv.Atoi(f[0])
	if err != nil {
		return nil
	}
	return n / 1024
}

func sysCPU() map[string]any {
	info := sysKV("/proc/cpuinfo")
	flags := " " + info["flags"] + " "
	var feats []string
	for _, f := range []string{"sse4_2", "avx", "avx2", "avx512f", "aes", "sha_ni", "vaes"} {
		if strings.Contains(flags, " "+f+" ") {
			feats = append(feats, f)
		}
	}
	return map[string]any{
		"model":          info["model name"],
		"vendor":         info["vendor_id"],
		"mhz":            info["cpu MHz"],
		"logical_cpus":   runtime.NumCPU(),
		"gomaxprocs":     runtime.GOMAXPROCS(0),
		"cgroup_cpu_max": sysRead("/sys/fs/cgroup/cpu.max"),
		"hypervisor":     strings.Contains(flags, " hypervisor "),
		"features":       feats,
		"arch":           runtime.GOARCH,
	}
}

func sysRAM() map[string]any {
	m := sysKV("/proc/meminfo")
	st := sysKV("/proc/self/status")
	out := map[string]any{
		"total_mb":         sysKBtoMB(m["MemTotal"]),
		"available_mb":     sysKBtoMB(m["MemAvailable"]),
		"process_rss_mb":   sysKBtoMB(st["VmRSS"]),
		"lambda_memory_mb": os.Getenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE"),
	}
	if v, err := strconv.ParseInt(sysRead("/sys/fs/cgroup/memory.max"), 10, 64); err == nil {
		out["cgroup_limit_mb"] = v / sysMB
	} else {
		out["cgroup_limit_mb"] = sysRead("/sys/fs/cgroup/memory.max")
	}
	if v, err := strconv.ParseInt(sysRead("/sys/fs/cgroup/memory.current"), 10, 64); err == nil {
		out["cgroup_used_mb"] = v / sysMB
	}
	return out
}

func sysGPU() map[string]any {
	var found []string
	for _, g := range []string{"/dev/nvidia*", "/proc/driver/nvidia/gpus/*", "/dev/dri/*"} {
		m, _ := filepath.Glob(g)
		found = append(found, m...)
	}
	note := ""
	if len(found) == 0 {
		note = "GPU не обнаружено (serverless-функции работают только на CPU)"
		found = []string{}
	}
	return map[string]any{"found": len(found) > 0, "devices": found, "note": note}
}

func sysDisks() []map[string]any {
	var out []map[string]any
	for _, p := range []string{"/", "/tmp"} {
		var s syscall.Statfs_t
		if err := syscall.Statfs(p, &s); err != nil {
			out = append(out, map[string]any{"path": p, "error": err.Error()})
			continue
		}
		bs := uint64(s.Bsize)
		out = append(out, map[string]any{"path": p, "total_mb": s.Blocks * bs / sysMB, "free_mb": s.Bavail * bs / sysMB})
	}
	return out
}

func sysSystem() map[string]any {
	osName := "linux"
	for _, l := range strings.Split(sysRead("/etc/os-release"), "\n") {
		if v, ok := strings.CutPrefix(l, "PRETTY_NAME="); ok {
			osName = strings.Trim(v, `"`)
		}
	}
	var u syscall.Utsname
	kernel := ""
	if syscall.Uname(&u) == nil {
		var b []byte
		for _, c := range u.Release {
			if c == 0 {
				break
			}
			b = append(b, byte(c))
		}
		kernel = string(b)
	}
	var uptime any
	if f := strings.Fields(sysRead("/proc/uptime")); len(f) > 0 {
		if v, err := strconv.ParseFloat(f[0], 64); err == nil {
			uptime = math.Round(v)
		}
	}
	region := os.Getenv("VERCEL_REGION")
	if region == "" {
		region = "?"
	}
	load := strings.Fields(sysRead("/proc/loadavg"))
	if len(load) > 3 {
		load = load[:3]
	}
	return map[string]any{
		"runtime":    "Go " + runtime.Version(),
		"os":         osName,
		"kernel":     kernel,
		"uptime_s":   uptime,
		"region":     region,
		"aws_region": os.Getenv("AWS_REGION"),
		"machine":    sysRead("/sys/class/dmi/id/product_name"),
		"loadavg":    strings.Join(load, " "),
	}
}

func sysMs(d time.Duration) int64 { return d.Milliseconds() }

func sysRate(mb float64, d time.Duration) float64 {
	if d <= 0 {
		return 0
	}
	return math.Round(mb / d.Seconds())
}

func sysBench() map[string]any {
	res := map[string]any{}
	block := make([]byte, sysMB)
	_, _ = rand.Read(block)

	// 1. SHA-256 over 128 MB
	t := time.Now()
	h := sha256.New()
	for i := 0; i < 128; i++ {
		h.Write(block)
	}
	_ = h.Sum(nil)
	d := time.Since(t)
	res["sha256_128mb"] = map[string]any{"ms": sysMs(d), "mb_s": sysRate(128, d)}

	// 2. Prime sieve up to 2 000 000
	t = time.Now()
	n := 2_000_000
	a := make([]bool, n+1)
	cnt := 0
	for i := 2; i <= n; i++ {
		if !a[i] {
			cnt++
			for j := i * i; j <= n; j += i {
				a[j] = true
			}
		}
	}
	d = time.Since(t)
	res["sieve_2m"] = map[string]any{"ms": sysMs(d), "primes": cnt}

	// 3. Memory: allocate 256 MB and copy it
	t = time.Now()
	src := make([]byte, 256*sysMB)
	dst := make([]byte, len(src))
	copy(dst, src)
	d = time.Since(t)
	res["mem_alloc_copy_256mb"] = map[string]any{"ms": sysMs(d), "gb_s": math.Round(25/d.Seconds()) / 100}
	src, dst = nil, nil

	// 4. Disk: write 64 MB to /tmp with fsync, read back
	path := fmt.Sprintf("/tmp/bench-%d.bin", os.Getpid())
	defer os.Remove(path)
	t = time.Now()
	f, err := os.Create(path)
	if err != nil {
		res["disk"] = map[string]any{"error": err.Error()}
		return res
	}
	for i := 0; i < 64 && err == nil; i++ {
		_, err = f.Write(block)
	}
	if err == nil {
		err = f.Sync()
	}
	f.Close()
	d = time.Since(t)
	if err != nil {
		res["disk"] = map[string]any{"error": err.Error()}
		return res
	}
	res["disk_write_64mb"] = map[string]any{"ms": sysMs(d), "mb_s": sysRate(64, d)}
	t = time.Now()
	if f, err = os.Open(path); err == nil {
		_, _ = io.Copy(io.Discard, f)
		f.Close()
	}
	d = time.Since(t)
	res["disk_read_64mb"] = map[string]any{"ms": sysMs(d), "mb_s": sysRate(64, d), "note": "из кеша ОС"}
	return res
}

// Sysinfo is the entry point Vercel calls for /api/gosys.
func Sysinfo(w http.ResponseWriter, r *http.Request) {
	t := time.Now()
	data := map[string]any{"lang": "go", "system": sysSystem(), "cpu": sysCPU(), "ram": sysRAM(), "gpu": sysGPU(), "disk": sysDisks()}
	if r.URL.Query().Get("bench") == "1" {
		data["bench"] = sysBench()
	}
	data["total_ms"] = time.Since(t).Milliseconds()
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(data)
}
