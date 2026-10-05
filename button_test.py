#!/usr/bin/env python3
"""Print the channel number on every button press. Ctrl+C to quit."""

import signal

from gpiozero import Button

from buttons import BOUNCE_TIME, CHANNEL_PINS


def make_handler(pin, channel):
    def pressed():
        print(f"channel {channel} (GPIO{pin})", flush=True)

    return pressed


# Kept in a list: gpiozero stops firing for Buttons that get garbage collected.
buttons = []
for pin, channel in sorted(CHANNEL_PINS.items()):
    button = Button(pin, pull_up=True, bounce_time=BOUNCE_TIME)
    button.when_pressed = make_handler(pin, channel)
    buttons.append(button)

print(f"Listening on GPIO {', '.join(str(p) for p in sorted(CHANNEL_PINS))}. Ctrl+C to quit.", flush=True)
try:
    signal.pause()
except KeyboardInterrupt:
    pass
finally:
    for button in buttons:
        button.close()
