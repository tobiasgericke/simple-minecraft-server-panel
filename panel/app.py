"""mc-panel: a minimal admin panel for a single Minecraft server.

Runs as a systemd service directly on the host, because it needs systemd, the server
console (a FIFO) and the log. It has no authentication of its own: put it behind a
reverse proxy that does (see examples/Caddyfile).

All settings come from environment variables, loaded by systemd from
/etc/mc-panel/config.env (see config.example.env).
"""
import asyncio
import datetime
import gzip
import json
import os
import re
import socket
import struct
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel


def env(key: str, default: str) -> str:
    return os.environ.get(key) or default


def parse_size(s: str) -> int:
    """'2560M' / '2G' (Java -Xmx syntax) -> bytes"""
    m = re.fullmatch(r"(\d+)([kKmMgG]?)", s.strip())
    if not m:
        return 0
    return int(m[1]) * {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30}[m[2].lower()]


MC_DIR = Path(env("MC_DIR", "/opt/minecraft"))
MC_SERVICE = env("MC_SERVICE", "minecraft")
MC_PORT = int(env("MC_PORT", "25565"))
SERVER_NAME = env("SERVER_NAME", "")
HEAP_BYTES = parse_size(env("HEAP", "0"))
BACKUP_STATE = Path(env("BACKUP_STATE_DIR", "/var/lib/mc-backup")) / "last.json"
BACKUP_WARN_HOURS = float(env("BACKUP_WARN_HOURS", "48"))
BACKUP_CRIT_HOURS = float(env("BACKUP_CRIT_HOURS", "168"))
LOG = MC_DIR / "logs" / "latest.log"
FIFO = MC_DIR / "console.fifo"
LOG_BACKLOG = 300          # lines sent to the browser on connect
STATIC = Path(__file__).parent / "static"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_cpu_sample: dict = {}     # pid -> (time, cpu ticks), for CPU % between two polls


# --- CSRF protection ------------------------------------------------------------------
# The browser sends basic auth with EVERY request to this domain, even when another site
# triggers it. So state-changing requests must come from the panel page itself.
@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    if request.method != "GET":
        site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        host = request.headers.get("host")
        if site not in (None, "same-origin") or (origin and origin.split("://")[-1] != host):
            return JSONResponse({"detail": "cross-site request"}, status_code=403)
    return await call_next(request)


# --- Helpers --------------------------------------------------------------------------
async def run(*args: str, timeout: float = 150) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    return proc.returncode, out.decode(errors="replace").strip()


async def service_state() -> tuple[str, int]:
    _, out = await run("systemctl", "show", MC_SERVICE, "-p", "ActiveState,MainPID")
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return props.get("ActiveState", "unknown"), int(props.get("MainPID", "0") or 0)


def process_stats(pid: int) -> dict:
    """Memory from /proc/<pid>/status, CPU % since the previous poll from /proc/<pid>/stat."""
    if not pid:
        return {}
    try:
        status = dict(l.split(":", 1) for l in Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in l)
        kb = lambda k: int(status.get(k, "0 kB").split()[0]) * 1024
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = int(fields[11]) + int(fields[12])          # utime + stime
        start_ticks = int(fields[19])
    except (OSError, ValueError, IndexError):
        return {}
    hz = os.sysconf("SC_CLK_TCK")
    now = time.monotonic()
    cpu = None
    if pid in _cpu_sample:
        t0, k0 = _cpu_sample[pid]
        if now > t0:
            cpu = (ticks - k0) / hz / (now - t0) / os.cpu_count() * 100
    _cpu_sample.clear()
    _cpu_sample[pid] = (now, ticks)
    uptime_sys = float(Path("/proc/uptime").read_text().split()[0])
    return {"rss": kb("VmRSS"), "swap": kb("VmSwap"),
            "cpu_pct": None if cpu is None else round(cpu, 1),
            "uptime_s": int(uptime_sys - start_ticks / hz)}


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _read_varint(sock: socket.socket) -> int:
    n = shift = 0
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError
        n |= (b[0] & 0x7F) << shift
        if not b[0] & 0x80:
            return n
        shift += 7


def _motd_text(desc) -> str:
    """MOTD can be a string or a chat component tree; flatten it and drop § colour codes."""
    if isinstance(desc, str):
        text = desc
    elif isinstance(desc, dict):
        text = desc.get("text", "") + "".join(_motd_text(e) for e in desc.get("extra", []))
    elif isinstance(desc, list):
        text = "".join(_motd_text(e) for e in desc)
    else:
        text = ""
    return re.sub("§.", "", text)


def server_list_ping() -> dict | None:
    """Asks the server like the in-game multiplayer list does: players, version, MOTD.
    That way the panel doesn't have to spam `list` into the console (and the log)."""
    try:
        with socket.create_connection(("127.0.0.1", MC_PORT), timeout=2) as s:
            host = b"localhost"
            hs = _varint(0) + _varint(-1 & 0xFFFFFFFF) + _varint(len(host)) + host \
                + struct.pack(">H", MC_PORT) + _varint(1)
            s.sendall(_varint(len(hs)) + hs + b"\x01\x00")
            _read_varint(s)                     # packet length
            _read_varint(s)                     # packet id
            length = _read_varint(s)
            data = b""
            while len(data) < length:
                chunk = s.recv(length - len(data))
                if not chunk:
                    raise ConnectionError
                data += chunk
        d = json.loads(data)
        players = d.get("players", {})
        return {"online": players.get("online", 0), "max": players.get("max", 0),
                "names": sorted(p["name"] for p in players.get("sample", []) or []),
                "version": d.get("version", {}).get("name"),
                "motd": _motd_text(d.get("description", "")).split("\n")[0].strip()}
    except (OSError, ValueError, ConnectionError):
        return None


def backup_info() -> dict | None:
    try:
        d = json.loads(BACKUP_STATE.read_text())
    except (OSError, ValueError):
        return None
    t = datetime.datetime.fromisoformat(d["time"])
    d["age_s"] = int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds())
    return d


# --- API ------------------------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/status")
async def status():
    state, pid = await service_state()
    ping = await asyncio.to_thread(server_list_ping) if state == "active" else None
    return {"name": SERVER_NAME or (ping or {}).get("motd") or "Minecraft",
            "state": state, "ready": ping is not None, "players": ping,
            "process": process_stats(pid), "heap": HEAP_BYTES,
            "backup": backup_info(),
            "backup_warn_h": BACKUP_WARN_HOURS, "backup_crit_h": BACKUP_CRIT_HOURS}


@app.get("/api/players")
async def players():
    """Every player name the server has seen (usercache.json), so the log can colour them."""
    try:
        cache = json.loads((MC_DIR / "usercache.json").read_text())
        names = sorted({e["name"] for e in cache if isinstance(e, dict) and "name" in e})
    except (OSError, ValueError):
        names = []
    return {"names": names}


class Command(BaseModel):
    cmd: str


@app.post("/api/console")
async def console(c: Command):
    cmd = c.cmd.strip().lstrip("/")
    if not cmd or "\n" in cmd:
        raise HTTPException(400, "empty or multi-line command")
    state, _ = await service_state()
    if state != "active" or not FIFO.exists():
        raise HTTPException(409, "server is not running")
    def write():
        # O_NONBLOCK: fail immediately instead of hanging if nobody reads the FIFO
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, (cmd + "\n").encode())
        finally:
            os.close(fd)
    try:
        await asyncio.to_thread(write)
    except OSError as e:
        raise HTTPException(409, f"console not reachable: {e}")
    return {"ok": True}


@app.post("/api/power/{action}")
async def power(action: str):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(404)
    # the sudoers rule installed by install.sh allows exactly these three
    code, out = await run("sudo", "-n", "systemctl", action, MC_SERVICE)
    if code:
        raise HTTPException(500, out or f"systemctl {action} failed")
    return {"ok": True}


@app.get("/api/logs")
async def logs(request: Request):
    """Server-Sent Events: first the last lines, then every new line as it arrives.
    Notices when the server starts a fresh latest.log on restart."""
    async def stream():
        inode, f = None, None
        try:
            while not await request.is_disconnected():
                try:
                    st = LOG.stat()
                except FileNotFoundError:
                    await asyncio.sleep(1)
                    continue
                if st.st_ino != inode or (f and st.st_size < f.tell()):
                    if f:
                        f.close()
                        yield "event: reset\ndata: \n\n"
                    f = LOG.open("r", errors="replace")
                    first = inode is None
                    inode = st.st_ino
                    lines = f.readlines()
                    for line in lines[-LOG_BACKLOG:] if first else lines:
                        yield f"data: {line.rstrip()}\n\n"
                line = f.readline()
                if line:
                    yield f"data: {line.rstrip()}\n\n"
                else:
                    yield ": ping\n\n"      # keeps the connection open through proxies
                    await asyncio.sleep(0.5)
        finally:
            if f:
                f.close()
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


ARCHIVE_RE = re.compile(r"\d{4}-\d{2}-\d{2}-\d+\.log\.gz")   # Paper's rotated logs
ARCHIVE_MAX_LINES = 50_000


@app.get("/api/logs/archive")
async def log_archive():
    """Older, gzipped logs (newest first)."""
    files = [p for p in LOG.parent.iterdir() if ARCHIVE_RE.fullmatch(p.name)]
    files.sort(key=lambda p: (p.name[:10], int(p.name[11:].split(".")[0])), reverse=True)
    return [{"name": p.name, "size": p.stat().st_size} for p in files]


@app.get("/api/logs/archive/{name}")
async def log_archive_file(name: str):
    # Only names matching the pattern, so nobody can read ../../etc/shadow
    if not ARCHIVE_RE.fullmatch(name) or not (LOG.parent / name).is_file():
        raise HTTPException(404)
    def read():
        with gzip.open(LOG.parent / name, "rt", errors="replace") as f:
            lines = f.read().splitlines()
        return "\n".join(lines[-ARCHIVE_MAX_LINES:])
    return PlainTextResponse(await asyncio.to_thread(read))


@app.get("/api/backup/download")
async def backup_download(request: Request):
    name = "mc-" + datetime.datetime.now().strftime("%Y-%m-%d_%H%M") + ".tar.zst"
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "/usr/local/bin/mc-backup", "panel",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    def abort():
        # stop mc-backup (its trap turns autosave back on) and close the pipe,
        # otherwise tar/zstd block forever writing into a full pipe
        if proc.returncode is None:
            proc.terminate()
            proc._transport.get_pipe_transport(1).close()

    async def watch():
        # uvicorn doesn't reliably tell the generator about an aborted download
        # (it then hangs in `yield`), so check ourselves whether the browser is still there
        while proc.returncode is None:
            if await request.is_disconnected():
                abort()
                return
            await asyncio.sleep(1)

    async def stream():
        watcher = asyncio.create_task(watch())
        complete = False
        try:
            while chunk := await proc.stdout.read(1 << 20):
                yield chunk
            complete = True     # stream fully sent; mc-backup is about to write last.json
        finally:
            if not complete:
                abort()
            watcher.cancel()
            try:
                await asyncio.wait_for(proc.wait(), 30)
            except asyncio.TimeoutError:
                proc.kill()
    return StreamingResponse(stream(), media_type="application/zstd",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})
