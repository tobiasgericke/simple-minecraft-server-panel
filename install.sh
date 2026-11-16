#!/usr/bin/env bash
# Installs (or updates) mc-panel: server scripts, systemd units and the web panel.
#
#   sudo ./install.sh                    # uses /etc/mc-panel/config.env
#   sudo ./install.sh path/to/config.env # copies that file to /etc/mc-panel/config.env first
#
# Safe to run again at any time. It never restarts the Minecraft server (that would kick
# players); if the server's start setup changed it tells you to restart it yourself.
# It does not download Minecraft: put your server.jar (e.g. Paper) into MC_DIR yourself.
set -euo pipefail
cd "$(dirname "$0")"
CONF=/etc/mc-panel/config.env

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }

if [ $# -ge 1 ]; then
  install -D -m 644 "$1" "$CONF"
elif [ ! -f "$CONF" ]; then
  install -D -m 644 config.example.env "$CONF"
  echo "Created $CONF from the example. Edit it, then run install.sh again." >&2
  exit 1
fi
set -a                                  # export everything, render() reads it from the environment
# shellcheck source=config.example.env
source "$CONF"
set +a

need_mc_restart=0
panel_changed=0

# Copies only when the content changed; returns 0 if it copied
put() {  # put <source> <target> <mode> [owner:group]
  if ! cmp -s "$1" "$2"; then
    install -D -m "$3" ${4:+-o "${4%%:*}" -g "${4##*:}"} "$1" "$2"
    echo "  updated: $2"
    return 0
  fi
  return 1
}

# Fills @VAR@ placeholders from the config into a temp file and prints its path
render() {
  local out
  out=$(mktemp)
  python3 - "$1" "$out" <<'PY'
import os, re, sys
src, dst = sys.argv[1:]
text = open(src).read()
def sub(m):
    if m[1] not in os.environ:
        sys.exit(f"{src}: @{m[1]}@ is not set in config.env")
    return os.environ[m[1]]
open(dst, "w").write(re.sub(r"@([A-Z_]+)@", sub, text))
PY
  echo "$out"
}

if [ -d /etc/needrestart ]; then
  echo "==> needrestart (before apt, so installing packages can't restart the server)"
  f=$(render systemd/needrestart.conf.in); put "$f" /etc/needrestart/conf.d/mc-panel.conf 644 || true; rm -f "$f"
fi

echo "==> packages"
missing=()
# shellcheck disable=SC2086
for p in $APT_PACKAGES; do dpkg -s "$p" &>/dev/null || missing+=("$p"); done
if ((${#missing[@]})); then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}" >/dev/null
  echo "  installed: ${missing[*]}"
fi
[ -x "$JAVA" ] || echo "  WARNING: JAVA=$JAVA does not exist" >&2

echo "==> users"
id "$MC_USER"    &>/dev/null || useradd -r -m -d "$MC_DIR" -s /bin/bash "$MC_USER"
id "$PANEL_USER" &>/dev/null || useradd -r -M -d "$PANEL_DIR" -s /usr/sbin/nologin "$PANEL_USER"
mkdir -p "$MC_DIR" && chown "$MC_USER:$MC_USER" "$MC_DIR"
[ -f "$MC_DIR/$JAR" ] || echo "  NOTE: $MC_DIR/$JAR is missing, put your server jar there." >&2

echo "==> server scripts"
put bin/mc-start /usr/local/bin/mc-start 755 && need_mc_restart=1
put bin/mc /usr/local/bin/mc 755 || true
put bin/mc-backup /usr/local/bin/mc-backup 755 || true
mkdir -p "$BACKUP_STATE_DIR"
# FIFO of an already running server: same mode as mc-start, so the panel can write right away
[ -p "$MC_DIR/console.fifo" ] && chmod 660 "$MC_DIR/console.fifo"
f=$(render systemd/minecraft.service.in)
put "$f" "/etc/systemd/system/$MC_SERVICE.service" 644 && need_mc_restart=1
rm -f "$f"

echo "==> panel"
put panel/app.py "$PANEL_DIR/app.py" 644 && panel_changed=1
put panel/static/index.html "$PANEL_DIR/static/index.html" 644 && panel_changed=1
if [ ! -x "$PANEL_DIR/venv/bin/uvicorn" ] || ! cmp -s panel/requirements.txt "$PANEL_DIR/requirements.txt"; then
  python3 -m venv "$PANEL_DIR/venv"
  "$PANEL_DIR/venv/bin/pip" install -q --upgrade pip
  "$PANEL_DIR/venv/bin/pip" install -q -r panel/requirements.txt
  cp panel/requirements.txt "$PANEL_DIR/requirements.txt"
  echo "  venv updated"
  panel_changed=1
fi
f=$(render systemd/sudoers.in)
visudo -cqf "$f"                        # a broken sudoers file would lock sudo for everyone
put "$f" /etc/sudoers.d/mc-panel 440 && panel_changed=1
rm -f "$f"
f=$(render systemd/mc-panel.service.in)
put "$f" /etc/systemd/system/mc-panel.service 644 && panel_changed=1
rm -f "$f"
# config.env is read at start by the panel and the server (HEAP, JVM_FLAGS, ...)
if ! cmp -s "$CONF" "$PANEL_DIR/.config.env.installed"; then
  cp "$CONF" "$PANEL_DIR/.config.env.installed"
  panel_changed=1
  need_mc_restart=1
fi

echo "==> services"
systemctl daemon-reload
systemctl enable -q "$MC_SERVICE" mc-panel
if ((panel_changed)) || ! systemctl is-active -q mc-panel; then
  systemctl restart mc-panel
  echo "  mc-panel restarted"
fi
if ! systemctl is-active -q "$MC_SERVICE" && [ -f "$MC_DIR/$JAR" ]; then
  echo "  $MC_SERVICE is not running: start it with 'systemctl start $MC_SERVICE' or in the panel"
elif ((need_mc_restart)); then
  echo
  echo "  NOTE: the server's start setup changed (config.env, mc-start or unit file)."
  echo "  It takes effect after a restart: in the panel or 'systemctl restart $MC_SERVICE'."
fi
echo "  panel: http://$PANEL_HOST:$PANEL_PORT (only reachable through your reverse proxy)"
