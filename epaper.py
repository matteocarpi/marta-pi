#!/usr/bin/env python3
"""Show text on the Waveshare 7.5" e-paper panel over USB.

The panel hangs off a Waveshare e-Paper ESP32 Driver Board, so the Pi never
touches SPI - it sends lines of text to the sketch in epaper_serial/ and the
ESP32 does the drawing.

    ./epaper.py                          sample text
    ./epaper.py "Hello Marta"            one-shot
    ./epaper.py --font m "Two\\nlines"    smaller type, explicit line break
    ./epaper.py --clear                  blank the screen
    ./epaper.py --watch /run/marta/epaper.txt
    echo "from a pipe" | ./epaper.py --stdin

Import it instead to drive the panel from other code:

    from epaper import EPaper
    with EPaper() as epd:
        epd.text("Button pressed")
"""

import argparse
import glob
import os
import sys
import time
import unicodedata

import serial  # python3-serial

# --- Serial ----------------------------------------------------------------
# None = pick the first USB serial adapter found. The driver board has a CP2102
# on it, so it shows up as /dev/ttyUSB0 rather than /dev/ttyACM0.
PORT = None
BAUD = 115200

# The board reboots whenever the port is opened on some setups, and panel init
# takes a moment after that.
READY_TIMEOUT = 12.0

# A full refresh of the 7.5" panel takes roughly 4-5 s, and it is the only kind
# this panel does well - it flashes black/white on every update by design.
REPLY_TIMEOUT = 30.0

# E-paper wears with refreshes and each one is slow anyway, so updates are
# rate-limited rather than queued.
MIN_INTERVAL = 3.0

WATCH_POLL = 0.5  # seconds between mtime checks in --watch mode

SAMPLE_TEXT = "Hello Marta\\nthe panel works"

DEBUG = bool(os.environ.get("DEBUG"))

# The Adafruit GFX fonts in the sketch only carry ASCII 0x20-0x7E, so anything
# outside that is folded down before it goes over the wire.
TRANSLITERATE = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-",
    "…": "...", " ": " ", "€": "EUR", "£": "GBP",
}


class EPaperError(RuntimeError):
    pass


def to_ascii(text):
    """Fold text down to what the panel's fonts can actually draw."""
    for source, target in TRANSLITERATE.items():
        text = text.replace(source, target)
    decomposed = unicodedata.normalize("NFKD", text)
    return decomposed.encode("ascii", "ignore").decode("ascii")


def find_port():
    """First USB serial device that looks like the driver board."""
    # by-id names carry the USB-serial chip, so prefer them and favour the
    # CP210x the board actually uses over any other adapter that is plugged in.
    by_id = sorted(glob.glob("/dev/serial/by-id/*"))
    for path in by_id:
        if "CP210" in path or "Silicon_Labs" in path:
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
    def __init__(self, port=PORT, baud=BAUD):
        self.port = port or find_port()
        self.baud = baud
        self.serial = None
        self._last_text = None
        self._last_send = 0.0

    # --- connection --------------------------------------------------------
    def open(self):
        # Opening a port asserts DTR and RTS, which is exactly how esptool drops
        # an ESP32 into its bootloader - so clear both before opening to leave
        # the sketch running. pyserial applies these at open() time.
        self.serial = serial.Serial()
        self.serial.port = self.port
        self.serial.baudrate = self.baud
        self.serial.timeout = 1.0
        self.serial.dtr = False
        self.serial.rts = False
        self.serial.open()

        self._wait_ready()
        return self

    def close(self):
        if self.serial is not None and self.serial.is_open:
            self.serial.close()
        self.serial = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *_exc):
        self.close()

    def _wait_ready(self):
        """Swallow the boot banner, or prove the sketch is alive with a PING."""
        deadline = time.monotonic() + READY_TIMEOUT
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
                f"flashed, and is the baud rate {self.baud}?"
            ) from error

    def _read_line(self):
        raw = self.serial.readline()
        if not raw:
            return None
        line = raw.decode("utf-8", "replace").strip()
        if DEBUG and line:
            print(f"  <- {line}", flush=True)
        return line or None

    def _command(self, command, expect="OK", timeout=REPLY_TIMEOUT):
        if self.serial is None:
            raise EPaperError("Port is not open - call open() first.")
        if DEBUG:
            print(f"  -> {command}", flush=True)

        self.serial.reset_input_buffer()
        self.serial.write(to_ascii(command).encode("ascii") + b"\n")
        self.serial.flush()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self._read_line()
            if line is None:
                continue
            if line == expect:
                return line
            if line.startswith("ERR"):
                raise EPaperError(line)
            # Ignore anything else: a late READY from a board that reset anyway.
        raise EPaperError(f"No reply to {command!r} within {timeout:.0f} s.")

    # --- drawing -----------------------------------------------------------
    def font(self, size):
        """Type size for subsequent text(): 's', 'm' or 'l'."""
        self._command(f"FONT {size.upper()}")

    def clear(self):
        self._command("CLEAR")
        self._last_text = None

    def text(self, text, force=False):
        """Draw text, centred and word-wrapped. Use \\n for a hard line break.

        Redrawing the same string is skipped unless force is set, since a
        refresh costs several seconds and a little panel life.
        """
        payload = to_ascii(text).replace("\n", "\\n").strip()
        if not payload:
            self.clear()
            return

        if payload == self._last_text and not force:
            if DEBUG:
                print("  (unchanged, skipping refresh)", flush=True)
            return

        wait = MIN_INTERVAL - (time.monotonic() - self._last_send)
        if wait > 0:
            time.sleep(wait)

        self._command(f"TEXT {payload}")
        self._last_text = payload
        self._last_send = time.monotonic()


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
    parser.add_argument("--font", choices=["s", "m", "l"], help="type size")
    parser.add_argument("--clear", action="store_true", help="blank the screen and exit")
    parser.add_argument("--watch", metavar="FILE", help="redraw whenever FILE changes")
    parser.add_argument("--stdin", action="store_true", help="read lines from stdin")
    args = parser.parse_args()

    try:
        epd = EPaper(port=args.port)
        print(f"Using {epd.port}", flush=True)
        with epd:
            if args.font:
                epd.font(args.font)

            if args.clear:
                epd.clear()
            elif args.watch:
                watch_file(epd, args.watch)
            elif args.stdin:
                read_stdin(epd)
            else:
                epd.text(" ".join(args.text) if args.text else SAMPLE_TEXT)
    except KeyboardInterrupt:
        print("", flush=True)
    except (EPaperError, serial.SerialException, OSError) as error:
        sys.exit(f"epaper: {error}")


if __name__ == "__main__":
    main()
