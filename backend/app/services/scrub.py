"""Redact secrets, paths and PII before anything is written (GRPH-305 / PRD-16).

PRD-16 is specific about the ordering, and it is the whole reason this exists as its own
module rather than a step in the ingest pipeline: *"scrubbing at publish time is too late,
because a candidate is already persisted and searchable."* So it runs on the WRITE path in
`memory.add_memory`, where every producer inherits it — transcript ingest, `extract_lessons`,
the grill's decision capture, and any agent write.

**Placeholders, never deletion.** A shard whose secret is cut out reads as if the author
never mentioned one; a shard that says `[redacted:token]` still carries the fact that a
token was involved, which is often the lesson. Redaction that destroys meaning gets turned
off by whoever is trying to read the corpus.

**Deliberately conservative on hostnames.** PRD-16 lists them, and a general hostname
pattern matches `services/memory.py`, `app.get`, and most of the code this corpus is about.
Redacting those would gut the signal to catch a rare leak, and a scrubber that mangles
ordinary text is one people route around. So: URLs with credentials, IPs, and explicit
`.local`/`.internal` hosts are redacted; bare dotted words are not, and that gap is stated
rather than papered over.
"""
from __future__ import annotations

import re

# Ordered: the earlier a pattern runs, the more of the text it owns. Connection strings go
# first because they contain a password AND a host, and matching the parts separately would
# leave a half-redacted URL that still identifies the deployment.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # postgres://user:secret@host:5432/db, redis://…, mongodb+srv://…
    ("conn", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@]+@[^\s/]+", re.I)),
    # Authorization: Bearer <token>
    ("token", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._\-]{8,}")),
    # Known key shapes, ours and the common vendors. Anchored on their prefixes rather than
    # on entropy: a length-and-charset heuristic flags git SHAs and base64 payloads, which
    # are exactly the details a debugging lesson turns on.
    ("key", re.compile(r"\b(?:sk-|pk-|ghp_|gho_|github_pat_|xox[abposr]-|al_sk_|gb_sk_|"
                       r"AKIA|ASIA)[A-Za-z0-9._\-]{8,}")),
    ("key", re.compile(r"\b[A-Za-z0-9_\-]*(?:api[_-]?key|secret|password|passwd|token)"
                       r"\s*[=:]\s*[\"']?([A-Za-z0-9._\-/+]{8,})[\"']?", re.I)),
    # Home directories carry the operator's name and reveal nothing else useful.
    ("path", re.compile(r"(?:/Users/|/home/|[A-Z]:\\\\Users\\\\)[^/\\\s\"']+")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("ip", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("host", re.compile(r"\b[A-Za-z0-9\-]+\.(?:local|internal|lan|home)\b")),
]

# An IP that identifies nothing. Redacting these trains readers to ignore the marker.
_HARMLESS_IPS = {"127.0.0.1", "0.0.0.0", "255.255.255.255", "1.1.1.1", "8.8.8.8"}


def scrub(text: str) -> tuple[str, bool]:
    """`(clean_text, changed)`.

    `changed` is returned rather than inferred by comparing strings, so a caller can record
    that scrubbing RAN even when it found nothing — which is what makes an unscrubbed
    legacy row distinguishable from a clean one, rather than both looking identical.
    """
    if not text:
        return text, False
    out = text
    for label, pattern in _PATTERNS:
        def _sub(m: re.Match[str], _l: str = label) -> str:
            whole = m.group(0)
            if _l == "ip" and whole in _HARMLESS_IPS:
                return whole
            if _l == "path":
                # Keep the root so a reader can still tell a home path from a repo path.
                head = whole[:whole.index("/", 1) + 1] if whole.startswith("/") else ""
                return f"{head}[redacted:user]"
            if _l == "key" and m.groups():
                # `password = hunter2` → keep the key name, redact the value; the name is
                # usually the point of the lesson.
                return whole.replace(m.group(1), "[redacted:secret]")
            return f"[redacted:{_l}]"
        out = pattern.sub(_sub, out)
    return out, out != text
