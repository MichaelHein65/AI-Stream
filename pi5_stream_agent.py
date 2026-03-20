#!/usr/bin/env python3
"""Controller running on the Pi5 to play a network stream on a Bluetooth sink."""

from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

RUNTIME_DIR = Path.home() / ".ai-stream"
STATE_FILE = RUNTIME_DIR / "state.json"
PID_FILE = RUNTIME_DIR / "controller.pid"
A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"

STOP_REQUESTED = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def load_state() -> dict:
    default = {
        "status": "idle",
        "running": False,
        "message": "Nichts aktiv.",
        "speaker_id": "",
        "speaker_name": "",
        "device_name": "",
        "speaker_mac": "",
        "stream_url": "",
        "reason": "",
        "updated_at": "",
        "controller_pid": None,
        "player_pid": None,
    }
    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    if not isinstance(payload, dict):
        return default
    default.update(payload)
    return default


def write_state(status: str, message: str, **extra: object) -> dict:
    payload = load_state()
    payload.update(
        {
            "status": status,
            "running": status in {"starting", "running"},
            "message": message,
            "updated_at": utc_now(),
        }
    )
    payload.update(extra)
    atomic_write_json(STATE_FILE, payload)
    return payload


def read_pid() -> int | None:
    try:
        value = PID_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def write_pid(pid: int) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid), encoding="utf-8")


def clear_pid() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


def pid_is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def run_command(
    args: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, check=False, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Befehl lief in Timeout: {' '.join(args)}") from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or "").strip() or (completed.stdout or "").strip() or f"Exit-Code {completed.returncode}"
        raise RuntimeError(detail)
    return completed


def require_commands() -> None:
    for binary in ("bluetoothctl", "ffmpeg", "busctl", "bluealsa-cli", "systemctl"):
        if not shutil.which(binary):
            raise RuntimeError(f"{binary} ist auf dem Pi5 nicht verfuegbar.")


def prepare_audio_stack() -> None:
    # BlueALSA works reliably here when user-level PipeWire Bluetooth endpoints are out of the way.
    run_command(
        [
            "systemctl",
            "--user",
            "stop",
            "wireplumber.service",
            "pipewire-pulse.service",
            "pipewire.service",
            "pipewire-pulse.socket",
            "pipewire.socket",
        ],
        check=False,
        timeout=10,
    )


def bluetooth_info(mac: str) -> str:
    return run_command(["bluetoothctl", "info", mac], check=False).stdout


def bluetooth_connected(mac: str) -> bool:
    return "Connected: yes" in bluetooth_info(mac)


def connect_bluetooth(mac: str, device_name: str, timeout_seconds: int = 35) -> None:
    deadline = time.time() + timeout_seconds
    last_detail = ""
    while time.time() < deadline:
        if bluetooth_connected(mac):
            return
        completed = run_command(["bluetoothctl", "connect", mac], check=False, timeout=8)
        last_detail = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        lowered = last_detail.lower()
        if completed.returncode == 0 and "connection successful" in lowered:
            time.sleep(1)
            if bluetooth_connected(mac):
                return
        if "br-connection-profile-unavailable" in lowered or "protocol not available" in lowered:
            raise RuntimeError(f"Bluetooth-Audioprofil fuer {device_name} ist auf dem Pi5 nicht verfuegbar.")
        time.sleep(2)
    raise RuntimeError(f"Bluetooth-Verbindung zu {device_name} konnte nicht aufgebaut werden. {last_detail}".strip())


def connect_a2dp_profile(mac: str, device_name: str, timeout_seconds: int = 20) -> None:
    object_path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"
    deadline = time.time() + timeout_seconds
    last_detail = ""
    while time.time() < deadline:
        completed = run_command(
            [
                "busctl",
                "--system",
                "call",
                "org.bluez",
                object_path,
                "org.bluez.Device1",
                "ConnectProfile",
                "s",
                A2DP_SINK_UUID,
            ],
            check=False,
            timeout=10,
        )
        last_detail = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        if completed.returncode == 0 and bluetooth_connected(mac):
            return
        lowered = last_detail.lower()
        if "br-connection-profile-unavailable" in lowered or "protocol not available" in lowered:
            raise RuntimeError(f"Bluetooth-Audioprofil fuer {device_name} ist auf dem Pi5 nicht verfuegbar.")
        time.sleep(1)
    raise RuntimeError(f"A2DP-Profil fuer {device_name} konnte nicht verbunden werden. {last_detail}".strip())


def bluealsa_pcm_path(mac: str) -> str | None:
    completed = run_command(["bluealsa-cli", "list-pcms"], check=False)
    needle = f"/dev_{mac.replace(':', '_')}/a2dpsrc/sink"
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if needle in line:
            return line
    return None


def wait_for_bluealsa_pcm(mac: str, timeout_seconds: int = 20) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        pcm_path = bluealsa_pcm_path(mac)
        if pcm_path:
            return pcm_path
        time.sleep(1)
    raise RuntimeError("BlueALSA-A2DP-PCM ist nicht erschienen.")


def launch_player(stream_url: str, speaker_mac: str) -> subprocess.Popen[bytes]:
    pcm_name = f"bluealsa:DEV={speaker_mac},PROFILE=a2dp"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-vn",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-i",
        stream_url,
        "-f",
        "alsa",
        pcm_name,
    ]
    return subprocess.Popen(command, start_new_session=True)


def stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def current_status() -> dict:
    payload = load_state()
    pid = read_pid()
    running = pid_is_alive(pid)
    payload["controller_pid"] = pid
    payload["running"] = running and payload.get("status") in {"starting", "running"}
    if not running and payload.get("status") in {"starting", "running"}:
        payload["status"] = "stopped"
        payload["message"] = "Controller ist nicht aktiv."
        payload["reason"] = payload.get("reason") or "controller_gone"
    return payload


def handle_signal(signum: int, _frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def run_controller(args: argparse.Namespace) -> int:
    global STOP_REQUESTED
    STOP_REQUESTED = False
    require_commands()

    existing_pid = read_pid()
    if pid_is_alive(existing_pid):
        raise RuntimeError(f"Ein Controller laeuft bereits mit PID {existing_pid}.")

    write_pid(os.getpid())
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    player: subprocess.Popen[bytes] | None = None
    final_status = "stopped"
    final_message = "Wiedergabe beendet."
    final_reason = "finished"

    try:
        write_state(
            "starting",
            f"Verbinde {args.device_name} und bereite die Wiedergabe vor.",
            speaker_id=args.speaker_id,
            speaker_name=args.speaker_name,
            device_name=args.device_name,
            speaker_mac=args.speaker_mac,
            stream_url=args.stream_url,
            reason="starting",
            controller_pid=os.getpid(),
            player_pid=None,
        )

        prepare_audio_stack()
        connect_bluetooth(args.speaker_mac, args.device_name)
        connect_a2dp_profile(args.speaker_mac, args.device_name)
        pcm_path = wait_for_bluealsa_pcm(args.speaker_mac)

        player = launch_player(args.stream_url, args.speaker_mac)
        write_state(
            "running",
            f"Stream laeuft auf {args.speaker_name}.",
            speaker_id=args.speaker_id,
            speaker_name=args.speaker_name,
            device_name=args.device_name,
            speaker_mac=args.speaker_mac,
            stream_url=args.stream_url,
            pcm_path=pcm_path,
            reason="running",
            controller_pid=os.getpid(),
            player_pid=player.pid,
        )

        disconnect_since: float | None = None
        while True:
            if STOP_REQUESTED:
                final_message = "Wiedergabe manuell gestoppt."
                final_reason = "manual_stop"
                break

            if player.poll() is not None:
                final_message = f"Player wurde beendet (Code {player.returncode})."
                final_reason = "player_exited"
                break

            still_connected = bluetooth_connected(args.speaker_mac) and bluealsa_pcm_path(args.speaker_mac)
            if still_connected:
                disconnect_since = None
            else:
                if disconnect_since is None:
                    disconnect_since = time.time()
                elif time.time() - disconnect_since >= 8:
                    final_message = f"Bluetooth-Verbindung zu {args.speaker_name} ist abgebrochen."
                    final_reason = "bluetooth_disconnected"
                    break

            time.sleep(2)

    except Exception as exc:  # noqa: BLE001
        final_status = "error"
        final_message = str(exc)
        final_reason = "error"
        return_code = 1
    else:
        return_code = 0
    finally:
        stop_process(player)
        clear_pid()
        write_state(
            final_status,
            final_message,
            speaker_id=args.speaker_id,
            speaker_name=args.speaker_name,
            device_name=args.device_name,
            speaker_mac=args.speaker_mac,
            stream_url=args.stream_url,
            reason=final_reason,
            controller_pid=None,
            player_pid=None,
        )
    return return_code


def stop_controller() -> int:
    pid = read_pid()
    if not pid_is_alive(pid):
        clear_pid()
        payload = write_state("stopped", "Keine laufende Wiedergabe.", reason="already_stopped", controller_pid=None, player_pid=None)
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 10
    while time.time() < deadline:
        if not pid_is_alive(pid):
            break
        time.sleep(0.5)
    if pid_is_alive(pid):
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)

    clear_pid()
    payload = current_status()
    payload.update(
        {
            "status": "stopped",
            "running": False,
            "message": "Wiedergabe gestoppt.",
            "reason": "manual_stop",
            "controller_pid": None,
            "player_pid": None,
            "updated_at": utc_now(),
        }
    )
    atomic_write_json(STATE_FILE, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def status_controller() -> int:
    print(json.dumps(current_status(), ensure_ascii=False))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pi5 stream controller")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--speaker-id", required=True)
    run_parser.add_argument("--speaker-name", required=True)
    run_parser.add_argument("--device-name", required=True)
    run_parser.add_argument("--speaker-mac", required=True)
    run_parser.add_argument("--stream-url", required=True)

    subparsers.add_parser("stop")
    subparsers.add_parser("status")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "run":
        return run_controller(args)
    if args.command == "stop":
        return stop_controller()
    return status_controller()


if __name__ == "__main__":
    sys.exit(main())
