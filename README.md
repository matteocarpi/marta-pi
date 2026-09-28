# marta-pi

Button-triggered playback for Raspberry Pi.

## Target

- Raspberry Pi 4
- Raspberry Pi OS Lite (64-bit, Bookworm)

## Dependencies

| Package | Use |
| --- | --- |
| `git` | pulling code onto the device |
| `mpv` | media playback |
| `python3-gpiozero` | GPIO access |
| `python3-lgpio` | gpiozero pin backend on Bookworm |

Install:

```bash
sudo apt update
sudo apt install -y git mpv python3-gpiozero python3-lgpio
```

## Setup

```bash
git clone https://github.com/matteocarpi/marta-pi.git
cd marta-pi
```

## Run

```bash
python3 button_gpio4.py
```

Prints a line on each press. `Ctrl+C` to quit.

## Wiring

Button between **GPIO4** (BCM 4, physical pin 7) and **GND** (physical pin 6 or 9).

Uses the Pi's internal pull-up (`pull_up=True`) — no external resistor needed.
Debounce is 50 ms.

## Audio notes

Check the output device before relying on playback:

```bash
mpv --audio-device=help
```

HDMI is the default when a display is attached. To force the 3.5 mm jack:

```bash
mpv --audio-device=alsa/sysdefault:CARD=Headphones <file>
```

## Updating a device

```bash
cd marta-pi && git pull
```

Clone over HTTPS so devices only ever need read access — no keys on the Pi.

## Roadmap

Fleet deployment to many Pis, shipped as a Docker container. Containers will need
`--device /dev/gpiochip0` (or `--privileged`) for GPIO and access to the host
audio device.
