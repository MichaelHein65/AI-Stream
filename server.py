#!/usr/bin/env python3
"""Local control app for sending a Pi4 stream to Bluetooth speakers on Pi5."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"

PI5_HOST = "pi5"
REMOTE_DIR = "/home/pi/.ai-stream"
REMOTE_SCRIPT = f"{REMOTE_DIR}/pi5_stream_agent.py"
REMOTE_LOG = f"{REMOTE_DIR}/controller.log"

DEFAULT_SETTINGS = {
    "ssh_host": PI5_HOST,
    "stream_url": "",
    "speakers": [
        {"id": "lg_soundbar", "name": "LG Soundbar", "device_name": "LG DS77TY(1F)", "mac": "68:52:10:27:4B:1F"},
        {"id": "wonderboom", "name": "Wonderboom", "device_name": "WONDERBOOM", "mac": "C0:28:8D:CC:71:CC"},
    ],
}

SETTINGS_LOCK = threading.Lock()


class ApiError(RuntimeError):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def ensure_dirs() -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    ensure_dirs()
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        return settings
    except json.JSONDecodeError:
        return settings

    if isinstance(stored, dict):
        stream_url = stored.get("stream_url")
        ssh_host = stored.get("ssh_host")
        if isinstance(stream_url, str):
            settings["stream_url"] = stream_url.strip()
        if isinstance(ssh_host, str) and ssh_host.strip():
            settings["ssh_host"] = ssh_host.strip()
    return settings


def save_settings(settings: dict) -> None:
    ensure_dirs()
    payload = {
        "ssh_host": settings.get("ssh_host", PI5_HOST),
        "stream_url": settings.get("stream_url", "").strip(),
    }
    tmp_path = SETTINGS_FILE.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    tmp_path.replace(SETTINGS_FILE)


def validate_stream_url(value: str) -> str:
    url = value.strip()
    if not url:
        raise ApiError("Bitte eine Stream-URL eintragen.")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ApiError("Die Stream-URL muss mit http:// oder https:// beginnen.")
    return url


def get_speaker(settings: dict, speaker_id: str) -> dict:
    for speaker in settings["speakers"]:
        if speaker["id"] == speaker_id:
            return speaker
    raise ApiError("Unbekannter Lautsprecher.")


def run_local_command(args: list[str], timeout: int = 20, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or f"Exit-Code {completed.returncode}"
        raise ApiError(detail, HTTPStatus.BAD_GATEWAY)
    return completed


def ssh_shell(host: str, command: str, timeout: int = 20, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_local_command(["ssh", host, command], timeout=timeout, check=check)


def deploy_remote_agent(host: str) -> None:
    local_script = BASE_DIR / "pi5_stream_agent.py"
    if not local_script.exists():
        raise ApiError("Pi5-Controller fehlt im Projektordner.", HTTPStatus.INTERNAL_SERVER_ERROR)

    ssh_shell(host, f"mkdir -p {shlex.quote(REMOTE_DIR)}")
    run_local_command(["scp", str(local_script), f"{host}:{REMOTE_SCRIPT}"], timeout=30)
    ssh_shell(host, f"chmod 755 {shlex.quote(REMOTE_SCRIPT)}")


def remote_status(host: str) -> dict:
    fallback = {
        "status": "idle",
        "running": False,
        "message": "Pi5-Controller noch nicht bereit.",
        "speaker_id": "",
        "speaker_name": "",
        "stream_url": "",
        "reason": "",
        "updated_at": "",
        "controller_pid": None,
    }
    command = (
        f"if [ -f {shlex.quote(REMOTE_SCRIPT)} ]; then "
        f"python3 {shlex.quote(REMOTE_SCRIPT)} status; "
        f"else printf '%s' {shlex.quote(json.dumps(fallback))}; fi"
    )
    completed = ssh_shell(host, command, timeout=15, check=True)
    try:
        return json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise ApiError(f"Unerwartete Status-Antwort vom Pi5: {exc}", HTTPStatus.BAD_GATEWAY) from exc


def remote_stop(host: str) -> dict:
    command = (
        f"if [ -f {shlex.quote(REMOTE_SCRIPT)} ]; then "
        f"python3 {shlex.quote(REMOTE_SCRIPT)} stop; "
        f"else printf '%s' '{{\"status\":\"idle\",\"running\":false,\"message\":\"Nichts aktiv.\"}}'; fi"
    )
    completed = ssh_shell(host, command, timeout=20, check=True)
    try:
        return json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise ApiError(f"Unerwartete Stop-Antwort vom Pi5: {exc}", HTTPStatus.BAD_GATEWAY) from exc


def remote_start(host: str, stream_url: str, speaker: dict) -> None:
    remote_args = [
        "nohup",
        "python3",
        REMOTE_SCRIPT,
        "run",
        "--speaker-id",
        speaker["id"],
        "--speaker-name",
        speaker["name"],
        "--device-name",
        speaker["device_name"],
        "--speaker-mac",
        speaker["mac"],
        "--stream-url",
        stream_url,
    ]
    command = f"{shlex.join(remote_args)} >> {shlex.quote(REMOTE_LOG)} 2>&1 < /dev/null &"
    ssh_shell(host, command, timeout=15, check=True)


def wait_for_remote_start(host: str, speaker_id: str, timeout_seconds: float = 6.0) -> dict:
    deadline = time.time() + timeout_seconds
    last_status = remote_status(host)
    while time.time() < deadline:
        if last_status.get("speaker_id") == speaker_id and last_status.get("status") in {"starting", "running", "error"}:
            return last_status
        time.sleep(0.5)
        last_status = remote_status(host)
    return last_status


def build_state() -> dict:
    settings = load_settings()
    remote = remote_status(settings["ssh_host"])
    return {"settings": settings, "remote": remote}


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/health":
            self.respond_json({"ok": True})
            return
        if self.path == "/api/state":
            try:
                self.respond_json(build_state())
            except ApiError as exc:
                self.respond_error(exc)
            return
        if self.path in {"/", ""}:
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/settings":
            try:
                payload = self.read_json()
                with SETTINGS_LOCK:
                    settings = load_settings()
                    settings["stream_url"] = validate_stream_url(str(payload.get("stream_url", "")))
                    save_settings(settings)
                self.respond_json(build_state())
            except ApiError as exc:
                self.respond_error(exc)
            return

        if self.path == "/api/play":
            try:
                payload = self.read_json()
                with SETTINGS_LOCK:
                    settings = load_settings()
                    speaker = get_speaker(settings, str(payload.get("speaker_id", "")))
                    stream_url = validate_stream_url(str(payload.get("stream_url") or settings.get("stream_url", "")))
                    settings["stream_url"] = stream_url
                    save_settings(settings)

                host = settings["ssh_host"]
                deploy_remote_agent(host)
                remote_stop(host)
                remote_start(host, stream_url, speaker)
                remote = wait_for_remote_start(host, speaker["id"])
                self.respond_json({"settings": settings, "remote": remote})
            except ApiError as exc:
                self.respond_error(exc)
            return

        if self.path == "/api/stop":
            try:
                settings = load_settings()
                status = remote_stop(settings["ssh_host"])
                self.respond_json({"settings": settings, "remote": status})
            except ApiError as exc:
                self.respond_error(exc)
            return

        self.respond_json({"error": "Nicht gefunden."}, status=HTTPStatus.NOT_FOUND)

    def read_json(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ApiError(f"Ungueltiges JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ApiError("JSON-Objekt erwartet.")
        return payload

    def respond_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def respond_error(self, exc: ApiError) -> None:
        self.respond_json({"error": exc.message}, status=exc.status)

    def log_message(self, fmt: str, *args) -> None:
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Stream control app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8091, type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure_dirs()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"AI Stream App laeuft unter http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
