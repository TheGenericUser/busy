"""Drives Busy from data/*.jsonl passbook files: one company open/login per
user, every one of their bank files entered as Receipt/Payment/Journal
vouchers, then company close - looping to the next user.

First launch on a new machine: since data/coords.json doesn't exist yet, it
walks you through a one-time calibration (3 clicks in Busy) before anything
else - never again after that, unless coords.json is deleted.
"""
import glob
import json
import logging
import os
import sys
import threading
import time
from collections import defaultdict

import pyautogui
from pynput import keyboard, mouse

from loader import (
    get_company_name,
    load_accounts,
    load_credentials,
    load_transactions,
    load_voucher_state,
    read_header,
    save_voucher_state,
    validate_all,
)

DOWN_ARROWS = {"payment": 4, "receipt": 5, "journal": 6}
try:
    STEP_PAUSE = float(input("Step pause: ") or 0.2)
except ValueError:
    STEP_PAUSE = 0.2
# Delay between individual characters within one typewrite() call. STEP_PAUSE
# only separates whole actions (click/type/press) - typewrite() itself sends
# characters with no gap by default, which fields with per-keystroke
# formatting logic (like a date field) can drop under.
TYPE_INTERVAL = 0.03
CALIBRATION_POINTS = ["company_button", "open_button", "close_button"]

# When frozen into an exe (PyInstaller), relative paths must resolve next to
# the exe itself, not whatever directory it happened to be launched from.
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "automation.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("automation")


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
    log.info("click %s at (%s, %s)", name, x, y)
    pyautogui.moveTo(x, y)
    pyautogui.click()
    time.sleep(STEP_PAUSE)


def type_text(text, redact=False):
    log.info("type %s", "*" * len(text) if redact else repr(text))
    pyautogui.typewrite(text, interval=TYPE_INTERVAL)


def press_key(key):
    log.info("press %s", key)
    pyautogui.press(key)


def press_hotkey(*keys):
    log.info("hotkey %s", "+".join(keys))
    pyautogui.hotkey(*keys)


def format_amount(amount):
    amount = round(abs(amount), 2)
    if amount == int(amount):
        return str(int(amount))
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def open_company(coords, accounts, credentials, user):
    click(coords, "company_button")
    click(coords, "open_button")

    type_text(get_company_name(accounts, user))
    press_key("enter")
    time.sleep(STEP_PAUSE)

    type_text(credentials["username"])
    press_key("enter")

    type_text(credentials["password"], redact=True)
    press_key("enter")
    press_key("enter")
    time.sleep(STEP_PAUSE)


def close_company(coords):
    click(coords, "company_button")
    click(coords, "close_button")


def enter_voucher(txn, seen_voucher_types):
    press_hotkey("alt", "f3")
    time.sleep(STEP_PAUSE)

    for _ in range(DOWN_ARROWS[txn.voucher]):
        press_key("down")
    press_key("enter")
    time.sleep(STEP_PAUSE)

    press_key("enter")
    time.sleep(STEP_PAUSE)

    if txn.voucher not in seen_voucher_types:
        press_key("enter")
        time.sleep(STEP_PAUSE)
        seen_voucher_types.add(txn.voucher)

    type_text(txn.raw_date)
    for _ in range(3):
        press_key("enter")
    time.sleep(STEP_PAUSE)

    # Journal entries: money received -> bank ledger first, then the other entity.
    # Money given -> other entity first, then bank (also the order for receipt/payment).
    bank_first = txn.voucher == "journal" and txn.amount > 0
    first_keyword = txn.bank_keyword if bank_first else txn.category_keyword
    second_keyword = txn.category_keyword if bank_first else txn.bank_keyword

    type_text(first_keyword)
    time.sleep(STEP_PAUSE)
    press_key("enter")
    time.sleep(STEP_PAUSE)

    type_text(format_amount(txn.amount))
    time.sleep(STEP_PAUSE)
    for _ in range(3):
        press_key("enter")
    time.sleep(STEP_PAUSE)

    type_text(second_keyword)
    press_key("enter")
    time.sleep(STEP_PAUSE)

    press_key("f2")
    press_key("f2")
    press_key("enter")
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
    log.info("run triggered")
    accounts = load_accounts(data_path("accounts.json"))

    skipped, errors = validate_all(data_path(), accounts)
    for path in skipped:
        log.info("skip %s: no entries yet", path)
    if errors:
        log.error("Found problems - fix these before running:")
        for error in errors:
            log.error("  - %s", error)
        return

    coords = load_coords()
    credentials = load_credentials(data_path("credentials.json"))

    voucher_state_path = data_path("voucher_state.json")
    voucher_state = load_voucher_state(voucher_state_path)

    user_transactions = {
        user: [txn for path in paths for txn in load_transactions(path, accounts)]
        for user, paths in files_by_user().items()
    }
    user_transactions = {user: txns for user, txns in user_transactions.items() if txns}
    total = sum(len(txns) for txns in user_transactions.values())
    log.info("%d entries across %d user(s)", total, len(user_transactions))

    progress = Progress(total)
    stop_event = threading.Event()
    ticker_thread = threading.Thread(target=ticker, args=(progress, stop_event), daemon=True)
    ticker_thread.start()

    for user, txns in user_transactions.items():
        log.info("=== %s: %d entries ===", user, len(txns))
        progress.start_user(user, len(txns))
        open_company(coords, accounts, credentials, user)

        user_fy_state = voucher_state.setdefault(user, {})
        for txn in txns:
            seen_voucher_types = set(user_fy_state.setdefault(txn.financial_year, []))
            enter_voucher(txn, seen_voucher_types)
            user_fy_state[txn.financial_year] = sorted(seen_voucher_types)
            save_voucher_state(voucher_state_path, voucher_state)
            progress.step()

        close_company(coords)

    stop_event.set()
    ticker_thread.join()
    log.info("run finished")


run_lock = threading.Lock()


def run_once():
    if not run_lock.acquire(blocking=False):
        log.info("Alt pressed but a run is already in progress - ignoring")
        return
    try:
        run()
    finally:
        run_lock.release()


def on_press(key):
    if key == keyboard.Key.alt_l:
        threading.Thread(target=run_once).start()


def on_release(key):
    if key == keyboard.Key.esc:
        return False


def main():
    if not os.path.exists(data_path("coords.json")):
        calibrate()

    log.info("Ready. Focus Busy, press left Alt to start a run, Esc to quit.")
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()


if __name__ == "__main__":
    main()
