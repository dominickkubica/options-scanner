"""Enumerations shared across the data layer."""

from __future__ import annotations

from enum import StrEnum


class Right(StrEnum):
    """Option right. Stored as a single character so parquet columns stay narrow."""

    CALL = "C"
    PUT = "P"

    @classmethod
    def parse(cls, value: str | Right) -> Right:
        """Accept the spellings vendors actually use: C, CALL, call, Calls, P, PUT."""
        if isinstance(value, cls):
            return value
        text = str(value).strip().upper().rstrip("S")
        if text in {"C", "CALL"}:
            return cls.CALL
        if text in {"P", "PUT"}:
            return cls.PUT
        raise ValueError(f"cannot interpret {value!r} as an option right")
