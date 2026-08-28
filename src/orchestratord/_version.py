"""Dynamic calendar-version with release-tag freeze."""

import os
from datetime import date


def _version() -> str:
    release_tag = os.environ.get("RELEASE_TAG", "")
    if release_tag:
        return release_tag.removeprefix("v")
    today = date.today()
    return f"{today.year}.{today.month}.{today.day}"


__version__ = _version()
__version_info__ = tuple(int(x) for x in __version__.split("."))