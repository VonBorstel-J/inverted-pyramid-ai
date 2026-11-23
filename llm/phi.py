"""PHI guard: refuse to send text that carries a planted-identifier pattern to a
non-local provider. Section 5 of ARCHITECTURE.md (Tracing)."""
import re

# Patterns that mark raw PHI. De-identified text has these replaced by [TAGS].
PATTERNS = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\bSSN\s*[:#]?\s*\d{9}\b", re.I),
    "mrn": re.compile(
        r"\b(?:MRN|MR#|chart\s*#|medical record(?: number| no\.?| #)?|sending record|record #)\s*[:#-]?\s*[A-Z]{0,3}[/-]?\d{4,}"
        r"|\bMR-\d{4,}(?:-[A-Z])?\b|\b[A-Z]{2}/\d{5,}\b", re.I),
    "phone": re.compile(r"(?:\(\d{3}\)\s*|\b\d{3}[-.\s])\d{3}[-.]\d{4}\b"),
    "dob": re.compile(
        r"\b(?:DOB|D\.O\.B\.?|date of birth|birth ?date|born)\b[^.\n\d]{0,25}"
        r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2,8}\.? \d{1,2},? \d{4}"
        r"|\d{1,2} [A-Z][a-z]{2,8}\.? \d{4})", re.I),
    "slash_date": re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"),
}

# Providers that may see raw PHI: they run on this machine.
PHI_SAFE_PROVIDERS = frozenset({"local", "rules"})


class PHIGuardError(RuntimeError):
    """Raised when text with an identifier pattern is headed to a cloud provider."""


def find_phi(text):
    """Return the sorted list of identifier kinds found in text (empty if clean)."""
    if not text:
        return []
    return sorted(k for k, p in PATTERNS.items() if p.search(text))


def check(provider, text):
    """Raise PHIGuardError when provider is not PHI-safe and text holds PHI."""
    if provider in PHI_SAFE_PROVIDERS:
        return
    kinds = find_phi(text)
    if kinds:
        raise PHIGuardError(f"phi_guard: refused {provider}: input holds {','.join(kinds)}")
