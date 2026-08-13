"""Registered company names, looked up by Chamber of Commerce number.

The Handelsregister is the authority on what a company is called. An invoice is
not: it prints a trade name, or a logo, or — on printed stationery — nothing at
all, while Exact books a creditor under one name and expects it to stay that
name. So where a KvK number is known and the register's name for it is known
too, that name wins over whatever the page happens to say.

The lookup is a file, not a web service. This tool makes no network calls, and
that is worth more than automatic name resolution: a scan must give the same
answer next year, on a laptop with no connection, without an API contract.
Filling the file is manual, one line per supplier, and only pays off for
suppliers that recur — which is exactly the set that matters.

    {"77731964": "Garagebedrijf Roos B.V."}

`kvk_names.json` beside this module holds the names that ship with the tool.
A file at ~/.config/pdfinvoice/kvk-names.json is merged over it and wins, so
additions survive reinstalling; PDFINVOICE_KVK_NAMES points somewhere else
instead, for a list kept under version control with the bookkeeping.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

BUNDLED_NAMES = Path(__file__).with_name("kvk_names.json")
USER_NAMES = Path.home() / ".config" / "pdfinvoice" / "kvk-names.json"
NAMES_ENV = "PDFINVOICE_KVK_NAMES"


def _load(path: Path) -> Dict[str, str]:
    """One name file, or nothing at all.

    A malformed or unreadable file must not stop an invoice from being read:
    the name is an improvement on what the page says, never the reason to fail.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        _digits(number): name.strip()
        for number, name in data.items()
        if isinstance(name, str) and name.strip() and _digits(number)
    }


@lru_cache(maxsize=1)
def names() -> Dict[str, str]:
    """Every KvK number this installation knows a registered name for."""
    override = os.environ.get(NAMES_ENV)
    sources = [BUNDLED_NAMES, Path(override) if override else USER_NAMES]

    merged: Dict[str, str] = {}
    for source in sources:
        merged.update(_load(source))
    return merged


def _digits(value: str) -> str:
    """KvK numbers are eight digits; they are printed with spaces and dots."""
    return re.sub(r"\D", "", str(value))


def registered_name(coc_number: Optional[str]) -> Optional[str]:
    """The name the Handelsregister has for this number, if the file knows it."""
    if not coc_number:
        return None
    return names().get(_digits(coc_number))
