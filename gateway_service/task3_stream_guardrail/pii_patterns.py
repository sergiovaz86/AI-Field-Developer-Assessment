"""Bounded detectors for the approved PII classes.

Matches are limited to these shapes:

- Email: an addr-spec of at most 320 characters. The local part is 1 to 64
  characters from ``A-Za-z0-9._%+-`` and starts with an alphanumeric. The
  domain has dot-separated labels and a final alphabetic label of 2 to 24
  characters.
- US SSN: exactly ``ddd-dd-dddd``. Area ``000``, ``666``, and ``900-999``
  are rejected. Group ``00`` and serial ``0000`` are rejected. Digits
  without dashes are not SSNs.
- Payment card: 13 to 19 digits, optionally separated by a single space or
  hyphen, and valid under the Luhn check. The whole span is at most 37
  characters.

A string longer than the class maximum is not a supported match.
"""

from __future__ import annotations

MAX_EMAIL_CHARS = 320
MAX_EMAIL_LOCAL = 64
MAX_CARD_DIGITS = 19
MAX_CARD_CHARS = 37
SSN_CHARS = 11
MAX_HOLDBACK = MAX_EMAIL_CHARS
REDACTION_TOKEN = "[REDACTED]"

_LOCAL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._%+-")
_DOMAIN = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-")


def luhn_valid(digits: str) -> bool:
    """Return whether ``digits`` satisfies the Luhn check."""
    if not digits.isdigit():
        return False
    total = 0
    double = False
    for character in reversed(digits):
        value = ord(character) - 48
        if double:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        double = not double
    return total % 10 == 0


def is_viable_prefix(value: str) -> bool:
    """Return whether ``value`` can still grow into a supported match."""
    if not value or len(value) > MAX_HOLDBACK:
        return False
    return is_email_prefix(value) or is_ssn_prefix(value) or is_card_prefix(value)


def longest_viable_suffix_length(value: str) -> int:
    """Return how many trailing characters must stay withheld."""
    limit = min(len(value), MAX_HOLDBACK)
    window = value[-limit:]
    for length in range(len(window), 0, -1):
        if is_viable_prefix(window[-length:]):
            return length
    return 0


def redact_stable(value: str, token: str) -> str:
    """Replace non-overlapping supported matches in text that cannot grow."""
    if not value:
        return ""
    spans = _spans(value)
    if not spans:
        return value
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(value[cursor:start])
        parts.append(token)
        cursor = end
    parts.append(value[cursor:])
    return "".join(parts)


def is_email_prefix(value: str) -> bool:
    """Return whether ``value`` is a prefix of an in-contract email."""
    if not value or len(value) > MAX_EMAIL_CHARS or not value[0].isalnum():
        return False
    at_index = value.find("@")
    if at_index == -1:
        return len(value) <= MAX_EMAIL_LOCAL and all(character in _LOCAL for character in value)
    if at_index == 0 or at_index > MAX_EMAIL_LOCAL:
        return False
    local = value[:at_index]
    if not local[0].isalnum() or not all(character in _LOCAL for character in local):
        return False
    return _domain_prefix(value[at_index + 1 :])


def is_ssn_prefix(value: str) -> bool:
    """Return whether ``value`` can still grow into a supported SSN."""
    if not value or len(value) > SSN_CHARS:
        return False
    for index, character in enumerate(value):
        if index in (3, 6):
            if character != "-":
                return False
        elif not character.isdigit():
            return False
    if len(value) >= 3:
        area = value[:3]
        if area in {"000", "666"} or area.startswith("9"):
            return False
    if len(value) >= 6 and value[4:6] == "00":
        return False
    if len(value) >= SSN_CHARS and value[7:11] == "0000":
        return False
    return len(value) < SSN_CHARS


def is_card_prefix(value: str) -> bool:
    """Return whether ``value`` can still grow into a supported card span."""
    if not value or len(value) > MAX_CARD_CHARS or not value[0].isdigit():
        return False
    digits = 0
    previous_separator = False
    for character in value:
        if character.isdigit():
            digits += 1
            previous_separator = False
            if digits > MAX_CARD_DIGITS:
                return False
            continue
        if character in " -" and digits and not previous_separator:
            previous_separator = True
            continue
        return False
    if digits == MAX_CARD_DIGITS and not previous_separator:
        return False
    return True


def _domain_prefix(domain: str) -> bool:
    if domain == "":
        return True
    if domain[0] in ".-":
        return False
    label_length = 0
    previous_dot = False
    for character in domain:
        if character == ".":
            if previous_dot or label_length == 0:
                return False
            previous_dot = True
            label_length = 0
            continue
        if character not in _DOMAIN:
            return False
        if character == "-" and (previous_dot or label_length == 0):
            return False
        previous_dot = False
        label_length += 1
        if label_length > 63:
            return False
    return True


def _spans(value: str) -> list[tuple[int, int]]:
    found = [*_email_spans(value), *_ssn_spans(value), *_card_spans(value)]
    found.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    chosen: list[tuple[int, int]] = []
    cursor = 0
    for start, end in found:
        if start < cursor or end <= start:
            continue
        chosen.append((start, end))
        cursor = end
    return chosen


def _email_spans(value: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(value)
    while index < length:
        end = _email_end(value, index)
        if end is None:
            index += 1
            continue
        spans.append((index, end))
        index = end
    return spans


def _email_end(value: str, start: int) -> int | None:
    if start >= len(value) or not value[start].isalnum():
        return None
    cursor = start
    local = 0
    while cursor < len(value) and value[cursor] in _LOCAL and value[cursor] != "@":
        local += 1
        if local > MAX_EMAIL_LOCAL:
            return None
        cursor += 1
    if cursor >= len(value) or value[cursor] != "@" or local < 1:
        return None
    cursor += 1
    domain_start = cursor
    label = 0
    previous_dot = False
    last_label_alpha = 0
    saw_dot = False
    best: int | None = None
    while cursor < len(value) and cursor - start <= MAX_EMAIL_CHARS:
        character = value[cursor]
        if character == ".":
            if previous_dot or label == 0:
                break
            previous_dot = True
            saw_dot = True
            label = 0
            last_label_alpha = 0
            cursor += 1
            continue
        if character not in _DOMAIN or (character == "-" and (previous_dot or label == 0)):
            break
        previous_dot = False
        label += 1
        if label > 63:
            break
        if character.isalpha():
            last_label_alpha += 1
        else:
            last_label_alpha = 0
        cursor += 1
        tld_ready = saw_dot and not previous_dot and label == last_label_alpha
        if tld_ready and 2 <= last_label_alpha <= 24:
            best = cursor
        if cursor - start == MAX_EMAIL_CHARS:
            break
    if best is None or best <= domain_start:
        return None
    return best


def _ssn_spans(value: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    while index + SSN_CHARS <= len(value):
        candidate = value[index : index + SSN_CHARS]
        if _is_ssn(candidate):
            spans.append((index, index + SSN_CHARS))
            index += SSN_CHARS
            continue
        index += 1
    return spans


def _is_ssn(value: str) -> bool:
    if len(value) != SSN_CHARS:
        return False
    for index, character in enumerate(value):
        if index in (3, 6):
            if character != "-":
                return False
        elif not character.isdigit():
            return False
    area, group, serial = value[:3], value[4:6], value[7:11]
    if area in {"000", "666"} or area.startswith("9"):
        return False
    if group == "00" or serial == "0000":
        return False
    return True


def _card_spans(value: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(value)
    while index < length:
        if not value[index].isdigit():
            index += 1
            continue
        end = _longest_luhn_end(value, index)
        if end is None:
            index = _end_of_card_shaped_run(value, index)
            continue
        spans.append((index, end))
        index = end
    return spans


def _end_of_card_shaped_run(value: str, start: int) -> int:
    """Skip one maximal digit run so a failed span is not scanned again."""
    cursor = start
    previous_separator = False
    while cursor < len(value):
        character = value[cursor]
        if character.isdigit():
            previous_separator = False
            cursor += 1
            continue
        if character in " -" and cursor > start and not previous_separator:
            previous_separator = True
            cursor += 1
            continue
        break
    if previous_separator:
        cursor -= 1
    return max(cursor, start + 1)


def _longest_luhn_end(value: str, start: int) -> int | None:
    """Match a whole card span, not a shorter Luhn window inside a longer run."""
    digits: list[str] = []
    previous_separator = False
    digit_end: int | None = None
    cursor = start
    while cursor < len(value) and cursor - start <= MAX_CARD_CHARS:
        character = value[cursor]
        if character.isdigit():
            if len(digits) == MAX_CARD_DIGITS:
                return None
            digits.append(character)
            previous_separator = False
            cursor += 1
            digit_end = cursor
            continue
        separator_ok = character in " -" and digits and not previous_separator
        if separator_ok and len(digits) < MAX_CARD_DIGITS:
            previous_separator = True
            cursor += 1
            continue
        break
    if digit_end is None or len(digits) < 13:
        return None
    if luhn_valid("".join(digits)):
        return digit_end
    return None
