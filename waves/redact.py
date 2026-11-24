"""Rules-first redaction of the 18 HIPAA Safe Harbor identifiers.

Regex covers the structured identifiers (numbers, dates, contacts, addresses,
labelled names). Names in free text without a label are what the model adds
(task wave1.billing.redact_names, local or rules only: raw PHI never leaves
the machine). Every hit is replaced by a [TAG]; the tags are counted.

Safe Harbor list -> tag: 1 names NAME; 2 geography below state ADDRESS/ZIP;
3 dates (and ages over 89) DATE/AGE; 4 phone and 5 fax PHONE; 6 email EMAIL;
7 SSN SSN; 8 MRN MRN; 9 health plan beneficiary number PLAN_ID; 10 account
ACCOUNT; 11 certificate/license LICENSE; 12 vehicle id/plate VEHICLE;
13 device id/serial DEVICE; 14 URL URL; 15 IP IP; 16 biometric BIOMETRIC;
17 photo PHOTO; 18 any other unique id ID.
"""
import re

MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
NAME_WORD = r"[A-Z][a-zA-Z'\-]+"
# "Given Family", "Family, Given", "Given Q. Family", "Given Family-Two"
NAME = rf"{NAME_WORD}(?:,?\s+(?:[A-Z]\.\s+)?{NAME_WORD}){{0,2}}"
NAME_LABELS = (r"wristband(?: reads| says|:)?|report for|patient(?: name)?(?: is)?:?|pt(?: name)?:|name:|named|"
               r"identified as|pt is|known as|signed by|"
               r"mr\.?|mrs\.?|ms\.?|miss|dr\.?|"
               r"(?:wife|husband|spouse|daughter|son|mother|father|sister|brother|partner|guardian|son-in-law|daughter-in-law)"
               r"(?:,)?(?: named)?")
STREET = (r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Ct|Court|Way|Spur|Rise|Pl|Place|Cir|Circle|"
          r"Ter|Terrace|Trl|Trail|Mews|Pkwy|Parkway|Hwy|Highway|Loop|Row|Run|Pass|Crossing|Sq|Square)\b\.?")

# Names may be introduced by conversational context or by a following verb.
PERSON = rf"{NAME_WORD}(?:[ \t]+{NAME_WORD})?"
CONTEXT_NAMES = [
    re.compile(rf"(?i:\b(?:spoke with|asked for|nickname|goes by|we used|between breaths))\s+[“\"']?({PERSON})"),
    re.compile(rf"\b({PERSON})\s+(?i:received (?:the )?handoff|goes by|squeezed my hand|used the (?:old )?nickname)\b"),
]
DIGIT_WORD = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)"
ORDINAL = r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|[a-z]+teenth|twentieth|thirtieth|(?:twenty|thirty)[- ](?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth))"

# (tag, pattern). Order matters: specific before general.
RULES = [
    ("DATE", re.compile(rf"\b(?:the )?{ORDINAL} of {MONTHS},?\s+[’']?\d{{2,4}}\b", re.I)),
    ("AGE", re.compile(r"\b(?:age[d]?|now)\s+(?:ninety(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?)\b", re.I)),
    ("SSN", re.compile(r"\b(?:SS(?:N| card)?|social security)\s+(?:suffix|ends(?: in)?|last four)\s*[:#]?\s*\d{4}\b", re.I)),
    ("MRN", re.compile(r"\b(?:wristband|band)(?: copied as)?\s+[A-Z]{1,4}-\d+(?:\s+\d+)?-[A-Z0-9]+\b", re.I)),
    ("PHONE", re.compile(rf"\b{DIGIT_WORD}(?:[ -]+{DIGIT_WORD}){{6,14}}\b", re.I)),
    ("VEHICLE", re.compile(r"\b(?:[Pp]arking slip shows|[Cc]ar at the gate wore|[Pp]late)\s+[A-Z]{1,3}-\d{3,6}-[A-Z]\b")),
    ("DEVICE", re.compile(r"\b[A-Z]{1,4}/\d+/[A-Z0-9]+\b")),

    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("URL", re.compile(r"\b(?:https?://|www\.)\S+", re.I)),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b|(?<=SSN )\d{9}\b|\b(?:SSN|social security(?: number| no\.?)?)\s*[:#]?\s*\d[\d -]{7,10}\d", re.I)),
    ("PHONE", re.compile(r"(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s*|\b\d{3}[\s.-])\d{3}[\s.-]\d{4}\b(?:\s*(?:ext\.?|extension|x)\s*\d+)?")),
    ("MRN", re.compile(
        r"(?:(?<=\bMRN)|(?<=\bMR#)|(?<=chart #)|(?<=chart)|(?<=record)|(?<=record #)|(?<=record number))\s*[:#-]?\s*[A-Z]{0,3}[/-]?\d[\w/-]*"
        r"|\bMR-\d{3,}(?:-[A-Z])?\b|\b[A-Z]{2}/\d{4,}\b|\b[A-Z]{2}\d{6,}\b", re.I)),
    ("PLAN_ID", re.compile(r"(?<=\b)(?:member|beneficiary|subscriber|policy|plan|insurance|medicare|medicaid)\s*(?:id|#|no\.?|number)\s*[:#]?\s*[A-Z0-9][\w-]{3,}", re.I)),
    ("ACCOUNT", re.compile(r"\b(?:acct|account)\s*(?:#|no\.?|number)?\s*[:#]?\s*[A-Z0-9][\w-]{3,}", re.I)),
    ("LICENSE", re.compile(r"\b(?:driver'?s? license|DL|license|licence|certificate)\s*(?:#|no\.?|number)\s*[:#]?\s*[A-Z0-9][\w-]{3,}", re.I)),
    ("VEHICLE", re.compile(r"\b(?:VIN\s*[:#]?\s*[A-HJ-NPR-Z0-9]{11,17}|(?:license )?plate\s*(?:#|no\.?|number)?\s*[:#]?\s*[A-Z0-9][A-Z0-9 -]{3,8}[A-Z0-9])", re.I)),
    ("DEVICE", re.compile(r"\b(?:serial|S/N|SN|device id|implant id|pacemaker id)\s*(?:#|no\.?|number)?\s*[:#]?\s*[A-Z0-9][\w-]{3,}", re.I)),
    ("DATE", re.compile(
        r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"
        rf"|\b\d{{1,2}}(?:st|nd|rd|th)? {MONTHS}\.?,? \d{{4}}\b|\b{MONTHS}\.? \d{{1,2}}(?:st|nd|rd|th)?,? \d{{4}}\b"
        rf"|\b{MONTHS}\.? \d{{4}}\b|\b\d{{1,2}}/\d{{4}}\b")),
    ("AGE", re.compile(r"\b(?:9\d|1[0-4]\d)\s*(?:yo|y/o|yr?s?\.? old|year[- ]old|years? of age)\b", re.I)),
    ("ADDRESS", re.compile(
        rf"(?<=Residence: )[^.\n]+|(?<=Home listed as )[^.\n]+|(?<=Address: )[^.\n]+|(?<=lives at )[^.\n]+|(?<=resides at )[^.\n]+"
        rf"|\b\d{{1,6}}\s+(?:[NSEW]\.?\s+)?(?:{NAME_WORD}\s+){{1,3}}{STREET}(?:,?\s*(?:Apt|Unit|#)\s*\w+)?", re.I)),
    ("ZIP", re.compile(r"(?<=[A-Z]{2} )\d{5}(?:-\d{4})?\b|\b(?:zip|zip code|postal code)\s*[:#]?\s*\d{5}(?:-\d{4})?\b", re.I)),
    ("BIOMETRIC", re.compile(r"\b(?:fingerprint|retina(?:l)? scan|voice ?print)\s*(?:id|#)?\s*[:#]?\s*[A-Z0-9][\w-]{3,}", re.I)),
    ("PHOTO", re.compile(r"\b(?:photo|image) (?:file|id)\s*[:#]?\s*\S+", re.I)),
]
LABELED_NAME = re.compile(rf"(?i:\b(?:{NAME_LABELS})\s+)({NAME})")
# Capitalised words that a label may precede but that are not names.
NOT_NAMES = {"stemi", "gcs", "vt", "als", "bls", "icu", "ed", "er", "md", "rn", "dx", "pt", "the", "a", "an", "no",
             "cath", "sending", "receiving", "physician", "certification", "pending", "flight", "crew", "ground",
             "transfer", "patient", "sp", "map", "bp", "hr", "spo2", "shock", "head", "respiratory", "unknown"}


def _clean_name(span):
    """Trim label-ish words (STEMI, Sending...) off both ends; keep the
    original spelling in between so the full span can be matched again."""
    span = span.strip().rstrip(",")
    while True:
        m = re.match(r"([\w'-]+)[,.]?\s*", span)
        if m and m.group(1).lower() in NOT_NAMES:
            span = span[m.end():]
            continue
        m = re.search(r"[,\s]+([\w'-]+)\.?$", span)
        if m and m.group(1).lower() in NOT_NAMES:
            span = span[:m.start()]
            continue
        break
    return span.strip().rstrip(",")


def rule_names(text):
    """Names the rules can see: words after a name label (wristband, patient,
    Mr./Dr., relatives...). The model task adds unlabelled names."""
    out = []
    for pattern in (LABELED_NAME, *CONTEXT_NAMES):
        for match in pattern.finditer(text):
            name = _clean_name(match.group(1))
            if name and name[0].isupper() and name.lower() not in NOT_NAMES:
                out.append(name)
    return sorted(set(out))


def _name_patterns(names):
    """Each full name and each of its words (>= 3 letters) wherever they occur,
    so 'Drelwick, Talviq' found once also removes a later bare 'Talviq'."""
    toks = set()
    for n in names:
        n = n.strip()
        if not n:
            continue
        toks.add(n)
        for w in re.split(r"[\s,]+", n):
            w = w.strip(".")
            if len(w) >= 3 and w[0].isupper() and w.lower() not in NOT_NAMES:
                toks.add(w)
    return [re.compile(r"(?<![\w])" + re.escape(t) + r"(?![\w])") for t in sorted(toks, key=len, reverse=True)]


def redact(text, names=()):
    """Return (deid_text, counts_by_tag). `names` are extra names (from the
    model) on top of the labelled ones the rules find."""
    counts = {}
    out = text or ""

    def sub(tag, pat, s):
        def rep(m):
            counts[tag] = counts.get(tag, 0) + 1
            g = m.group(0)
            return g[:len(g) - len(g.lstrip())] + f"[{tag}]"
        return pat.sub(rep, s)

    all_names = set(rule_names(out)) | {n for n in names if n and n in out}
    for tag, pat in RULES:
        out = sub(tag, pat, out)
    for pat in _name_patterns(all_names):
        out = sub("NAME", pat, out)
    return out, counts
