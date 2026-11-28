"""Credentials an edit writes, for the edit hook. Only the lines an edit adds or changes are read, and only for shapes that
cannot be anything else (a provider's token prefix with its fixed length, an AWS key pair line, a private-key header at
the start of a line): a false alarm costs more than a silence here, so a general "high entropy string" rule is not used.
Lines that say they are an example are skipped. The matched text is never repeated in a note."""
import re

import doc_check

_PLACEHOLDER = re.compile(r"example|placeholder|your[_-]|dummy|fake|redacted|x{6,}|\*{4,}|<[^>]*>", re.I)
_PATTERNS = (
    ("private key", re.compile(r"^\s*-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("AWS secret access key", re.compile(r"aws_secret_access_key[\"']?\s*[=:]\s*[\"']?[A-Za-z0-9/+=]{40}\b", re.I)),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe live key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{24,}")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
)


def review(before, after):
    """Credentials among the lines an edit adds or changes.

    Parameters: before (str or None) text before the edit; after (str) text after it.
    Returns: list of dicts with line (1-based) and kind, in line order, one per line; the secret itself is not included.
    """
    lines = after.split("\n")
    findings = []
    for number in sorted(doc_check._touched_lines(before, after)):
        if number > len(lines) or _PLACEHOLDER.search(lines[number - 1]):
            continue
        for kind, pattern in _PATTERNS:
            if pattern.search(lines[number - 1]):
                findings.append({"line": number, "kind": kind})
                break
    return findings


def redact(text):
    """`text` with every credential of the shapes `review` knows replaced by `<redacted: kind>`, line by line.

    Unlike `review` it does not skip lines that say they are an example: a copy that hides one secret too many costs
    nothing, one that lets a secret through does. Parameters: text (str). Returns: the redacted str.

    Technical doc: docs/scope/technical/_root.md#credential_check.py::redact"""
    lines = []
    for line in text.split("\n"):
        for kind, pattern in _PATTERNS:
            line = pattern.sub(f"<redacted: {kind}>", line)
        lines.append(line)
    return "\n".join(lines)
