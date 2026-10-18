import re

NRIC = re.compile(r"\b[STFGM]\d{7}[A-Z]\b", re.I)
NATIONAL_ID = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
PASSWORD = re.compile(
    r"""(?:password|密码|口令)["']?\s*(?:is|是|为|:|=|：)\s*["']?([^\s，,；;。！？!?"'<>]+)""",
    re.I,
)
TAGS = re.compile(r"<[^>]{0,300}>")


def sensitive_values(text: str) -> tuple[str, ...]:
    found = [m.group(0) for pattern in (NRIC, NATIONAL_ID, CARD) for m in pattern.finditer(text)]
    found.extend(
        m.group(1) for m in PASSWORD.finditer(text) if not m.group(1).startswith("[REDACTED_")
    )
    return tuple(dict.fromkeys(found))


def redact(text: str, known: tuple[str, ...] = ()) -> str:
    for value in sorted(known, key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED_PII]")
    for pattern, label in ((NRIC, "ID"), (NATIONAL_ID, "ID"), (CARD, "CARD")):
        text = pattern.sub(f"[REDACTED_{label}]", text)
    text = PASSWORD.sub(
        lambda m: (
            m.group(0)
            if m.group(1).startswith("[REDACTED_")
            else m.group(0).replace(m.group(1), "[REDACTED_PASSWORD]")
        ),
        text,
    )
    return text


def sanitize(text: str, known: tuple[str, ...] = ()) -> str:
    return TAGS.sub("[UNTRUSTED_TAG]", redact(text, known))
