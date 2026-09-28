#!/usr/bin/env python3
"""Print a message every time a button on GPIO4 is pressed."""

from gpiozero import Button
from signal import pause

# Wire the button between GPIO4 (BCM 4, physical pin 7) and GND.
# pull_up=True uses the Pi's internal pull-up, so no external resistor needed.
button = Button(4, pull_up=True, bounce_time=0.05)


def on_press():
    print("Button pressed!", flush=True)


def on_release():
    print("Button released.", flush=True)


button.when_pressed = on_press
button.when_released = on_release

print("Waiting for button on GPIO4... (Ctrl+C to quit)", flush=True)
pause()
