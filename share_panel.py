"""Run the local panel behind a temporary, password-protected Cloudflare URL."""
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".hosting"


def main():
    STATE.mkdir(exist_ok=True)
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 8080))
        except OSError:
            raise SystemExit("Port 8080 is busy. Stop the existing panel before sharing it.")
    binary = ROOT / "tools" / "cloudflared.exe"
    if not binary.is_file():
        raise SystemExit("Missing tools/cloudflared.exe")
    password_file = STATE / "password.txt"
    if not password_file.exists():
        with password_file.open("x", encoding="utf-8") as file:
            file.write(secrets.token_urlsafe(24) + "\n")
    password = password_file.read_text(encoding="utf-8").strip()
    if len(password) < 16:
        raise SystemExit("The sharing password must contain at least 16 characters.")
    stop_file = STATE / "stop"
    stop_file.unlink(missing_ok=True)
    config = STATE / "tunnel.yml"
    config.write_text("protocol: http2\n", encoding="utf-8")
    children = []
    status = {"running": False}
    try:
        with (STATE / "tunnel.log").open("w", encoding="utf-8") as tunnel_log, (STATE / "panel.log").open("w", encoding="utf-8") as panel_log:
            tunnel = subprocess.Popen([str(binary), "--no-autoupdate", "tunnel", "--config", str(config),
                "--url", "http://127.0.0.1:8080", "--protocol", "http2"],
                cwd=ROOT, stdout=tunnel_log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            children.append(tunnel)
            origin = None
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and tunnel.poll() is None and not stop_file.exists():
                content = (STATE / "tunnel.log").read_text(encoding="utf-8", errors="replace")
                match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", content)
                if match:
                    origin = match.group(0)
                    break
                time.sleep(.5)
            if not origin:
                raise RuntimeError("Cloudflare did not provide a URL. See .hosting/tunnel.log")
            env = os.environ.copy()
            env.update(PANEL_PUBLIC_ORIGIN=origin, PANEL_PASSWORD=password, PYTHONUTF8="1")
            panel = subprocess.Popen([sys.executable, str(ROOT / "web_panel.py")], cwd=ROOT, env=env,
                stdout=panel_log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            children.append(panel)
            status = {"running": True, "url": origin, "supervisor_pid": os.getpid(),
                      "panel_pid": panel.pid, "tunnel_pid": tunnel.pid}
            (STATE / "status.json").write_text(json.dumps(status), encoding="utf-8")
            (STATE / "address.txt").write_text(origin + "\n", encoding="utf-8")
            print("Panel URL: " + origin, flush=True)
            print("Password is stored in .hosting/password.txt", flush=True)
            print("Use stop-sharing.cmd to stop sharing.", flush=True)
            while not stop_file.exists():
                if any(child.poll() is not None for child in children):
                    raise RuntimeError("Panel or tunnel exited. See .hosting logs.")
                time.sleep(1)
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        status["running"] = False
        (STATE / "status.json").write_text(json.dumps(status), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
