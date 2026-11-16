# Simple Minecraft Server Panel

A minimal, self-hosted admin panel for **one** Minecraft server (Paper, Spigot, vanilla) on a
Linux box: the few things hosting panels do, without the hosting panel.

- **Status**: online/offline, players (with names), memory (incl. swap), CPU, uptime
- **Live log** in the browser, warnings and errors highlighted
- **Console**: send commands, with history (↑/↓)
- **Start / stop / restart** (stop goes through the console, so the world is saved)
- **Backups**: download a consistent backup from the browser, or let your PC fetch one every
  day; the panel shows when the last one was and turns yellow/red when it's getting old

It installs as `mc-panel` (service, user, `/etc/mc-panel/`) and is one small Python file
(FastAPI) and one HTML page, no database, no build step. Everything is configured in a
single file, `config.env`.

```
Browser ─HTTPS─▶ reverse proxy (password) ─▶ mc-panel ─┬─ systemctl (sudo, 3 commands only)
                                                       ├─ console.fifo ─▶ server console
                                                       └─ logs/latest.log
PC (daily) ─SSH─▶ sudo mc-backup ─▶ tar.zst stream ─▶ backup folder on the PC
```

## How it works

- The server runs as a **systemd service**. Its console reads from a named pipe
  (`MC_DIR/console.fifo`), so the panel, the `mc` command and the stop command can type into
  it without tmux or screen.
- The panel runs as its own systemd service and user. A sudoers rule lets it run exactly
  `systemctl start|stop|restart <server>` and `mc-backup`, nothing else.
- Player info comes from the same **server list ping** the in-game server list uses, so the
  panel doesn't spam `list` into your log.
- `mc-backup` pauses autosave, flushes the world to disk, and streams
  `tar | zstd` to **stdout**: no space is used on the server. Autosave is switched back on
  afterwards, also when the download is aborted.
- The panel has **no login of its own**. It must sit behind a reverse proxy with
  authentication (example for Caddy in [`examples/Caddyfile`](examples/Caddyfile)).
  State-changing requests are additionally checked to come from the panel page itself (CSRF).

## Requirements

- Debian/Ubuntu (uses `apt`, `systemd`, optional `needrestart`)
- Python 3.10+, a Java version that matches your server (21 for 1.21.x, 25 for 26.x)
- A reverse proxy with HTTPS and a password (Caddy, nginx, Traefik, ...)

## Install

```bash
git clone https://github.com/tobiasgericke/simple-minecraft-server-panel.git
cd simple-minecraft-server-panel
cp config.example.env config.env
nano config.env                      # name, paths, heap size, Java, ports
sudo ./install.sh config.env         # copies it to /etc/mc-panel/config.env
```

Then put your server jar into `MC_DIR` (e.g. Paper from [papermc.io](https://papermc.io/downloads/paper)),
accept the EULA (`eula=true` in `MC_DIR/eula.txt`) and start it:

```bash
sudo systemctl start minecraft       # or the Start button in the panel
```

Moving an existing server: copy its folder to `MC_DIR`, `chown -R minecraft:minecraft` it,
and use the same `JAR` name in `config.env`.

Finally point your reverse proxy at `PANEL_HOST:PANEL_PORT` with a password, see
[`examples/Caddyfile`](examples/Caddyfile). **Never** expose the panel port directly.

### Updating

```bash
git pull && sudo ./install.sh
```

`install.sh` is safe to run any time. It never restarts the Minecraft server; when something
that affects it changed (config, start script) it tells you to restart it yourself.

## Configuration

All settings live in `/etc/mc-panel/config.env`, see
[`config.example.env`](config.example.env) for every option. The most important ones:

| Setting | Meaning |
|---|---|
| `SERVER_NAME` | Name in the panel (empty = server MOTD) |
| `MC_DIR`, `JAR` | Server folder and jar |
| `JAVA`, `HEAP`, `JVM_FLAGS` | Java binary, `-Xms/-Xmx`, extra flags (Paper's recommended ones by default) |
| `PANEL_HOST`, `PANEL_PORT` | Where the panel listens (`127.0.0.1`, or `172.17.0.1` if the proxy is a Docker container) |
| `BACKUP_PATHS` | What goes into a backup (relative to `MC_DIR`, globs allowed) |
| `BACKUP_WARN_HOURS`, `BACKUP_CRIT_HOURS` | When the "last backup" turns yellow / red |

**Heap size**: leave room for the OS and everything else on the machine. If the panel shows
a lot of memory as *swapped*, the heap is too big for the box, which causes lag spikes.

## Command line

```bash
mc list              # send a console command and show the response
mc                   # follow the live log (Ctrl+C)
sudo mc-backup > backup.tar.zst
```

## Backups on your PC

The backup is created on demand and streamed over SSH, so your PC needs SSH key access to a
user that may run `sudo mc-backup` without a password (or root).

**Windows**: copy [`pc/backup.ps1`](pc/backup.ps1) to your backup folder and run it daily with
Task Scheduler (PowerShell, adjust server and folder):

```powershell
$a = New-ScheduledTaskAction -Execute powershell.exe -Argument '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "D:\MC-Backups\backup.ps1" -Server myserver -Keep 10'
$t = New-ScheduledTaskTrigger -Daily -At 20:00
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable
Register-ScheduledTask -TaskName "Minecraft Backup" -Action $a -Trigger $t -Settings $s
```

`-StartWhenAvailable` catches up on a missed run when the PC was off at 20:00.

**Linux/macOS**: [`pc/backup.sh`](pc/backup.sh) with cron:
`0 20 * * * /path/to/backup.sh myserver 10 ~/mc-backups`

Both verify the SHA256 of the download and keep the newest N backups.

**Restore**:

```bash
scp mc-<date>.tar.zst myserver:/tmp/
ssh myserver
sudo systemctl stop minecraft
cd /opt/minecraft && sudo rm -rf world
sudo tar -I zstd -xf /tmp/mc-<date>.tar.zst
sudo chown -R minecraft:minecraft . && sudo systemctl start minecraft
```

## Files

| Repo | Installed to | Purpose |
|---|---|---|
| `config.example.env` | `/etc/mc-panel/config.env` | all settings |
| `bin/mc-start` | `/usr/local/bin/` | starts Java with the console FIFO |
| `bin/mc` | `/usr/local/bin/` | console command / live log from the shell |
| `bin/mc-backup` | `/usr/local/bin/` | streams a consistent backup to stdout |
| `panel/` | `PANEL_DIR` | the web panel (FastAPI app + one HTML page) |
| `systemd/*.in` | `/etc/systemd/system/`, `/etc/sudoers.d/`, `/etc/needrestart/conf.d/` | rendered from `config.env` |
| `pc/` | your PC | daily backup download |

## License

MIT
