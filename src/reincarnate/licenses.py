"""License handling in one place: normalize free text to an SPDX-style id, compare against policy.

Donor licenses arrive as prose ("AGPL-3.0 (NOTE: package.json says MIT ...)"). Nothing outside
this module compares raw license strings; `normalize` is applied on every write and `level`
on every policy check, so annotated, long-name and "-only"/"-or-later" variants cannot slip
past a deny or warn list.
"""

from __future__ import annotations

import re

UNKNOWN = "unknown"
PROPRIETARY = "proprietary"

_KNOWN_IDS = (
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense", "Zlib",
    "WTFPL", "CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "CC-BY-NC-4.0", "CC-BY-NC-SA-4.0",
    "GPL-2.0", "GPL-3.0", "LGPL-2.1", "LGPL-3.0", "AGPL-3.0", "MPL-2.0", "EUPL-1.2",
    "SSPL-1.0", "BUSL-1.1", "BSL-1.0", "FSL-1.1-MIT", "FSL-1.1-Apache-2.0", "Python-2.0",
    "Artistic-2.0", "EPL-2.0",
)
_CANON = {i.lower(): i for i in _KNOWN_IDS}
_SUFFIX = re.compile(r"(-only|-or-later|\+)$", re.I)
# an id we did not list but that is shaped like one: letters, then a hyphenated version part
_ID_SHAPE = re.compile(r"^(?=.*-\d)[A-Za-z][A-Za-z0-9.]*(-[A-Za-z0-9.]+)*\+?$")
_UNKNOWN_START = re.compile(
    r"^(unknown|none|no\s+licen[sc]e|noassertion|n/a|other)\b", re.I)
_PROPRIETARY = re.compile(
    r"proprietary|all\s+rights\s+reserved|commercial\s+licen[sc]e|non-?\s?commercial", re.I)
_OPS = {"OR", "AND", "WITH"}


def _gnu(prefix: str, default: str):
    def build(m: re.Match) -> str:
        ver = m.group("v") or default
        return f"{prefix}-{ver if '.' in ver else ver + '.0'}"
    return build


# long names at the start of the text; the value is an id or a builder taking the match
_LONG_NAMES = (
    (re.compile(r"^(the\s+)?gnu\s+affero\s+general\s+public\s+licen[sc]e(,?\s*(version|v)?\s*"
                r"(?P<v>\d(\.\d)?))?", re.I), _gnu("AGPL", "3")),
    (re.compile(r"^(the\s+)?gnu\s+lesser\s+general\s+public\s+licen[sc]e(,?\s*(version|v)?\s*"
                r"(?P<v>\d(\.\d)?))?", re.I), _gnu("LGPL", "3")),
    (re.compile(r"^(the\s+)?gnu\s+(library\s+)?general\s+public\s+licen[sc]e(,?\s*(version|v)?\s*"
                r"(?P<v>\d(\.\d)?))?", re.I), _gnu("GPL", "3")),
    (re.compile(r"^(the\s+)?apache(\s+software)?(\s+licen[sc]e)?[,\s]*(version\s*|v)?2(\.0)?\b",
                re.I), "Apache-2.0"),
    (re.compile(r"^(the\s+)?mit\s+licen[sc]e", re.I), "MIT"),
    (re.compile(r"^mozilla\s+public\s+licen[sc]e[,\s]*(version\s*|v)?2(\.0)?", re.I), "MPL-2.0"),
    (re.compile(r"^isc\s+licen[sc]e", re.I), "ISC"),
    (re.compile(r"^the\s+unlicense|^unlicense\b", re.I), "Unlicense"),
    (re.compile(r"^(the\s+)?((new|revised|modified)\s+)?bsd[\s-]*3[\s-]*clause|^3-clause\s+bsd",
                re.I), "BSD-3-Clause"),
    (re.compile(r"^(the\s+)?(simplified\s+|freebsd\s+)?bsd[\s-]*2[\s-]*clause|^2-clause\s+bsd",
                re.I), "BSD-2-Clause"),
    (re.compile(r"^creative\s+commons\s+zero|^cc0\b", re.I), "CC0-1.0"),
)


def _canon_id(tok: str) -> str:
    """Canonical casing for a recognised id (suffix kept), '' when `tok` is not an id."""
    tok = tok.strip().rstrip(".,;:")
    suffix = _SUFFIX.search(tok)
    base = tok[:suffix.start()] if suffix else tok
    known = _CANON.get(base.lower())
    if known:
        return known + (suffix.group(1).lower() if suffix else "")
    if _ID_SHAPE.match(tok):
        return tok
    return ""


def _leading_expression(s: str) -> str:
    """The SPDX expression a text starts with ('' when it does not start with an id)."""
    head = re.split(r"\s+[—–-]\s+|[—–;]|\.\s|\s*\(", s, maxsplit=1)[0].strip()
    toks = head.split()
    out: list[str] = []
    i = 0
    while i < len(toks):
        if out:
            op = toks[i].upper()
            if op not in _OPS or i + 1 >= len(toks):
                break
            if op == "WITH":
                out += ["WITH", toks[i + 1].rstrip(".,;:")]
                i += 2
                continue
            ident = _canon_id(toks[i + 1])
            if not ident:
                break
            out += [op, ident]
            i += 2
            continue
        ident = _canon_id(toks[i])
        if not ident:
            return ""
        out.append(ident)
        i += 1
    return " ".join(out)


def normalize(raw) -> str:
    """Free text to an SPDX-style id or expression, 'proprietary' or 'unknown'; never prose."""
    s = str(raw or "").strip()
    if len(s) > 2 and s[0] == "(" and s[-1] == ")" and s.count("(") == 1:
        s = s[1:-1].strip()
    if not s or _UNKNOWN_START.match(s):
        return UNKNOWN
    for pat, value in _LONG_NAMES:
        m = pat.match(s)
        # "BSD-2-Clause OR MIT" is an expression, not the long name "BSD 2-clause"
        if m and not re.match(r"\s+(OR|AND|WITH)\s", s[m.end():], re.I):
            return value(m) if callable(value) else value
    expr = _leading_expression(s)
    if expr:
        return expr
    if _PROPRIETARY.search(s):
        return PROPRIETARY
    return UNKNOWN


def family(license_id: str) -> str:
    """Lower-case id without -only, -or-later or a trailing + (GPL-3.0-or-later -> gpl-3.0)."""
    return _SUFFIX.sub("", str(license_id or "").strip()).lower()


def policy_entry(entry) -> str:
    """A policy entry normalized like a license; a bare name such as "GPL" stays as typed."""
    s = str(entry or "").strip()
    n = normalize(s)
    if n == UNKNOWN and s and not _UNKNOWN_START.match(s):
        return s
    return n


def _options(expr: str) -> list[list[str]]:
    """OR alternatives, each a list of AND-ed ids (WITH exceptions dropped)."""
    alts = []
    for alt in re.split(r"\s+OR\s+", expr):
        ids = []
        for part in re.split(r"\s+AND\s+", alt):
            ids.append(part.split(" WITH ")[0].strip())
        alts.append(ids)
    return alts


_RANK = {None: 0, "warn": 1, "deny": 2}


def _option_level(ident: str, deny: set[str], warn: set[str]) -> str | None:
    fam = family(ident)
    if fam in deny:
        return "deny"
    if fam in warn or fam in (UNKNOWN, PROPRIETARY):
        return "warn"
    return None


def level(raw, policy) -> str | None:
    """'deny', 'warn' or None. OR: the user may pick the best option; AND: the worst applies."""
    pol = policy if isinstance(policy, dict) else {}
    deny = {family(policy_entry(x)) for x in pol.get("deny") or [] if str(x).strip()}
    warn = {family(policy_entry(x)) for x in pol.get("warn") or [] if str(x).strip()}
    best = None
    for n, ids in enumerate(_options(normalize(raw))):
        worst = max((_option_level(i, deny, warn) for i in ids), key=_RANK.__getitem__)
        if n == 0 or _RANK[worst] < _RANK[best]:
            best = worst
    return best


# families that need a human look before reuse even when the policy does not name them
_REVIEW_PREFIXES = ("gpl", "agpl", "lgpl", "mpl", "eupl", "sspl", "fsl", "busl", "bsl-1.1",
                    "cc-by-nc", PROPRIETARY, UNKNOWN)


def is_review(raw) -> bool:
    """Copyleft, source-available, non-commercial, proprietary or unknown licenses."""
    def risky(ident: str) -> bool:
        return family(ident).startswith(_REVIEW_PREFIXES)
    return min(any(risky(i) for i in ids) for ids in _options(normalize(raw)))
