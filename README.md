# marta-pi

Looping video on both HDMI outputs of a Raspberry Pi, plus a looping audio track.

## Target

- Raspberry Pi 4
- Raspberry Pi OS Lite (64-bit, Bookworm)

## Dependencies

| Package | Use |
| --- | --- |
| `git` | pulling code onto the device |
| `mpv` | media playback |
| `xserver-xorg` | X server — required for dual-screen output (see below) |
| `xinit` | provides `startx` |
| `x11-xserver-utils` | provides `xrandr`, used to lay out the two screens |
| `python3-gpiozero` | GPIO access |
| `python3-lgpio` | gpiozero pin backend on Bookworm |
| `python3-serial` | `pyserial` — `epaper.py` uses it to reach the e-paper's ESP32 board over USB |

Install:

```bash
sudo apt update
sudo apt install -y git mpv xserver-xorg xinit x11-xserver-utils \
    python3-gpiozero python3-lgpio python3-serial
```

Opening the serial port also needs group membership, which only takes effect
after a fresh login:

```bash
sudo usermod -aG dialout $USER
```

### On the ESP32

`epaper_serial/epaper_serial.ino` is built and flashed from a workstation, not
the Pi. In the Arduino IDE: board **ESP32 Dev Module**, and via Library Manager:

| Library | Use |
| --- | --- |
| `GxEPD2` | e-paper panel driver (Jean-Marc Zingg) |
| `Adafruit GFX Library` | fonts and text rendering — pulled in as a GxEPD2 dependency |

## Setup

```bash
git clone https://github.com/matteocarpi/marta-pi.git
cd marta-pi
chmod +x main.py
```

## Run

From the Pi's **own console** — not over SSH, which has no VT for X to claim:

```bash
startx ./main.py
```

If the shebang is not honoured, name the interpreter instead:

```bash
startx /usr/bin/python3 /home/admin/marta-pi/main.py
```

`Ctrl+C` stops every player and exits.

Set `DEBUG=1` to print each mpv command line and let mpv report its own errors:

```bash
DEBUG=1 startx ./main.py
```

## Autostart

`marta-pi.service` runs playback at boot. It needs `startx` to be usable by a
normal user, so set this in `/etc/X11/Xwrapper.config` first:

```
allowed_users=anybody
needs_root_rights=yes
```

Then install and enable:

```bash
sudo cp marta-pi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now marta-pi
```

Check on it:

```bash
systemctl status marta-pi
journalctl -u marta-pi -f
```

X runs on `vt7` and switches to it, leaving the console login on `tty1` alone.
Don't move it to `vt1`: the getty owns that terminal, and the service gets
SIGHUP'd the moment it starts (`code=killed, signal=HUP`).

The unit also waits for `/mnt/usb` to be mounted, and restarts playback after
5 s if it exits.

Stop it while working by hand: `sudo systemctl stop marta-pi`.

## Why X is required

On bare KMS only one process can hold DRM master per card, so a second `mpv`
targeting the other connector cannot start — one screen plays, the other stays
dark. Running inside X makes the X server the sole DRM master and both `mpv`
windows are ordinary clients.

Xorg clones all outputs at 0,0 by default, so `main.py` calls `xrandr` at
startup to place the connected outputs left to right. Note that X names them
`HDMI-1` / `HDMI-2`, while bare KMS calls them `HDMI-A-1` / `HDMI-A-2`.

## Configuration

All at the top of `main.py`:

| Setting | Meaning |
| --- | --- |
| `VIDEO_HDMI_1`, `VIDEO_HDMI_2`, `AUDIO_TRACK` | media paths |
| `OUTPUT_MODE` | `"session"` for X/Wayland (two screens), `"drm"` for bare KMS (one screen) |
| `SCREEN_1`, `SCREEN_2` | which screen each video goes to; swap if they come out reversed |
| `ARRANGE_SCREENS` | set `False` to skip the `xrandr` layout call |
| `DRM_DEVICE`, `DRM_CONNECTOR_1/2` | only used by `OUTPUT_MODE = "drm"` |
| `HWDEC` | `"no"`; see decoding note below |
| `AUDIO_DEVICE` | `None` for mpv's default |
| `AUDIO_VOLUME` | mpv volume 0–100; `65` keeps the PAM8403 amp from clipping |
| `RESTART_DELAY` | seconds before respawning a player that exited |

Leave `DRM_DEVICE` as `None` so mpv probes for the card that actually has
connectors — on this Pi `card0` is the v3d render node with none, and pinning it
there makes mpv exit with code 2.

## Decoding

`HWDEC = "no"` (software) is deliberate. `auto-safe` selects vulkan-copy, which
the Pi's Vulkan driver cannot do — it lacks `VK_KHR_video_decode_queue` — and
falls back to software anyway, after logging harmless misses for `libcuda.so.1`
and `libvdpau_vc4.so`. Software decode handles two streams comfortably at the
resolutions in use.

If CPU decoding ever falls behind, try `HWDEC = "v4l2m2m-copy"` and confirm with:

```bash
mpv --no-config --hwdec=v4l2m2m-copy --msg-level=vd=v --length=3 <file> 2>&1 | grep -i decod
```

## Audio notes

Check the output device before relying on playback:

```bash
mpv --audio-device=help
```

The kernel cmdline on this device ends with
`snd_bcm2835.enable_headphones=1 snd_bcm2835.enable_hdmi=0`, and later flags
win — so **HDMI audio is disabled** and only the 3.5 mm jack is available. To
force the jack explicitly:

```bash
mpv --audio-device=alsa/sysdefault:CARD=Headphones <file>
```

For sound out of a screen instead, change `snd_bcm2835.enable_hdmi` to `1` in
`/boot/firmware/cmdline.txt` and reboot.

## Troubleshooting

`startx` refuses to start as a normal user — set in `/etc/X11/Xwrapper.config`:

```
allowed_users=anybody
needs_root_rights=yes
```

Running `sudo startx` also works but warns about `/root/.Xauthority` and gives
you a root-owned X server; prefer the config above.

Output names differ from the defaults — run `xrandr` inside the session to list
them.

## Media

Video and audio files live on a USB stick mounted at `/mnt/usb`, not in this
repo.

## Wiring

The GPIO packages are installed for button-triggered playback, which is not yet
part of `main.py` (an earlier standalone button script is in the git history).

Button between **GPIO4** (BCM 4, physical pin 7) and **GND** (physical pin 6 or
9). Uses the Pi's internal pull-up (`pull_up=True`) — no external resistor
needed. Debounce is 50 ms.

## Updating a device

```bash
cd marta-pi && git pull
```

Clone over HTTPS so devices only ever need read access — no keys on the Pi.

## Roadmap

Fleet deployment to many Pis, shipped as a Docker container. Containers will need
`--device /dev/gpiochip0` (or `--privileged`) for GPIO and access to the host
audio device.
