"""One-time per-machine setup. Run this after opening Tally on a new laptop;
it walks you through the 3 clicks main.py needs and writes data/coords.json.

"close_button" only appears once the Company menu is open, so when prompted
for it you may need to click Company again first to bring the menu back up.
"""
import json

from pynput import mouse

POINTS = ["company_button", "open_button", "close_button"]


def capture_click():
    captured = {}

    def on_click(x, y, button, pressed):
        if pressed:
            captured["xy"] = (x, y)
            return False

    with mouse.Listener(on_click=on_click) as listener:
        listener.join()
    return captured["xy"]


def main():
    coords = {}
    for point in POINTS:
        input(f"Press Enter, then click: {point.replace('_', ' ')}")
        x, y = capture_click()
        print(f"  -> {x}, {y}")
        coords[point] = [x, y]

    with open("data/coords.json", "w") as f:
        json.dump(coords, f, indent=2)
    print("Saved to data/coords.json")


if __name__ == "__main__":
    main()
