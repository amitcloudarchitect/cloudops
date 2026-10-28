"""Redaction before any model call (LLD, Secret handling; Gateway controls).

Every message content — the user's text, the system prompt, retrieved chunks
and tool results — passes through ``redact`` before it reaches a provider,
and so does every text sent for embedding. Secrets go first (keys, tokens,
connection strings, private keys), then personal data (email, phone, card
numbers, IBAN, UK National Insurance numbers), then any known personal
values the caller supplies (for example the names of people on a ticket),
because names cannot be found reliably by pattern.

Placeholders name the kind: ``[REDACTED:EMAIL]``. The original is never kept
by the gateway, never written to the trace, and never logged.

This reduces what can leak; it does not make leaking impossible. Free text
can carry personal data no pattern recognises, which is why tools return
structured fields and why the trace stores only the redacted prompt.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final


def _luhn(digits: str) -> bool:
    nums = [int(d) for d in digits if d.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _iban(value: str) -> bool:
    v = value.replace(" ", "").upper()
    if not 15 <= len(v) <= 34:
        return False
    rearranged = v[4:] + v[:4]
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    return int(numeric) % 97 == 1


@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]
    valid: Callable[[str], bool] | None = None
    keep_prefix_group: int | None = None


# Order matters: secrets before personal data, longer forms before shorter.
RULES: Final = (
    _Rule("PRIVATE_KEY", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    _Rule("CONNECTION_STRING", re.compile(
        r"(?i)\b(?:DefaultEndpointsProtocol|Server|Host|Data Source)=[^\n]*?"
        r"(?:AccountKey|Password|Pwd|SharedAccessKey)=[^;\s]+[^\n]*")),
    _Rule("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    _Rule("BEARER_TOKEN", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    _Rule("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    _Rule("URL_CREDENTIALS", re.compile(r"\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"),
          keep_prefix_group=1),
    _Rule("SECRET", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|client[_-]?secret"
        r"|token|sas|accountkey|sharedaccesskey)\b\s*[:=]\s*[\"']?[^\s\"',;]{4,}")),
    _Rule("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    _Rule("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b"),
          _iban),
    _Rule("CARD_NUMBER", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), _luhn),
    _Rule("UK_NI_NUMBER", re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2} ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b")),
    _Rule("PHONE", re.compile(r"(?<![\w.])\+\d{1,3}[\s.-]?(?:\(?\d+\)?[\s.-]?){2,5}\d{2,}(?!\w)")),
)


@dataclass(frozen=True)
class Redacted:
    text: str
    kinds: Counter[str]

    @property
    def count(self) -> int:
        return sum(self.kinds.values())


def redact(text: str, personal_terms: Iterable[str] = ()) -> Redacted:
    kinds: Counter[str] = Counter()
    out = text
    for rule in RULES:
        def replace(m: re.Match[str], rule: _Rule = rule) -> str:
            if rule.valid is not None and not rule.valid(m.group(0)):
                return m.group(0)
            kinds[rule.kind] += 1
            if rule.keep_prefix_group:
                return f"{m.group(rule.keep_prefix_group)}[REDACTED:{rule.kind}]@"
            return f"[REDACTED:{rule.kind}]"

        out = rule.pattern.sub(replace, out)
    # Known personal values, longest first so "Asha Patel" goes before "Asha".
    for term in sorted({t.strip() for t in personal_terms if len(t.strip()) >= 3},
                       key=len, reverse=True):
        pattern = re.compile(rf"(?i)(?<!\w){re.escape(term)}(?!\w)")
        out, n = pattern.subn("[REDACTED:PERSON]", out)
        if n:
            kinds["PERSON"] += n
    return Redacted(out, kinds)
