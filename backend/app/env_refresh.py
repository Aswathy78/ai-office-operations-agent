"""Pick up edits to the mail settings in ``backend/.env`` without restarting the API.

The API reads ``.env`` once, at start. Changing the mail account or app password and
saving the file did nothing until the next restart, so e-mail kept failing with the old
login and the edit looked like it had not worked. Mail settings are now re-read when the
file changes. Only mail settings are refreshed: other settings (database path, workers,
keys) still change only on restart, because they are used once at start-up.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
MAIL_SETTING_PREFIXES = ("SMTP_", "FALLBACK_SMTP_", "EMAIL_")


def _modified(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _under_test() -> bool:
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


# When the file looked the last time we read it; the start-up read counts as the first.
_last_seen: float | None = _modified(ENV_FILE)


def load_mail_settings(path: Path | None = None) -> list[str]:
    """Apply the mail settings in ``.env``, letting the file win over the environment.

    ``load_dotenv`` never replaces a variable that already exists, so a stale
    ``SMTP_PASSWORD`` left in the Windows or shell environment silently beat the file
    and no edit to ``.env`` could fix e-mail. Returns the names that were changed.
    """
    global _last_seen
    if _under_test():
        return []  # tests set their own environment
    path = path or ENV_FILE
    _last_seen = _modified(path)
    changed = []
    for key, value in dotenv_values(path).items():
        if key.startswith(MAIL_SETTING_PREFIXES) and value is not None:
            if os.environ.get(key) != value:
                os.environ[key] = value
                changed.append(key)
    return changed


def refresh_mail_settings(path: Path | None = None) -> bool:
    """Re-read the mail settings if ``.env`` was saved since the last look.

    Returns True when settings were reloaded. Cheap enough to call before every send:
    it is one ``stat`` when nothing changed.
    """
    if _under_test():
        return False  # tests set their own environment
    path = path or ENV_FILE
    modified = _modified(path)
    if modified is None or modified == _last_seen:
        return False
    reloaded = load_mail_settings(path)
    if reloaded:
        # Names only: the values include a password.
        logger.warning("backend/.env changed; reloaded mail settings: %s", ", ".join(sorted(reloaded)))
    return bool(reloaded)
