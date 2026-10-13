"""Simulated IF Credit amounts: integers of hundredths (2500 = 25.00). Never floats."""

from __future__ import annotations

import re

from .errors import DomainError

MAX_AMOUNT_MINOR = 100_000  # 1,000.00 IF Credits per payment or allocation in this pilot
_AMOUNT = re.compile(r"^\d{1,7}(\.\d{1,2})?$")


def parse_amount(value: object) -> int:
    """'25', '25.5', '25.50' or 25 -> 2500. Rejects negatives, zero, more than two decimals and absurd sizes."""
    if isinstance(value, bool):
        raise DomainError("invalid_amount", "Enter an amount such as 25 or 25.50.")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise DomainError("invalid_amount", "Enter an amount such as 25 or 25.50.")
    if not _AMOUNT.match(text):
        raise DomainError("invalid_amount", "Enter an amount such as 25 or 25.50.")
    whole, _, frac = text.partition(".")
    minor = int(whole) * 100 + int((frac + "00")[:2])
    if minor <= 0:
        raise DomainError("invalid_amount", "The amount must be greater than zero.")
    if minor > MAX_AMOUNT_MINOR:
        raise DomainError("invalid_amount", "The pilot limit is 1,000.00 IF Credits per transaction.")
    return minor


def format_amount(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    return f"{sign}{minor // 100}.{minor % 100:02d}"
