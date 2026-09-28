#!/usr/bin/env python3
"""Loop a video on each HDMI output and an audio track, forever.

Spawns three mpv processes (one per HDMI connector, one for audio) and keeps
them alive until Ctrl+C / SIGTERM.
"""

import glob
import os
import shlex
import signal
import subprocess
import sys
import time

# --- Media files (placeholders) --------------------------------------------
VIDEO_HDMI_1 = "/mnt/usb/video/1/1.mp4"
VIDEO_HDMI_2 = "/mnt/usb/video/1/2.mp4"
AUDIO_TRACK = "/mnt/usb/audio.mp3"

# --- Output configuration --------------------------------------------------
# None = find the card that owns the connector (vc4 is not always card0).
DRM_DEVICE = None
DRM_CONNECTOR_1 = "HDMI-A-1"
DRM_CONNECTOR_2 = "HDMI-A-2"

# None = mpv default. Force the jack with "alsa/sysdefault:CARD=Headphones".
# List options with: mpv --audio-device=help
AUDIO_DEVICE = None

RESTART_DELAY = 2.0  # seconds to wait before respawning a dead player

# Run as `DEBUG=1 python3 main.py` to let mpv print its errors instead of
# staying silent.
DEBUG = bool(os.environ.get("DEBUG"))

COMMON = [
    "mpv",
    "--no-config",
    "--loop-file=inf",
    "--no-input-default-bindings",
    "--no-osc",
    "--no-osd-bar",
]

if DEBUG:
    COMMON += ["--msg-level=all=v"]
else:
    COMMON += ["--really-quiet", "--no-terminal"]


def check_connector(connector):
    """Warn if the connector is missing or has nothing plugged into it."""
    entries = glob.glob(f"/sys/class/drm/card*-{connector}")
    if not entries:
        found = sorted(
            os.path.basename(p).split("-", 1)[1]
            for p in glob.glob("/sys/class/drm/card*-*")
        )
        print(
            f"Warning: no DRM connector named {connector}. Found: {', '.join(found)}",
            flush=True,
        )
        return
    try:
        with open(os.path.join(entries[0], "status")) as handle:
            status = handle.read().strip()
    except OSError:
        return
    if status != "connected":
        print(f"Warning: {connector} reports '{status}'.", flush=True)


def video_command(path, connector):
    check_connector(connector)
    command = COMMON + [
        "--no-audio",
        "--fullscreen",
        "--vo=gpu",
        "--gpu-context=drm",
        "--hwdec=auto-safe",
        f"--drm-connector={connector}",
    ]
    # Leave --drm-device unset so mpv probes for the card that actually has
    # connectors (vc4 is often card1, while card0 is the v3d render node).
    if DRM_DEVICE:
        command.append(f"--drm-device={DRM_DEVICE}")
    command.append(path)
    return command


def audio_command(path):
    cmd = COMMON + ["--no-video"]
    if AUDIO_DEVICE:
        cmd.append(f"--audio-device={AUDIO_DEVICE}")
    cmd.append(path)
    return cmd


class Player:
    def __init__(self, name, command):
        self.name = name
        self.command = command
        self.process = None

    def start(self):
        print(f"Starting {self.name}...", flush=True)
        if DEBUG:
            print(f"  {shlex.join(self.command)}", flush=True)
        self.process = subprocess.Popen(self.command)

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        if not self.is_running():
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


players = [
    Player(f"video {DRM_CONNECTOR_1}", video_command(VIDEO_HDMI_1, DRM_CONNECTOR_1)),
    Player(f"video {DRM_CONNECTOR_2}", video_command(VIDEO_HDMI_2, DRM_CONNECTOR_2)),
    # Player("audio", audio_command(AUDIO_TRACK)),
]

running = True


def shutdown(_signum, _frame):
    global running
    running = False


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

try:
    for player in players:
        player.start()

    print("Playing. Ctrl+C to quit.", flush=True)

    while running:
        for player in players:
            if not player.is_running() and running:
                code = player.process.returncode
                print(f"{player.name} exited (code {code}), restarting.", flush=True)
                time.sleep(RESTART_DELAY)
                player.start()
        time.sleep(0.5)
finally:
    print("\nStopping players...", flush=True)
    for player in players:
        player.stop()
    sys.exit(0)
