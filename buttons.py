#!/usr/bin/env python3
"""Three pushbuttons that select the current channel.

This is the only module that touches gpiozero. main.py imports ChannelButtons
from here and reads .current_channel; nothing else needs to know about GPIO.

Wiring: one button per pin, each between the GPIO pin and GND. The Pi's
internal pull-ups are used, so no external resistors are needed.

    GPIO4  (physical pin 7)  -> channel 1
    GPIO17 (physical pin 11) -> channel 2
    GPIO27 (physical pin 13) -> channel 3

Typical use:

    from buttons import ChannelButtons

    buttons = ChannelButtons(on_change=lambda ch: print(ch)).start()
    ...
    buttons.current_channel   # 1, 2 or 3
    buttons.stop()

Run it directly to check the wiring:

    ./buttons.py
"""

import threading

from gpiozero import Button  # python3-gpiozero, with python3-lgpio as backend

# --- Wiring ----------------------------------------------------------------
# BCM pin -> channel number. Add a fourth button by adding a line here.
CHANNEL_PINS = {
    4: 1,
    17: 2,
    27: 3,
}

INITIAL_CHANNEL = 1
BOUNCE_TIME = 0.05  # seconds, matching the wiring notes in README.md


class ChannelButtons:
    """Tracks which channel the buttons have selected.

    on_change, if given, is called with the new channel number whenever it
    changes. It runs on a worker thread rather than inline, because gpiozero
    callbacks must return promptly - an e-paper refresh takes about 7 s, and
    doing that in the callback would swallow presses in the meantime.
    """

    def __init__(self, pins=None, initial=INITIAL_CHANNEL, on_change=None,
                 bounce_time=BOUNCE_TIME):
        self.pins = dict(pins if pins is not None else CHANNEL_PINS)
        self._channel = initial
        self._on_change = on_change
        self._bounce_time = bounce_time

        self._lock = threading.Lock()
        # gpiozero stops delivering events once a Button is garbage collected,
        # so they are kept for the lifetime of this object.
        self._buttons = []

        self._pending = None
        self._wake = threading.Event()
        self._worker = None
        self._running = False

    @property
    def current_channel(self):
        """The selected channel. Safe to read from any thread."""
        with self._lock:
            return self._channel

    @property
    def channels(self):
        return sorted(set(self.pins.values()))

    # --- lifecycle ---------------------------------------------------------
    def start(self):
        """Claim the pins and begin listening. Returns self so it can chain."""
        if self._buttons:
            return self

        self._running = True
        if self._on_change is not None:
            self._worker = threading.Thread(
                target=self._dispatch, name="channel-on-change", daemon=True
            )
            self._worker.start()

        try:
            for pin, channel in sorted(self.pins.items()):
                button = Button(pin, pull_up=True, bounce_time=self._bounce_time)
                button.when_pressed = self._handler(channel)
                self._buttons.append(button)
        except Exception:
            # Half-claimed pins would stay held until the process exits, and a
            # retry would then fail on "pin already in use".
            self.stop()
            raise
        return self

    def stop(self):
        self._running = False
        self._wake.set()

        for button in self._buttons:
            button.close()
        self._buttons = []

        if self._worker is not None:
            self._worker.join(timeout=1.0)
            self._worker = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.stop()

    # --- events ------------------------------------------------------------
    def _handler(self, channel):
        # One closure per channel, so the callback does not have to work out
        # which button fired.
        def pressed():
            self.select(channel)

        return pressed

    def select(self, channel):
        """Set the channel as though its button had been pressed.

        Pressing the button for the channel already showing does nothing.
        """
        if channel not in self.pins.values():
            raise ValueError(f"no button maps to channel {channel!r}")

        with self._lock:
            if channel == self._channel:
                return
            self._channel = channel
            self._pending = channel

        self._wake.set()

    def _dispatch(self):
        """Deliver on_change off the gpiozero callback thread.

        Only the most recent channel is delivered, so mashing the buttons
        settles on the last one instead of replaying every press through a slow
        callback.
        """
        while self._running:
            self._wake.wait()
            self._wake.clear()

            with self._lock:
                channel, self._pending = self._pending, None

            if channel is None or not self._running:
                continue
            try:
                self._on_change(channel)
            except Exception as error:
                # A broken callback must not take the listener down with it.
                print(f"Warning: channel callback failed ({error}).", flush=True)


if __name__ == "__main__":
    import signal

    buttons = ChannelButtons(on_change=lambda ch: print(f"channel {ch}", flush=True))
    with buttons:
        print(
            f"Listening on GPIO {', '.join(str(p) for p in sorted(buttons.pins))}. "
            f"Channel {buttons.current_channel}. Ctrl+C to quit.",
            flush=True,
        )
        signal.pause()
