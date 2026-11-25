// Vercel Go function: /api/go
//
// GET  /api/go              -> HTML page with a form + server info
// POST /api/go (form text)  -> same page with the processed text
// GET  /api/go?format=json  -> server info as JSON (also with ?text=...)
//
// Nothing from the request is ever executed: the text is only escaped and echoed.
// System commands (df, ifconfig) run with fixed arguments, if the binary exists.
package handler

import (
	"context"
	"encoding/json"
	"html/template"
	"net"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
)

const maxText = 2000

type Disk struct {
	Path    string  `json:"path"`
	TotalMB uint64  `json:"total_mb"`
	UsedMB  uint64  `json:"used_mb"`
	FreeMB  uint64  `json:"free_mb"`
	FreePct float64 `json:"free_pct"`
	Error   string  `json:"error,omitempty"`
}

type Iface struct {
	Name  string   `json:"name"`
	Addrs []string `json:"addrs"`
	MAC   string   `json:"mac,omitempty"`
	Flags string   `json:"flags"`
}

type Info struct {
	Runtime    string  `json:"runtime"`
	Hostname   string  `json:"hostname"`
	Region     string  `json:"region"`
	TimeUTC    string  `json:"time_utc"`
	Loadavg    string  `json:"loadavg"`
	CPUs       int     `json:"cpus"`
	Disk       []Disk  `json:"disk"`
	Interfaces []Iface `json:"interfaces"`
	Df         string  `json:"df"`
	Ifconfig   string  `json:"ifconfig"`
}

type Result struct {
	Text     string `json:"text"`
	Length   int    `json:"length"`
	Words    int    `json:"words"`
	Upper    string `json:"upper"`
	Reversed string `json:"reversed"`
}

func disk(path string) Disk {
	var s syscall.Statfs_t
	if err := syscall.Statfs(path, &s); err != nil {
		return Disk{Path: path, Error: err.Error()}
	}
	bs := uint64(s.Bsize)
	total, free := s.Blocks*bs, s.Bavail*bs
	d := Disk{Path: path, TotalMB: total >> 20, UsedMB: (total - s.Bfree*bs) >> 20, FreeMB: free >> 20}
	if total > 0 {
		d.FreePct = float64(int(float64(free)*1000/float64(total))) / 10
	}
	return d
}

func loadavg() string {
	b, err := os.ReadFile("/proc/loadavg")
	if err != nil {
		return err.Error()
	}
	f := strings.Fields(string(b))
	if len(f) >= 3 {
		return strings.Join(f[:3], " ")
	}
	return strings.TrimSpace(string(b))
}

func interfaces() []Iface {
	var out []Iface
	ifs, err := net.Interfaces()
	if err != nil {
		return out
	}
	for _, i := range ifs {
		x := Iface{Name: i.Name, MAC: i.HardwareAddr.String(), Flags: i.Flags.String(), Addrs: []string{}}
		if addrs, err := i.Addrs(); err == nil {
			for _, a := range addrs {
				x.Addrs = append(x.Addrs, a.String())
			}
		}
		out = append(out, x)
	}
	return out
}

func run(name string, args ...string) string {
	if _, err := exec.LookPath(name); err != nil {
		return "(" + name + ": not available in this runtime)"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	b, err := exec.CommandContext(ctx, name, args...).CombinedOutput()
	if err != nil && len(b) == 0 {
		return "(" + name + ": " + err.Error() + ")"
	}
	return strings.TrimSpace(string(b))
}

func info() Info {
	host, _ := os.Hostname()
	region := os.Getenv("VERCEL_REGION")
	if region == "" {
		region = "?"
	}
	ifc := ""
	if _, err := exec.LookPath("ifconfig"); err == nil {
		ifc = run("ifconfig")
	} else {
		ifc = run("ip", "addr")
	}
	return Info{
		Runtime:    "Go " + runtime.Version() + " " + runtime.GOOS + "/" + runtime.GOARCH,
		Hostname:   host,
		Region:     region,
		TimeUTC:    time.Now().UTC().Format("2006-01-02 15:04:05"),
		Loadavg:    loadavg(),
		CPUs:       runtime.NumCPU(),
		Disk:       []Disk{disk("/"), disk("/tmp")},
		Interfaces: interfaces(),
		Df:         run("df", "-h"),
		Ifconfig:   ifc,
	}
}

func process(t string) *Result {
	if t == "" {
		return nil
	}
	if utf8.RuneCountInString(t) > maxText {
		t = string([]rune(t)[:maxText])
	}
	r := []rune(t)
	for i, j := 0, len(r)-1; i < j; i, j = i+1, j-1 {
		r[i], r[j] = r[j], r[i]
	}
	return &Result{Text: t, Length: utf8.RuneCountInString(t), Words: len(strings.Fields(t)),
		Upper: strings.ToUpper(t), Reversed: string(r)}
}

var page = template.Must(template.New("p").Parse(`<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Go backend</title>
<link rel="stylesheet" href="/style.css"></head><body>
<p><a href="/">← все backend</a> · <a href="/api/py">Python</a> · <a href="?format=json">JSON</a></p>
<h1>🐹 {{.I.Runtime}}</h1>
<form method="post"><input name="text" maxlength="2000" placeholder="Введите текст" autofocus>
<button>Отправить</button></form>
{{with .R}}<h2>Ответ</h2><table><tr><th>Текст</th><td>{{.Text}}</td></tr>
<tr><th>Символов / слов</th><td>{{.Length}} / {{.Words}}</td></tr>
<tr><th>UPPER</th><td>{{.Upper}}</td></tr><tr><th>Наоборот</th><td>{{.Reversed}}</td></tr></table>{{end}}
<h2>Сервер</h2><table>
<tr><th>Host</th><td>{{.I.Hostname}}</td></tr><tr><th>Region</th><td>{{.I.Region}}</td></tr>
<tr><th>Время UTC</th><td>{{.I.TimeUTC}}</td></tr>
<tr><th>loadavg</th><td>{{.I.Loadavg}}</td></tr><tr><th>CPU</th><td>{{.I.CPUs}}</td></tr></table>
<h2>Диск</h2><table><tr><th>Путь</th><th>Свободно</th><th>Всего</th><th>Свободно %</th></tr>
{{range .I.Disk}}<tr><td>{{.Path}}</td><td>{{.FreeMB}} MB</td><td>{{.TotalMB}} MB</td><td>{{if .Error}}{{.Error}}{{else}}{{.FreePct}}%{{end}}</td></tr>{{end}}</table>
<h2>Сеть</h2><table><tr><th>Интерфейс</th><th>Адреса</th><th>MAC</th><th>Flags</th></tr>
{{range .I.Interfaces}}<tr><td>{{.Name}}</td><td>{{range .Addrs}}{{.}}<br>{{end}}</td><td>{{.MAC}}</td><td>{{.Flags}}</td></tr>{{end}}</table>
<h2>df -h</h2><pre>{{.I.Df}}</pre><h2>ifconfig / ip addr</h2><pre>{{.I.Ifconfig}}</pre>
</body></html>`))

// Handler is the entry point Vercel calls for /api/go.
func Handler(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
	text, format := r.URL.Query().Get("text"), r.URL.Query().Get("format")
	if r.Method == http.MethodPost {
		if strings.HasPrefix(r.Header.Get("Content-Type"), "application/json") {
			var in struct {
				Text string `json:"text"`
			}
			_ = json.NewDecoder(r.Body).Decode(&in)
			text, format = in.Text, "json"
		} else {
			_ = r.ParseForm()
			text = r.PostFormValue("text")
		}
	}
	res, i := process(text), info()
	w.Header().Set("Cache-Control", "no-store")
	if format == "json" {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		enc := json.NewEncoder(w)
		enc.SetIndent("", "  ")
		_ = enc.Encode(map[string]any{"input": res, "server": i})
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_ = page.Execute(w, map[string]any{"R": res, "I": i})
}
