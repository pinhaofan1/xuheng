"""Recognize opaque generated identifiers without treating ordinary names as IDs."""
import re

_INTERNAL_ID = re.compile(
    r"^(?:ephemeral-\d+-[0-9a-f]{8}|(?:novel|book)-.+-char-.+|char-[0-9a-f]{8,}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I
)


def is_internal_entity_id(value: str) -> bool:
    return bool(_INTERNAL_ID.fullmatch((value or "").strip()))
