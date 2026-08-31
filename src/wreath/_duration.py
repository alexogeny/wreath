from __future__ import annotations

__all__ = ["decimal_unit"]


def decimal_unit(value: str, *, require_unit: bool = False) -> tuple[str, str] | None:
    text = value.strip()
    length = len(text)
    index = 0
    while index < length and "0" <= text[index] <= "9":
        index += 1
    if index < length and text[index] == ".":
        index += 1
        fraction = index
        while index < length and "0" <= text[index] <= "9":
            index += 1
        if index == fraction:
            return None
    elif index == 0:
        return None
    number_end = index
    while index < length and text[index].isspace():
        index += 1
    unit = text[index:]
    if require_unit and not unit:
        return None
    if unit and any(not ("A" <= character <= "Z" or "a" <= character <= "z") for character in unit):
        return None
    return text[:number_end], unit
