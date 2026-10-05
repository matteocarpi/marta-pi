#!/usr/bin/env python3
"""Show text or images on the Waveshare 7.5" e-paper panel over USB.

The panel hangs off a Waveshare e-Paper ESP32 Driver Board, so the Pi never
touches SPI - it sends lines of text, or a 1-bit bitmap, to the sketch in epaper_serial/ and the
ESP32 does the drawing.

Use it from another script - keep one instance alive and call text() as often
as you like, since opening the port costs a second or two:

    from epaper import EPaper

    epd = EPaper(font="m").open()
    epd.text("Hello")
    epd.text("Changed")
    epd.image("/mnt/usb/1/text.jpg")    # scaled and cropped to fill the panel
    epd.close()

Config is keyword arguments, or a Config instance if you'd rather build it up:

    from epaper import Config, EPaper

    settings = Config(port="/dev/ttyACM0", font="l", min_interval=5.0)
    with EPaper(settings) as epd:
        epd.text("Marta")

For a single update where the connection cost doesn't matter:

    from epaper import show
    show("Hello", font="s")

It is also a CLI:

    ./epaper.py                          sample text
    ./epaper.py "Hello Marta"            one-shot
    ./epaper.py --font m "Two\\nlines"    smaller type, explicit line break
    ./epaper.py --clear                  blank the screen
    ./epaper.py --image picture.jpg      show an image full screen
    ./epaper.py --watch /path/to/file    redraw whenever the file changes
    echo "from a pipe" | ./epaper.py --stdin
"""

import argparse
import glob
import os
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, fields

import serial  # python3-serial
from PIL import Image, ImageOps  # python3-pil

# --- Defaults --------------------------------------------------------------
# Every one of these is overridable per instance; see Config below.

# None = pick the first USB serial adapter found. Current driver boards carry a
# WCH CH343 bridge, so the Pi names it /dev/ttyACM0; older ones have a CP2102
# and come up as /dev/ttyUSB0. Both are matched.
PORT = None
BAUD = 115200

FONT = "l"  # "s" 12pt, "m" 18pt, "l" 24pt - must match epaper_serial.ino

# Panel size as the sketch sees it - must follow ROTATION in epaper_serial.ino.
WIDTH = 480
HEIGHT = 800

# The board reboots whenever the port is opened on some setups, and panel init
# takes a moment after that.
READY_TIMEOUT = 12.0

# A full refresh of the 7.5" panel takes roughly 5-7 s, and it is the only kind
# this panel does well - it flashes black/white on every update by design.
REPLY_TIMEOUT = 30.0

# E-paper wears with refreshes and each one is slow anyway, so updates are
# rate-limited rather than queued.
MIN_INTERVAL = 3.0

BLACK_POINT = 30  # grey levels at or below this print solid black
WHITE_POINT = 225  # and at or above this solid white, with no dither specks

WATCH_POLL = 0.5  # seconds between mtime checks in --watch mode

SAMPLE_TEXT = "Hello Marta\\nthe panel works"

FONTS = ("s", "m", "l")

# The Adafruit GFX fonts in the sketch only carry ASCII 0x20-0x7E, so anything
# outside that is folded down before it goes over the wire.
TRANSLITERATE = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-",
    "…": "...", " ": " ", "€": "EUR", "£": "GBP",
}


class EPaperError(RuntimeError):
    pass


@dataclass
class Config:
    port: str = PORT
    baud: int = BAUD
    font: str = FONT
    width: int = WIDTH
    height: int = HEIGHT
    min_interval: float = MIN_INTERVAL
    ready_timeout: float = READY_TIMEOUT
    reply_timeout: float = REPLY_TIMEOUT
    debug: bool = bool(os.environ.get("DEBUG"))

    def __post_init__(self):
        if self.font not in FONTS:
            raise ValueError(f"font must be one of {FONTS}, got {self.font!r}")


def to_ascii(text):
    """Fold text down to what the panel's fonts can actually draw."""
    for source, target in TRANSLITERATE.items():
        text = text.replace(source, target)
    decomposed = unicodedata.normalize("NFKD", text)
    return decomposed.encode("ascii", "ignore").decode("ascii")


def prepare_image(path, size):
    """Image file -> packed 1-bit bitmap filling size, 1 bits white."""
    with Image.open(path) as source:
        picture = ImageOps.exif_transpose(source)
        if "A" in picture.getbands() or "transparency" in picture.info:
            picture = picture.convert("RGBA")
            background = Image.new("RGBA", picture.size, "white")
            picture = Image.alpha_composite(background, picture)
        picture = picture.convert("L")
    # Cover the panel, cropping the overflow, rather than letterboxing.
    picture = ImageOps.fit(picture, size, Image.Resampling.LANCZOS)
    # JPEG "white" is really off-white plus compression noise, which the dither
    # turns into stray black specks - so clip both ends to pure before it runs.
    span = WHITE_POINT - BLACK_POINT
    picture = picture.point(
        lambda v: 0 if v <= BLACK_POINT else 255 if v >= WHITE_POINT
        else (v - BLACK_POINT) * 255 // span
    )
    # Floyd-Steinberg dithering; PIL packs mode "1" rows MSB first, 1 = white,
    # which is what the sketch expects.
    return picture.convert("1").tobytes()


def find_port():
    """First USB serial device that looks like the driver board."""
    # by-id names carry the USB-serial chip, so prefer them and favour the
    # bridges these boards actually use over any other adapter plugged in.
    by_id = sorted(glob.glob("/dev/serial/by-id/*"))
    for path in by_id:
        if any(tag in path for tag in ("CP210", "Silicon_Labs", "CH343", "1a86", "QinHeng")):
            return path
    if by_id:
        return by_id[0]

    candidates = sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))
    if not candidates:
        raise EPaperError(
            "No USB serial port found. Check the cable, and that the user is in "
            "the 'dialout' group (groups | grep dialout)."
        )
    return candidates[0]


class EPaper:
    """A connection to the panel. Safe to call from more than one thread."""

    def __init__(self, config=None, **overrides):
        # Always copy: font() writes back to config, and a Config handed to two
        # instances must not have one instance's changes leak into the other.
        if config is None:
            config = Config()
        else:
            config = Config(**{f.name: getattr(config, f.name) for f in fields(config)})

        for key, value in overrides.items():
            if not hasattr(config, key):
                raise TypeError(f"unknown setting {key!r}")
            setattr(config, key, value)
        config.__post_init__()  # re-validate after the overrides

        self.config = config
        self.port = config.port or find_port()
        self.serial = None

        # gpiozero button callbacks and --watch polling run on their own
        # threads, so two writers could otherwise interleave on the port.
        # Reentrant because the public methods call _command() under the lock.
        self._lock = threading.RLock()
        self._last_shown = None
        self._last_send = 0.0

    # --- connection --------------------------------------------------------
    def open(self):
        with self._lock:
            # Opening a port asserts DTR and RTS, which is exactly how esptool
            # drops an ESP32 into its bootloader - so clear both before opening
            # to leave the sketch running. pyserial applies these at open().
            self.serial = serial.Serial()
            self.serial.port = self.port
            self.serial.baudrate = self.config.baud
            self.serial.timeout = 1.0
            self.serial.dtr = False
            self.serial.rts = False
            self.serial.open()

            self._wait_ready()
            # Re-assert the font, so a board that did reset matches our config.
            self._command(f"FONT {self.config.font.upper()}")
        return self

    def close(self):
        with self._lock:
            if self.serial is not None and self.serial.is_open:
                self.serial.close()
            self.serial = None
            self._last_shown = None  # a reconnected board has a stale screen

    def __enter__(self):
        return self.open()

    def __exit__(self, *_exc):
        self.close()

    def _wait_ready(self):
        """Swallow the boot banner, or prove the sketch is alive with a PING."""
        deadline = time.monotonic() + self.config.ready_timeout
        while time.monotonic() < deadline:
            line = self._read_line()
            if line == "READY":
                return
            if line is None and self.serial.in_waiting == 0:
                break  # nothing booting, so it is already up and idle

        self.serial.reset_input_buffer()
        try:
            self._command("PING", expect="PONG")
        except EPaperError as error:
            raise EPaperError(
                f"{self.port} did not answer PING ({error}). Is epaper_serial.ino "
                f"flashed, and is the baud rate {self.config.baud}?"
            ) from error

    def _read_line(self):
        raw = self.serial.readline()
        if not raw:
            return None
        line = raw.decode("utf-8", "replace").strip()
        if self.config.debug and line:
            print(f"  <- {line}", flush=True)
        return line or None

    def _command(self, command, expect="OK", timeout=None):
        with self._lock:
            if self.serial is None:
                raise EPaperError("Port is not open - call open() first.")
            if self.config.debug:
                print(f"  -> {command}", flush=True)

            self.serial.reset_input_buffer()
            self.serial.write(to_ascii(command).encode("ascii") + b"\n")
            self.serial.flush()
            return self._await(command, expect, timeout)

    def _await(self, command, expect, timeout=None):
        if timeout is None:
            timeout = self.config.reply_timeout

        with self._lock:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = self._read_line()
                if line is None:
                    continue
                if line == expect:
                    return line
                if line.startswith("ERR"):
                    raise EPaperError(line)
                # Ignore anything else: a late READY from a board that reset.
            raise EPaperError(f"No reply to {command!r} within {timeout:.0f} s.")

    # --- drawing -----------------------------------------------------------
    def font(self, size):
        """Type size for subsequent text(): 's', 'm' or 'l'."""
        size = size.lower()
        if size not in FONTS:
            raise ValueError(f"font must be one of {FONTS}, got {size!r}")
        with self._lock:
            self._command(f"FONT {size.upper()}")
            self.config.font = size
            self._last_shown = None  # same string at a new size must redraw

    def clear(self):
        with self._lock:
            self._command("CLEAR")
            self._last_shown = None

    def text(self, text, font=None, force=False):
        """Draw text, centred and word-wrapped. Use \\n for a hard line break.

        Redrawing the same string is skipped unless force is set, since a
        refresh costs several seconds and a little panel life. Pass font to
        change size in the same call.
        """
        with self._lock:
            if font is not None and font.lower() != self.config.font:
                self.font(font)

            payload = to_ascii(text).replace("\n", "\\n").strip()
            if not payload:
                self.clear()
                return

            if payload == self._last_shown and not force:
                if self.config.debug:
                    print("  (unchanged, skipping refresh)", flush=True)
                return

            self._throttle()
            self._command(f"TEXT {payload}")
            self._last_shown = payload
            self._last_send = time.monotonic()

    def image(self, path, force=False):
        """Draw an image file (JPG, PNG, ...) full screen, in black and white.

        It is scaled to cover the panel and the overflow cropped, so for an
        exact fit make it 480x800. Unchanged images are skipped like text().
        """
        width, height = self.config.width, self.config.height
        bitmap = prepare_image(path, (width, height))

        with self._lock:
            if bitmap == self._last_shown and not force:
                if self.config.debug:
                    print("  (unchanged, skipping refresh)", flush=True)
                return

            self._throttle()
            self._command(f"IMAGE {width} {height}", expect="SEND")
            if self.config.debug:
                print(f"  -> {len(bitmap)} bytes of bitmap", flush=True)
            self.serial.write(bitmap)
            self.serial.flush()
            self._await("IMAGE data", "OK")
            self._last_shown = bitmap
            self._last_send = time.monotonic()

    def _throttle(self):
        wait = self.config.min_interval - (time.monotonic() - self._last_send)
        if wait > 0:
            time.sleep(wait)


def show(text, **config):
    """Connect, draw once, disconnect.

    Convenient for a one-off update, but it pays the connection cost every
    call - hold an EPaper instance instead if you update repeatedly.
    """
    with EPaper(**config) as epd:
        epd.text(text)


def connect(**config):
    """An open EPaper instance. Remember to close() it."""
    return EPaper(**config).open()


# --- CLI -------------------------------------------------------------------
def watch_file(epd, path):
    """Redraw whenever path changes - the hook for any other process."""
    print(f"Watching {path}. Write to it to change the display. Ctrl+C to quit.",
          flush=True)
    last_stamp = None
    while True:
        try:
            stamp = os.stat(path).st_mtime_ns
        except FileNotFoundError:
            stamp = None

        if stamp != last_stamp:
            last_stamp = stamp
            if stamp is None:
                print(f"{path} is missing, waiting for it.", flush=True)
            else:
                with open(path, encoding="utf-8") as handle:
                    epd.text(handle.read())
        time.sleep(WATCH_POLL)


def read_stdin(epd):
    """One line of stdin per screen."""
    print("Reading stdin, one line per screen. Ctrl+D to quit.", flush=True)
    for line in sys.stdin:
        line = line.rstrip("\n")
        if line:
            epd.text(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("text", nargs="*", help="text to display")
    parser.add_argument("--port", default=PORT, help="serial port (default: autodetect)")
    parser.add_argument("--font", choices=FONTS, default=FONT, help="type size")
    parser.add_argument("--clear", action="store_true", help="blank the screen and exit")
    parser.add_argument("--image", metavar="FILE", help="show an image full screen")
    parser.add_argument("--watch", metavar="FILE", help="redraw whenever FILE changes")
    parser.add_argument("--stdin", action="store_true", help="read lines from stdin")
    args = parser.parse_args()

    try:
        epd = EPaper(port=args.port, font=args.font)
        print(f"Using {epd.port}", flush=True)
        with epd:
            if args.clear:
                epd.clear()
            elif args.image:
                epd.image(args.image)
            elif args.watch:
                watch_file(epd, args.watch)
            elif args.stdin:
                read_stdin(epd)
            else:
                epd.text(" ".join(args.text) if args.text else SAMPLE_TEXT)
    except KeyboardInterrupt:
        print("", flush=True)
    except (EPaperError, ValueError, serial.SerialException, OSError) as error:
        sys.exit(f"epaper: {error}")


if __name__ == "__main__":
    main()
