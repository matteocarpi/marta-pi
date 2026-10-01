#!/usr/bin/env python3
"""Loop a video on each HDMI output, forever.

Spawns one mpv process per HDMI connector and keeps them alive until Ctrl+C /
SIGTERM. Sound comes from video_1's own audio track; video_2 plays muted.
"""

import glob
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

# Buttons and e-paper are both optional. A missing gpiozero, a GPIO chip that
# cannot be claimed (in a container without /dev/gpiochip0, say), or an
# unplugged panel must not stop the video playing - that is this machine's
# primary job.
try:
    from buttons import ChannelButtons
except ImportError as error:
    ChannelButtons = None
    print(f"Warning: button support unavailable ({error}).", flush=True)

try:
    from epaper import EPaper
except ImportError as error:
    EPaper = None
    print(f"Warning: e-paper support unavailable ({error}).", flush=True)

current_channel = 1

USB_PATH = "/mnt/usb"


# --- Media files -----------------------------------------------------------
# A function rather than constants, because the channel changes while the
# program runs and f-strings at module level would only ever be evaluated once.
def media_paths(channel):
    return {
        "video1": f"{USB_PATH}/{channel}/video_1.mp4",
        "video2": f"{USB_PATH}/{channel}/video_2.mp4",
    }


def channel_image(channel):
    """The first JPEG in the channel's folder, or None if there is none.

    Dotfiles are skipped: a Mac copying to the stick leaves "._name.jpg"
    metadata files beside the real ones, and they are not images.
    """
    folder = f"{USB_PATH}/{channel}"
    names = sorted(
        name
        for name in os.listdir(folder)
        if not name.startswith(".") and name.lower().endswith((".jpg", ".jpeg"))
    )
    return os.path.join(folder, names[0]) if names else None


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

# mpv volume, 0-100. Higher overdrives the PAM8403 on the jack and it distorts.
AUDIO_VOLUME = 65

# `marta` (the control script) talks to CONTROL_SOCKET; main.py in turn sets the
# volume on the audio player through mpv's own IPC socket.
CONTROL_SOCKET = "/tmp/marta-pi.sock"
MPV_SOCKET = "/tmp/marta-pi-mpv.sock"

audio_volume = AUDIO_VOLUME  # changed at runtime by `marta volume`

RESTART_DELAY = 2.0  # seconds to wait before respawning a player that died

# How long the supervisor loop sleeps between checks. A button press interrupts
# the sleep, so this is not the switching latency.
POLL_INTERVAL = 0.5

# How long to let an in-progress e-paper refresh finish on shutdown, rather than
# closing the port from under it.
PANEL_SHUTDOWN_WAIT = 10.0

# "auto-safe" ends up on software decoding here anyway: it picks vulkan-copy,
# which the Pi's Vulkan driver cannot do (no VK_KHR_video_decode_queue), after
# logging misses for libcuda / libvdpau_vc4. "no" skips the pointless probing.
# Try "v4l2m2m-copy" if CPU decoding ever falls behind.
HWDEC = "no"

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


def video_command(path, connector, screen, audio=False):
    command = COMMON + ["--fullscreen", f"--hwdec={HWDEC}"]
    if audio:
        command += [f"--volume={audio_volume}", f"--input-ipc-server={MPV_SOCKET}"]
        if AUDIO_DEVICE:
            command.append(f"--audio-device={AUDIO_DEVICE}")
    else:
        command.append("--no-audio")
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


def player_commands(channel):
    """(name, argv) for every player, for one channel.

    Both the initial players and each channel switch come through here, so
    there is one place that decides what plays.
    """
    paths = media_paths(channel)
    return [
        (
            "video screen 1",
            video_command(paths["video1"], DRM_CONNECTOR_1, SCREEN_1, audio=True),
        ),
        ("video screen 2", video_command(paths["video2"], DRM_CONNECTOR_2, SCREEN_2)),
    ]


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

    def terminate(self):
        """Ask the process to quit, without waiting for it.

        Split out from stop() so a channel switch can signal every player at
        once and have them shut down in parallel instead of one after another.
        """
        if self.is_running():
            self.process.terminate()

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

players = [Player(name, command) for name, command in player_commands(current_channel)]

running = True


def shutdown(_signum, _frame):
    global running
    running = False


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


# --- Channel switching -----------------------------------------------------
# requested_channel is written by the button thread and read by play(). A plain
# assignment is atomic under the GIL, so no lock is needed - and because every
# mutation of `players` stays on the main thread, the supervisor loop below can
# never race with a switch.
requested_channel = current_channel
requested_volume = audio_volume
switch_request = threading.Event()
panel_request = threading.Event()


def switch_channel(channel):
    """Repoint every player at the new channel and restart it.

    Main thread only, called from play().
    """
    global current_channel
    started = time.monotonic()

    # New argv first: if the supervisor sees a player die before we restart it,
    # it must respawn the new channel rather than the old one.
    for player, (_, command) in zip(players, player_commands(channel)):
        player.command = command

    for player in players:
        player.terminate()  # signal them all, so they shut down in parallel
    for player in players:
        player.stop()  # reap, and kill anything that ignored SIGTERM
    for player in players:
        player.start()

    current_channel = channel
    print(
        f"Channel {channel} playing ({(time.monotonic() - started) * 1000:.0f} ms).",
        flush=True,
    )


def set_volume(volume):
    """Apply a new volume to the running player and to future ones.

    Main thread only, called from play().
    """
    global audio_volume
    audio_volume = volume
    # So a respawn or channel switch keeps the new volume.
    for player, (_, command) in zip(players, player_commands(current_channel)):
        player.command = command

    message = {"command": ["set_property", "volume", volume]}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(1.0)
            conn.connect(MPV_SOCKET)
            conn.sendall(json.dumps(message).encode() + b"\n")
    except OSError as error:
        # Usually mpv is mid-restart; it will start with the new volume anyway.
        print(f"Warning: could not reach mpv ({error}).", flush=True)
    print(f"Volume {volume}.", flush=True)


def show_image(channel):
    """Put the channel's image on the e-paper. Panel thread only."""
    if epd is None:
        return
    try:
        path = channel_image(channel)
    except OSError as error:
        print(f"Warning: cannot list channel {channel} ({error}).", flush=True)
        return
    if path is None:
        print(f"Warning: no .jpg in {USB_PATH}/{channel}.", flush=True)
        return
    try:
        epd.image(path)
    except OSError as error:
        print(f"Warning: cannot read {path} ({error}).", flush=True)
    except Exception as error:
        print(f"Warning: e-paper update failed ({error}).", flush=True)


def panel_loop():
    """Refresh the e-paper on a thread of its own.

    A refresh takes about 7 s, so it must not run on the supervisor loop (which
    keeps the video alive) nor on the button worker (where it would hold up the
    next press). Only the channel currently selected is drawn, so a burst of
    presses costs one refresh showing whatever was settled on - and the video
    switches immediately regardless of what the panel is doing.
    """
    while running:
        fired = panel_request.wait(timeout=POLL_INTERVAL)
        panel_request.clear()
        if fired and running:
            show_image(requested_channel)


def on_channel_change(channel):
    """Runs on the button worker thread whenever the selection changes.

    Does no work itself: both the video switch and the panel refresh happen on
    threads that own those resources, so this returns immediately and the next
    press is never held up.
    """
    global requested_channel
    requested_channel = channel
    switch_request.set()
    panel_request.set()


def handle_control(words):
    """Run one request from `marta` and return the reply line.

    Runs on the control thread, so like the buttons it only hands requests to
    the main thread rather than touching the players itself.
    """
    global requested_volume
    if words == ["status"]:
        return f"channel {requested_channel} volume {requested_volume}"

    if len(words) == 2 and words[0] == "channel":
        channel = int(words[1])
        if not os.path.isdir(f"{USB_PATH}/{channel}"):
            raise ValueError(f"no folder for channel {channel} in {USB_PATH}")
        if buttons is not None:
            buttons.select(channel)  # keeps the buttons' idea of "current" right
        else:
            on_channel_change(channel)
        return f"channel {channel}"

    if len(words) == 2 and words[0] == "volume":
        value = words[1]
        volume = int(value)
        if value[0] in "+-":
            volume += requested_volume
        requested_volume = max(0, min(100, volume))
        switch_request.set()
        return f"volume {requested_volume}"

    raise ValueError(f"unknown command: {' '.join(words)}")


def control_loop(server):
    while running:
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            return
        with conn:
            conn.settimeout(1.0)
            try:
                words = conn.makefile().readline().split()
                reply = handle_control(words)
            except Exception as error:
                reply = f"error: {error}"
            try:
                conn.sendall((reply + "\n").encode())
            except OSError:
                pass


def open_control_socket():
    try:
        os.unlink(CONTROL_SOCKET)  # left over from a previous run
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(CONTROL_SOCKET)
    server.listen()
    server.settimeout(POLL_INTERVAL)
    return server


epd = None
panel_thread = None
if EPaper is not None:
    try:
        epd = EPaper().open()
        panel_thread = threading.Thread(target=panel_loop, name="panel", daemon=True)
        panel_thread.start()
    except Exception as error:
        print(f"Warning: e-paper unavailable ({error}).", flush=True)

buttons = None
if ChannelButtons is not None:
    try:
        buttons = ChannelButtons(
            initial=current_channel, on_change=on_channel_change
        ).start()
        print(f"Buttons ready, channel {buttons.current_channel}.", flush=True)
    except Exception as error:
        print(f"Warning: buttons unavailable ({error}).", flush=True)


control_server = None
try:
    control_server = open_control_socket()
    threading.Thread(
        target=control_loop, args=(control_server,), name="control", daemon=True
    ).start()
except OSError as error:
    print(f"Warning: control socket unavailable ({error}).", flush=True)


def play():
    global running
    try:
        for player in players:
            player.start()

        print("Playing. Ctrl+C to quit.", flush=True)
        panel_request.set()  # draw the starting channel without delaying video

        while running:
            if requested_channel != current_channel:
                switch_channel(requested_channel)
            if requested_volume != audio_volume:
                set_volume(requested_volume)

            for player in players:
                if not player.is_running() and running:
                    code = player.process.returncode
                    print(
                        f"{player.name} exited (code {code}), restarting.", flush=True
                    )
                    time.sleep(RESTART_DELAY)
                    player.start()

            # Waiting on the event rather than the clock: a button press returns
            # from this at once, so a switch never waits out the poll interval.
            switch_request.wait(timeout=POLL_INTERVAL)
            switch_request.clear()
    finally:
        print("\nStopping players...", flush=True)
        running = False  # tells the panel thread to wind up too
        for player in players:
            player.stop()
        if control_server is not None:
            control_server.close()
            try:
                os.unlink(CONTROL_SOCKET)
            except OSError:
                pass
        if buttons is not None:
            buttons.stop()
        if panel_thread is not None:
            panel_request.set()  # wake it so it sees running == False
            panel_thread.join(timeout=PANEL_SHUTDOWN_WAIT)
        if epd is not None:
            epd.close()
        sys.exit(0)


play()
