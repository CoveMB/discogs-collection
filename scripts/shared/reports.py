"""Small report helpers shared by CLI scripts."""

from __future__ import annotations

import datetime as dt


def readable_timestamp() -> str:
    return dt.datetime.now().replace(microsecond=0).strftime("%Y-%m-%d_%H-%M-%S")
