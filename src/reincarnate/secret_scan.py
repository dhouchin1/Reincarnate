"""Redact credentials from donor text before a model reads it or the store keeps it.

The vendor patterns are adapted from Code Read's built-in secret rules
(cli/src/security/diffSecret.ts, https://github.com/dhouchin1/coderead, Apache License 2.0, same
author). Like there, precision beats recall: each pattern is anchored on a vendor prefix or a
structural marker. One generic rule catches quoted `password = "..."` style assignments.

A secret value is never stored, returned or printed: it is replaced in place by
`[REDACTED:<rule-id>]` and only the per-rule counts survive.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase

# (rule id, pattern, group holding the secret; 0 = the whole match)
RULES: list[tuple[str, re.Pattern, int]] = [
    ("aws-access-key-id",
     re.compile(r"\b((?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16})\b"), 1),
    ("aws-secret-access-key",
     re.compile(r"\baws_secret_access_key[\"'\s:=]+([A-Za-z0-9/+=]{40})\b", re.IGNORECASE), 1),
    ("github-token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,255})\b"), 1),
    ("gitlab-token", re.compile(r"\b(glpat-[A-Za-z0-9_-]{20,})\b"), 1),
    ("slack-token", re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})\b"), 1),
    ("stripe-secret-key", re.compile(r"\b(sk_(?:live|test)_[A-Za-z0-9]{16,})\b"), 1),
    # before the OpenAI rule, whose pattern also matches `sk-ant-...`
    ("anthropic-api-key", re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{24,})\b"), 1),
    ("openai-api-key", re.compile(r"\b(sk-(?:proj-)?[A-Za-z0-9_-]{32,})\b"), 1),
    ("google-api-key", re.compile(r"\b(AIza[A-Za-z0-9_-]{35})\b"), 1),
    ("sendgrid-api-key",
     re.compile(r"\b(SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43})\b"), 1),
    ("twilio-api-key", re.compile(r"\b(SK[0-9a-fA-F]{32})\b"), 1),
    ("npm-token", re.compile(r"\b(npm_[A-Za-z0-9]{36})\b"), 1),
    # the whole block through its END line (or to the end of the text when it has none)
    ("private-key-block",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----"
                r"(?:.*?-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|.*\Z)", re.DOTALL), 0),
    ("connection-string-password",
     re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:@/]+:"
                r"([^\s:@/]{8,})@"), 1),
    ("generic-assignment",
     re.compile(r"(?:password|passwd|secret|token|api_key|apikey|access_key)[\"']?\s*[:=]\s*"
                r"[\"']([^\"'\s]{8,})[\"']", re.IGNORECASE), 1),
]

# Values that look like a secret but are documentation or test placeholders (Code Read's list).
_PLACEHOLDER = re.compile(
    r"(x{8,}|\.{3,}|<[^>]+>|\$\{[^}]+\}|example|placeholder|redacted|changeme"
    r"|your[-_]?(?:key|token|secret)|dummy|fake|sample|test[-_]?key|not[-_]?a[-_]?real)",
    re.IGNORECASE)

# File names that are credentials by name; such files are never excerpted.
SECRET_FILE_GLOBS = ("*secret*", "*credential*", "*.pem", "*.key", "id_rsa*")


def is_secrets_file(name: str) -> bool:
    low = str(name or "").lower()
    return any(fnmatchcase(low, g) for g in SECRET_FILE_GLOBS)


def _sub(rule_id: str, pat: re.Pattern, group: int, text: str, counts: dict) -> str:
    def repl(m: re.Match) -> str:
        value = m.group(group)
        if group and (value.startswith("[REDACTED:") or _PLACEHOLDER.search(value)):
            return m.group(0)
        counts[rule_id] = counts.get(rule_id, 0) + 1
        mark = f"[REDACTED:{rule_id}]"
        if not group:
            return mark
        s = m.start(group) - m.start(0)
        return m.group(0)[:s] + mark + m.group(0)[s + len(value):]

    return pat.sub(repl, text)


def redact(text) -> tuple[str, dict[str, int]]:
    """-> (text with every secret replaced by `[REDACTED:<rule-id>]`, {rule id: count})."""
    out = text if isinstance(text, str) else ("" if text is None else str(text))
    counts: dict[str, int] = {}
    for rule_id, pat, group in RULES:
        out = _sub(rule_id, pat, group, out, counts)
    return out, counts


def redact_value(obj, counts: dict[str, int] | None = None):
    """`redact` over every string in a JSON-like structure (dict keys are left as they are).
    -> (new structure, counts); `counts` is updated in place when given."""
    counts = {} if counts is None else counts

    def walk(v):
        if isinstance(v, str):
            new, c = redact(v)
            for k, n in c.items():
                counts[k] = counts.get(k, 0) + n
            return new
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    return walk(obj), counts
