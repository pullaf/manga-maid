"""Helpers for validating and applying file permission masks."""

import os
import re

_PERM_MASK_RE = re.compile(r"^[0-7]{3,4}$")


def sanitize_file_permission_mask(mask: str | None, default: str = "664") -> str:
    """Return a normalized octal permission mask string (e.g. ``664``)."""
    raw = str(mask or "").strip()
    if not raw:
        return default
    if raw.startswith("0o"):
        raw = raw[2:]
    elif raw.startswith("0") and len(raw) > 3:
        raw = raw[1:]
    if not _PERM_MASK_RE.fullmatch(raw):
        return default
    return raw[-3:]


def parse_file_permission_mask(mask: str | None, default: str = "664") -> int:
    """Parse a mask string into an int suitable for ``os.chmod``."""
    return int(sanitize_file_permission_mask(mask, default=default), 8)


def apply_file_permission_mask(path: str, mask: str | None, default: str = "664") -> None:
    """Apply a sanitized permission mask to ``path`` if it exists.

    Skips silently on failure (e.g. read-only or root-owned bind mounts) so callers
    are not aborted after a successful rename or download.
    """
    if not os.path.exists(path):
        return
    try:
        os.chmod(path, parse_file_permission_mask(mask, default=default))
    except OSError:
        pass
