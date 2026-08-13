"""Settings that outlive one command.

Only one thing needs remembering so far — where invoices are mailed — but it is
the kind of thing that must not live in the source. The purchase inbox of an
Exact administration accepts anything sent to it, so an address committed to a
repository is an open door to someone else's bookkeeping. It goes in a file
under the user's own config directory instead, which is also what makes it
settable without editing code.

    ~/.config/pdfinvoice/config.json      {"mail_to": "…@inkoop.exactonline.nl"}

PDFINVOICE_CONFIG points somewhere else. Anything on the command line wins over
the file, and the environment wins over both — see `pdfinvoice.web`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_ENV = "PDFINVOICE_CONFIG"
DEFAULT_PATH = Path.home() / ".config" / "pdfinvoice" / "config.json"


def path() -> Path:
    """Where the settings live, this run."""
    override = os.environ.get(CONFIG_ENV)
    return Path(override).expanduser() if override else DEFAULT_PATH


def load() -> Dict[str, Any]:
    """The settings, or an empty set of them.

    A missing file is the normal case on a fresh install, and a damaged one
    must not stop invoices from being read: settings are a convenience, never
    a precondition.
    """
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get(name: str, default: Optional[str] = None) -> Optional[str]:
    value = load().get(name, default)
    return value.strip() if isinstance(value, str) else default


def set_value(name: str, value: str) -> Path:
    """Write one setting, leaving the others as they are, and say where."""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    settings = load()
    settings[name] = value
    target.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return target
