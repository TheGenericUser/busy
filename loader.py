"""Turn hand-entered passbook transactions (data/*.jsonl) into records
ready for the Tally automation.

One file = one passbook: file format (data/savita_bagri_hdfc_26-27.jsonl):
    line 1:   {"financial_year": "26-27", "user": "Savita Bagri", "bank": "HDFC"}
    line 2+:  {"date": "D/M", "category": ..., "amount": <signed number>, "voucher": "j" (optional)}

"voucher" is only needed to mark a journal entry ("j"/"journal"). Otherwise
it's inferred from the amount's sign: negative -> payment, positive -> receipt.
It still accepts "r"/"p"/"receipt"/"payment" explicitly if given.

user/bank live in the header so you don't retype them per line; a line can
still set its own "user"/"bank" to override the header for a one-off mixed entry.

amount is signed for bookkeeping (+ = money into the bank, - = money out) but
Tally only ever needs the magnitude typed in - direction comes from the
voucher type, not from ledger ordering (the category ledger is always typed
before the bank ledger, for every voucher type).

data/accounts.json holds, per user: "_company" (the exact Tally company name
to type at Open), one entry per bank mapping to {"_ledger": <exact keyword>}
for that bank's own ledger, and one keyword per category - categories sit at
the user level (not nested per bank), since the same category keyword applies
across all of that user's banks.

data/credentials.json holds a single {"username", "password"} - every company
uses the same Tally login.
"""
import glob
import json
import os
from dataclasses import dataclass
from datetime import date

VOUCHER_ALIASES = {
    "r": "receipt", "receipt": "receipt",
    "p": "payment", "payment": "payment",
    "j": "journal", "journal": "journal",
}


def normalize_voucher(voucher: str) -> str:
    key = voucher.strip().lower()
    if key not in VOUCHER_ALIASES:
        raise ValueError(f"unknown voucher type {voucher!r} (use r/p/j or receipt/payment/journal)")
    return VOUCHER_ALIASES[key]


def infer_voucher(amount: float) -> str:
    return "payment" if amount < 0 else "receipt"


def parse_fy(fy: str) -> int:
    """'26-27' -> 2026 (the financial year's starting calendar year)."""
    start, end = fy.split("-")
    start_year = int(start) + (2000 if len(start) == 2 else 0)
    end_year = int(end) + (2000 if len(end) == 2 else 0)
    if end_year % 100 != (start_year + 1) % 100:
        raise ValueError(f"'{fy}' isn't a valid FY range (expected consecutive years)")
    return start_year


def resolve_date(fy_start_year: int, day_month: str) -> date:
    """'3/4' + fy_start_year=2026 -> date(2026, 4, 3). Jan-Mar roll into fy_start_year + 1."""
    day, month = (int(part) for part in day_month.split("/"))
    year = fy_start_year if month >= 4 else fy_start_year + 1
    return date(year, month, day)


def read_header(jsonl_path: str) -> dict:
    with open(jsonl_path) as f:
        return json.loads(f.readline())


def load_accounts(path: str) -> dict:
    with open(path) as f:
        raw = json.load(f)
    accounts = {}
    for user, user_data in raw.items():
        banks, categories = {}, {}
        for key, value in user_data.items():
            if key == "_company":
                continue
            if isinstance(value, dict):
                banks[key.lower()] = value.get("_ledger", key)
            else:
                categories[key.lower()] = value
        accounts[user.lower()] = {
            "company": user_data.get("_company", user),
            "banks": banks,
            "categories": categories,
        }
    return accounts


def get_company_name(accounts: dict, user: str) -> str:
    return accounts.get(user.lower(), {}).get("company", user)


def get_bank_keyword(accounts: dict, user: str, bank: str) -> str:
    keyword = accounts.get(user.lower(), {}).get("banks", {}).get(bank.lower())
    if keyword is None:
        print(f"[warn] no ledger keyword mapped for {user!r} / {bank!r}; typing bank name as-is")
        return bank
    return keyword


def resolve_category(accounts: dict, user: str, category: str):
    """Keyword for a category; falls back to that user's own bank names so a
    journal entry can name a sibling bank for inter-account transfers (e.g.
    category "Indusind" on a PNB passbook). None if genuinely unmapped."""
    user_accounts = accounts.get(user.lower())
    if user_accounts is None:
        return None
    return user_accounts["categories"].get(category.lower()) or user_accounts["banks"].get(category.lower())


def get_keyword(accounts: dict, user: str, category: str) -> str:
    """Case-insensitive lookup; falls back to the raw category text (and warns) if unmapped."""
    keyword = resolve_category(accounts, user, category)
    if keyword is None:
        print(f"[warn] no keyword mapped for {user!r} / {category!r}; typing category as-is")
        return category
    return keyword


def load_credentials(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


@dataclass
class Transaction:
    user: str
    bank: str
    voucher: str
    txn_date: date
    raw_date: str
    category: str
    amount: float
    category_keyword: str
    bank_keyword: str


def load_transactions(jsonl_path: str, accounts: dict) -> list[Transaction]:
    with open(jsonl_path) as f:
        lines = [json.loads(line) for line in f if line.strip()]

    if len(lines) < 2:
        return []

    header = lines[0]
    fy_start_year = parse_fy(header["financial_year"])
    default_user = header.get("user")
    default_bank = header.get("bank")

    transactions = []
    for row in lines[1:]:
        user = row.get("user", default_user)
        bank = row.get("bank", default_bank)
        if user is None or bank is None:
            raise ValueError(f"no user/bank for row {row!r} (set it in the header or on the row)")

        voucher = normalize_voucher(row["voucher"]) if "voucher" in row else infer_voucher(row["amount"])

        transactions.append(Transaction(
            user=user,
            bank=bank,
            voucher=voucher,
            txn_date=resolve_date(fy_start_year, row["date"]),
            raw_date=row["date"],
            category=row["category"],
            amount=row["amount"],
            category_keyword=get_keyword(accounts, user, row["category"]),
            bank_keyword=get_bank_keyword(accounts, user, bank),
        ))
    return transactions


def parse_filename(path: str):
    """Best-effort: pull (user, bank, financial_year) out of a filename like
    'savita_bagri_pnb_26_27.jsonl' or 'savita_bagri_hdfc_26-27.jsonl'. The last
    two tokens are the FY years, the token before that is the bank, everything
    before is the user. Returns None if the filename doesn't fit that shape."""
    stem = os.path.splitext(os.path.basename(path))[0]
    tokens = stem.replace("-", "_").split("_")
    if len(tokens) < 3 or not (tokens[-1].isdigit() and tokens[-2].isdigit()):
        return None
    fy = f"{tokens[-2]}-{tokens[-1]}"
    middle = tokens[:-2]
    bank = middle[-1]
    user = " ".join(middle[:-1])
    return user, bank, fy


def validate_file(path: str, header: dict, rows: list[dict], accounts: dict) -> list[str]:
    """Cross-checks one passbook file's header against its own filename, and
    every user/bank/category it references against accounts.json. Returns a
    list of human-readable problems (empty means the file is clean)."""
    errors = []

    parsed = parse_filename(path)
    if parsed:
        expected_user, expected_bank, expected_fy = parsed
        if expected_user.lower() != header.get("user", "").lower():
            errors.append(f"{path}: filename implies user {expected_user!r}, header says {header.get('user')!r}")
        if expected_bank.lower() != header.get("bank", "").lower():
            errors.append(f"{path}: filename implies bank {expected_bank!r}, header says {header.get('bank')!r}")
        if expected_fy != header.get("financial_year", ""):
            errors.append(f"{path}: filename implies FY {expected_fy!r}, header says {header.get('financial_year')!r}")

    header_user = header.get("user", "")
    header_bank = header.get("bank", "")
    header_accounts = accounts.get(header_user.lower())
    if header_accounts is None:
        errors.append(f"{path}: user {header_user!r} not found in accounts.json")
    elif header_bank.lower() not in header_accounts["banks"]:
        errors.append(f"{path}: bank {header_bank!r} not found under {header_user!r} in accounts.json")

    for row in rows:
        row_user = row.get("user", header_user)
        row_category = row.get("category")
        if row_category and resolve_category(accounts, row_user, row_category) is None:
            errors.append(f"{path}: category {row_category!r} not found in accounts.json for {row_user!r}")

    return errors


def validate_all(data_dir: str, accounts: dict) -> list[str]:
    """Validates every data/*.jsonl file. Files with no entries yet (empty or
    header-only) are skipped rather than flagged as errors."""
    errors = []
    for path in sorted(glob.glob(os.path.join(data_dir, "*.jsonl"))):
        with open(path) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        if len(lines) < 2:
            print(f"[skip] {path} has no entries yet")
            continue
        errors.extend(validate_file(path, lines[0], lines[1:], accounts))
    return errors


if __name__ == "__main__":
    accounts = load_accounts("data/accounts.json")
    for path in sorted(glob.glob("data/*.jsonl")):
        print(f"--- {path} ---")
        for txn in load_transactions(path, accounts):
            print(txn)
