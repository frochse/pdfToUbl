"""Hand a message to the Mail client on macOS, attachments and all.

A `mailto:` link cannot carry attachments — the scheme has no room for them and
Mail ignores the ones people try to smuggle in — so this drives Mail over
AppleScript instead.

The message is composed and shown, never sent. Sending is the one step that
cannot be taken back, so it stays with whoever is at the keyboard.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence

# Paths arrive as arguments rather than baked into the source: a filename with
# a quote in it would otherwise end the string and run as script.
_SCRIPT = """
on run argv
    set theRecipient to item 1 of argv
    set theSubject to item 2 of argv
    set theBody to item 3 of argv

    tell application "Mail"
        set newMessage to make new outgoing message with properties ¬
            {subject:theSubject, content:theBody, visible:true}
        tell newMessage
            make new to recipient at end of to recipients ¬
                with properties {address:theRecipient}
            repeat with i from 4 to (count of argv)
                tell content
                    make new attachment with properties ¬
                        {file name:(POSIX file (item i of argv))} ¬
                        at after the last paragraph
                end tell
            end repeat
        end tell
        activate
    end tell
end run
"""

# Mail keeps reading the attachments after the script returns, so they cannot
# be deleted straight away. One outbox is reused and swept on the way in, which
# leaves at most one set of files behind.
_OUTBOX_PREFIX = "pdfinvoice-mail-"


class MailError(RuntimeError):
    """Mail could not be driven; the message carries what to do about it."""


def mail_available() -> bool:
    """Whether this machine can be asked to compose a message at all."""
    return platform.system() == "Darwin" and bool(shutil.which("osascript"))


def outbox() -> Path:
    """A directory for the files being attached, swept of previous runs."""
    root = Path(tempfile.gettempdir())
    for stale in root.glob(f"{_OUTBOX_PREFIX}*"):
        shutil.rmtree(stale, ignore_errors=True)
    return Path(tempfile.mkdtemp(prefix=_OUTBOX_PREFIX))


def compose(
    recipient: str,
    subject: str,
    body: str,
    attachments: Sequence[Path],
    runner=subprocess.run,
) -> None:
    """Open a draft in Mail, addressed and with `attachments` attached."""
    if not mail_available():
        raise MailError(
            "de Mail-client kan alleen op macOS worden aangestuurd"
        )
    missing = [str(p) for p in attachments if not Path(p).is_file()]
    if missing:
        raise MailError(f"bijlage niet gevonden: {', '.join(missing)}")

    result = runner(
        ["osascript", "-", recipient, subject, body,
         *[str(Path(p).resolve()) for p in attachments]],
        input=_SCRIPT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MailError(_explain(result.stderr or ""))


def _explain(stderr: str) -> str:
    """Turn an osascript failure into something worth reading."""
    detail = stderr.strip().splitlines()
    message = detail[-1] if detail else "onbekende fout"
    if "-1743" in stderr or "not allowed" in stderr.lower():
        return (
            "macOS staat het aansturen van Mail nog niet toe. Zet dit aan bij "
            "Systeeminstellingen → Privacy en beveiliging → Automatisering, "
            "onder Terminal → Mail."
        )
    if "-600" in stderr or "not running" in stderr.lower():
        return "Mail kon niet gestart worden; open Mail en probeer het opnieuw."
    return f"Mail gaf een fout: {message}"


def write_attachments(
    directory: Path,
    pdf_bytes: Optional[bytes],
    pdf_name: str,
    ubl_xml: Optional[str],
    ubl_name: str,
) -> List[Path]:
    """Put the PDF and the UBL side by side, ready to attach."""
    written: List[Path] = []
    if pdf_bytes:
        pdf_path = directory / pdf_name
        pdf_path.write_bytes(pdf_bytes)
        written.append(pdf_path)
    if ubl_xml:
        ubl_path = directory / ubl_name
        ubl_path.write_text(ubl_xml, encoding="utf-8")
        written.append(ubl_path)
    return written
