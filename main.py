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
# "session" = run inside an X or Wayland session, one fullscreen window per
#             screen. Required for two screens: on bare KMS only one process
#             can hold DRM master, so a second mpv cannot start.
# "drm"     = bare KMS, no session needed. Single screen only.
OUTPUT_MODE = "session"

# Bare-KMS connector names, used by OUTPUT_MODE = "drm".
# None = let mpv probe (vc4 is not always card0).
DRM_DEVICE = None
DRM_CONNECTOR_1 = "HDMI-A-1"
DRM_CONNECTOR_2 = "HDMI-A-2"

# Screen indices for OUTPUT_MODE = "session", in xrandr/compositor order.
SCREEN_1 = 0
SCREEN_2 = 1

# Xorg clones all outputs at 0,0 by default. Lay them out side by side so the
# --fs-screen indices above address different physical screens.
ARRANGE_SCREENS = True

# None = mpv default. Force the jack with "alsa/sysdefault:CARD=Headphones".
# List options with: mpv --audio-device=help
AUDIO_DEVICE = None

RESTART_DELAY = 2.0  # seconds to wait before respawning a dead player

# "auto-safe" probes every backend, which logs harmless "Cannot load
# libcuda.so.1" / "libvdpau_vc4.so" misses on a Pi. "v4l2m2m-copy" targets the
# Pi's decoder directly; "no" forces software decoding.
HWDEC = "auto-safe"

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


def connected_outputs():
    """X output names with a display attached, e.g. ['HDMI-1', 'HDMI-2']."""
    result = subprocess.run(
        ["xrandr", "--query"], capture_output=True, text=True, check=True
    )
    names = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "connected":
            names.append(fields[0])
    return names


def arrange_screens():
    """Place the connected outputs left-to-right instead of cloned."""
    if not os.environ.get("DISPLAY"):
        return  # Wayland compositors handle their own layout.
    try:
        outputs = connected_outputs()
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Warning: could not run xrandr ({error}).", flush=True)
        return

    print(f"Outputs: {', '.join(outputs) or 'none'}", flush=True)
    if len(outputs) < 2:
        print("Warning: fewer than two screens detected.", flush=True)
    if not outputs:
        return

    command = ["xrandr", "--output", outputs[0], "--auto", "--pos", "0x0"]
    for left, right in zip(outputs, outputs[1:]):
        command += ["--output", right, "--auto", "--right-of", left]
    if DEBUG:
        print(f"  {shlex.join(command)}", flush=True)
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Warning: xrandr layout failed ({error}).", flush=True)


def video_command(path, connector, screen):
    command = COMMON + [
        "--no-audio",
        "--fullscreen",
        f"--hwdec={HWDEC}",
    ]
    if OUTPUT_MODE == "drm":
        check_connector(connector)
        command += [
            "--vo=gpu",
            "--gpu-context=drm",
            f"--drm-connector={connector}",
        ]
        # Leave --drm-device unset so mpv probes for the card that actually has
        # connectors (vc4 is often card1, while card0 is the v3d render node).
        if DRM_DEVICE:
            command.append(f"--drm-device={DRM_DEVICE}")
    else:
        command += [
            f"--fs-screen={screen}",
            "--no-border",
            "--ontop",
            "--cursor-autohide=always",
        ]
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


if OUTPUT_MODE == "session" and not (
    os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
):
    sys.exit(
        "OUTPUT_MODE is 'session' but no DISPLAY/WAYLAND_DISPLAY is set.\n"
        "Start it from a session, e.g.  startx $(pwd)/main.py\n"
        "Or set OUTPUT_MODE = 'drm' for a single screen."
    )

if OUTPUT_MODE == "session" and ARRANGE_SCREENS:
    arrange_screens()

players = [
    Player("video screen 1", video_command(VIDEO_HDMI_1, DRM_CONNECTOR_1, SCREEN_1)),
    Player("video screen 2", video_command(VIDEO_HDMI_2, DRM_CONNECTOR_2, SCREEN_2)),
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
