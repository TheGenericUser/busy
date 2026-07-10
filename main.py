"""Drives Busy from data/*.jsonl passbook files: one company open/login per
user, every one of their bank files entered as Receipt/Payment/Journal
vouchers, then company close - looping to the next user.

First launch on a new machine: since data/coords.json doesn't exist yet, it
walks you through a one-time calibration (3 clicks in Busy) before anything
else - never again after that, unless coords.json is deleted.

Press left Alt (with Busy focused) to start the whole run, Esc to stop.
"""
import glob
import json
import os
import sys
import threading
import time
from collections import defaultdict

import pyautogui
from pynput import keyboard, mouse

from loader import get_company_name, load_accounts, load_credentials, load_transactions, read_header, validate_all

DOWN_ARROWS = {"payment": 4, "receipt": 5, "journal": 6}
try:
    STEP_PAUSE = float(input("Step pause: ") or 0.25)
except ValueError:
    STEP_PAUSE = 0.5
CALIBRATION_POINTS = ["company_button", "open_button", "close_button"]

# When frozen into an exe (PyInstaller), relative paths must resolve next to
# the exe itself, not whatever directory it happened to be launched from.
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))


def data_path(*parts):
    return os.path.join(BASE_DIR, "data", *parts)


def load_coords():
    with open(data_path("coords.json")) as f:
        return json.load(f)


def capture_click():
    captured = {}

    def on_click(x, y, button, pressed):
        if pressed:
            captured["xy"] = (x, y)
            return False

    with mouse.Listener(on_click=on_click) as listener:
        listener.join()
    return captured["xy"]


def calibrate():
    print("First-time setup: click 3 spots in Busy (data/coords.json not found yet).")
    print("Note: 'close_button' only appears once the Company menu is open, so you")
    print("may need to click Company again first to bring the menu back up.\n")

    coords = {}
    for point in CALIBRATION_POINTS:
        input(f"Press Enter, then click: {point.replace('_', ' ')}")
        x, y = capture_click()
        print(f"  -> {x}, {y}")
        coords[point] = [x, y]

    os.makedirs(data_path(), exist_ok=True)
    with open(data_path("coords.json"), "w") as f:
        json.dump(coords, f, indent=2)
    print("Saved. This only happens once per machine.\n")


def click(coords, name):
    x, y = coords[name]
    pyautogui.moveTo(x, y)0.75
    pyautogui.click()
    time.sleep(STEP_PAUSE)


def format_amount(amount):
    amount = round(abs(amount), 2)
    if amount == int(amount):
        return str(int(amount))
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def open_company(coords, accounts, credentials, user):
    click(coords, "company_button")
    click(coords, "open_button")

    pyautogui.typewrite(get_company_name(accounts, user))
    pyautogui.press("enter")
    time.sleep(STEP_PAUSE)

    pyautogui.typewrite(credentials["username"])
    pyautogui.press("enter")
    time.sleep(STEP_PAUSE)

    pyautogui.typewrite(credentials["password"])
    pyautogui.press("enter")
    pyautogui.press("enter")
    time.sleep(STEP_PAUSE)


def close_company(coords):
    click(coords, "company_button")
    click(coords, "close_button")


def enter_voucher(txn, seen_voucher_types):
    pyautogui.hotkey("alt", "f3")
    time.sleep(STEP_PAUSE)

    for _ in range(DOWN_ARROWS[txn.voucher]):
        pyautogui.press("down")
    pyautogui.press("enter")
    time.sleep(STEP_PAUSE)

    pyautogui.press("enter")
    time.sleep(STEP_PAUSE)

    if txn.voucher not in seen_voucher_types:
        pyautogui.press("enter")
        time.sleep(STEP_PAUSE)
        seen_voucher_types.add(txn.voucher)

    pyautogui.typewrite(txn.raw_date)
    for _ in range(3):
        pyautogui.press("enter")
    time.sleep(STEP_PAUSE)

    # Journal entries: money received -> bank ledger first, then the other entity.
    # Money given -> other entity first, then bank (also the order for receipt/payment).
    bank_first = txn.voucher == "journal" and txn.amount > 0
    first_keyword = txn.bank_keyword if bank_first else txn.category_keyword
    second_keyword = txn.category_keyword if bank_first else txn.bank_keyword

    pyautogui.typewrite(first_keyword)
    pyautogui.press("enter")
    time.sleep(STEP_PAUSE)

    pyautogui.typewrite(format_amount(txn.amount))
    for _ in range(3):
        pyautogui.press("enter")
    time.sleep(STEP_PAUSE)

    pyautogui.typewrite(second_keyword)
    pyautogui.press("enter")
    time.sleep(STEP_PAUSE)

    pyautogui.press("f2")
    pyautogui.press("f2")
    pyautogui.press("enter")
    time.sleep(STEP_PAUSE)


def files_by_user():
    grouped = defaultdict(list)
    for path in sorted(glob.glob(data_path("*.jsonl"))):
        if os.path.getsize(path) == 0:
            continue
        grouped[read_header(path)["user"]].append(path)
    return grouped


def format_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Progress:
    def __init__(self, total):
        self.total = total
        self.done = 0
        self.current_user = ""
        self.user_total = 0
        self.user_done = 0
        self.start = time.monotonic()

    def start_user(self, user, user_total):
        self.current_user = user
        self.user_total = user_total
        self.user_done = 0

    def step(self):
        self.done += 1
        self.user_done += 1

    def render(self):
        elapsed = time.monotonic() - self.start
        pct = (self.done / self.total * 100) if self.total else 100
        eta_str = format_duration(elapsed / self.done * (self.total - self.done)) if self.done else "--:--"
        bar_len = 30
        filled = int(bar_len * self.done / self.total) if self.total else bar_len
        bar = "#" * filled + "-" * (bar_len - filled)
        user_progress = f"{self.user_done}/{self.user_total}"
        return (f"\r[{bar}] {self.done}/{self.total} ({pct:5.1f}%) overall | "
                f"{self.current_user:<20} {user_progress:<7} "
                f"elapsed {format_duration(elapsed)}  eta {eta_str}   ")


def ticker(progress, stop_event):
    while not stop_event.wait(1):
        print(progress.render(), end="", flush=True)
    print(progress.render(), flush=True)


def run():
    accounts = load_accounts(data_path("accounts.json"))

    errors = validate_all(data_path(), accounts)
    if errors:
        print("Found problems - fix these before running:")
        for error in errors:
            print(f"  - {error}")
        return

    coords = load_coords()
    credentials = load_credentials(data_path("credentials.json"))

    user_transactions = {
        user: [txn for path in paths for txn in load_transactions(path, accounts)]
        for user, paths in files_by_user().items()
    }
    user_transactions = {user: txns for user, txns in user_transactions.items() if txns}
    total = sum(len(txns) for txns in user_transactions.values())
    print(f"{total} entries across {len(user_transactions)} user(s)")

    progress = Progress(total)
    stop_event = threading.Event()
    ticker_thread = threading.Thread(target=ticker, args=(progress, stop_event), daemon=True)
    ticker_thread.start()

    for user, txns in user_transactions.items():
        progress.start_user(user, len(txns))
        open_company(coords, accounts, credentials, user)

        seen_voucher_types = set()
        for txn in txns:
            enter_voucher(txn, seen_voucher_types)
            progress.step()

        close_company(coords)

    stop_event.set()
    ticker_thread.join()


def on_press(key):
    if key == keyboard.Key.alt_l:
        threading.Thread(target=run).start()


def on_release(key):
    if key == keyboard.Key.esc:
        return False


def main():
    if not os.path.exists(data_path("coords.json")):
        calibrate()

    print("Ready. Focus Busy, press left Alt to start a run, Esc to quit.")
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()


if __name__ == "__main__":
    main()
