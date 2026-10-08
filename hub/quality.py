"""Deterministic quality gates for translation batch proposals.

The score measures structural and content integrity signals. It is not a
machine estimate of linguistic fluency or semantic fidelity.
"""
import os
import re
from collections import Counter
from html.parser import HTMLParser


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag.lower(), False))

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag.lower(), True))


def _html_tags(value):
    parser = _Tags()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return None
    return Counter(parser.tags)


# Double quotation marks in the usual typographic styles (straight, German,
# English, French) plus their HTML entity forms.
_QUOTE_CHARS = '"„“”‟«»'
_QUOTE_ENTITIES = re.compile(r"&(?:quot|ldquo|rdquo|bdquo|laquo|raquo|#34|#8220|#8221|#8222|#171|#187);", re.I)


def has_double_quotes(value, is_html=False):
    # Attribute quotes inside tags are markup, not text.
    value = re.sub(r"<[^>]*>", " ", value or "") if is_html else (value or "")
    return any(ch in _QUOTE_CHARS for ch in value) or bool(_QUOTE_ENTITIES.search(value))


def strip_added_quotes(source, translation, is_html=False):
    """Remove double quotes the translator added around words.

    DeepL often wraps model names in quotes (e.g. German „Lola Nature“). When the
    source has no double quotes at all, any in the translation were added by the
    engine, so they are dropped from text (never from HTML tags or attributes).
    """
    if not translation or has_double_quotes(source, is_html) or not has_double_quotes(translation, is_html):
        return translation

    def clean(text):
        text = _QUOTE_ENTITIES.sub("", text)
        text = re.sub(r"«[\s\u00a0\u202f]*", "", text)
        text = re.sub(r"[\s\u00a0\u202f]*»", "", text)
        return "".join(ch for ch in text if ch not in _QUOTE_CHARS)

    if not is_html:
        return clean(translation)
    return "".join(part if (part.startswith("<") and part.endswith(">")) else clean(part)
                   for part in re.split(r"(<[^>]+>)", translation))


def _numbers(value):
    # Normalize spaces and unit case while retaining decimal punctuation.
    pattern = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)*)(?:\s?(mm|cm|kg|lm|°c|m|g|w|v|%))?", re.I)
    return Counter((match.group(1).replace(',', '.'), (match.group(2) or '').lower()) for match in pattern.finditer(value))


def _placeholders(value):
    patterns = (r"\{\{[^{}]+\}\}", r"\{[A-Za-z_][\w.-]*\}", r"%[sdif]", r"\[[A-Z0-9_ -]{2,}\]")
    return Counter(match for pattern in patterns for match in re.findall(pattern, value))


def assess_translation(source, proposal, *, is_html=False):
    """Return PASS/REVIEW/FAIL, a 0..100 integrity score, and explainable signals."""
    source = source or ""
    proposal = proposal or ""
    checks = []
    hard_failures = []
    score = 100

    if not proposal.strip():
        hard_failures.append("La propuesta está vacía")
    checks.append({"name": "non_empty", "passed": bool(proposal.strip())})

    missing_numbers = _numbers(source) - _numbers(proposal)
    if missing_numbers:
        missing_labels = [f"{number}{(' ' + unit) if unit else ''}" for number, unit in missing_numbers.elements()]
        hard_failures.append("Faltan cifras o unidades: " + ", ".join(sorted(missing_labels)))
    checks.append({"name": "numbers_preserved", "passed": not missing_numbers,
                   "missing": sorted(missing_labels) if missing_numbers else []})

    missing_placeholders = _placeholders(source) - _placeholders(proposal)
    if missing_placeholders:
        hard_failures.append("Faltan variables o marcadores: " + ", ".join(sorted(missing_placeholders.elements())))
    checks.append({"name": "placeholders_preserved", "passed": not missing_placeholders,
                   "missing": sorted(missing_placeholders.elements())})

    if is_html:
        source_tags, proposal_tags = _html_tags(source), _html_tags(proposal)
        tags_ok = source_tags is not None and source_tags == proposal_tags
        if not tags_ok:
            hard_failures.append("La estructura HTML no coincide con el texto de origen")
        checks.append({"name": "html_structure", "passed": tags_ok})

    source_len, proposal_len = len(re.sub(r"<[^>]*>", " ", source).strip()), len(re.sub(r"<[^>]*>", " ", proposal).strip())
    ratio = proposal_len / max(source_len, 1)
    min_ratio = float(os.getenv("QUALITY_MIN_LENGTH_RATIO", "0.35"))
    max_ratio = float(os.getenv("QUALITY_MAX_LENGTH_RATIO", "3.0"))
    ratio_ok = proposal_len > 0 and min_ratio <= ratio <= max_ratio
    if not ratio_ok:
        score -= 25
    checks.append({"name": "length_ratio", "passed": ratio_ok, "ratio": round(ratio, 3),
                   "minimum": min_ratio, "maximum": max_ratio})

    # Exact source reuse is a useful warning signal, but can be legitimate for
    # brand names, identifiers, and already-target-language content.
    unchanged = bool(source.strip()) and re.sub(r"\s+", " ", source).strip().casefold() == re.sub(r"\s+", " ", proposal).strip().casefold()
    if unchanged and re.search(r"[A-Za-zÀ-ÿ]{3}", source):
        score -= 20
    checks.append({"name": "not_unchanged", "passed": not unchanged, "warning_only": True})

    # Quotes that appear or vanish between source and proposal are a common
    # machine-translation artefact; they need a human look but do not block.
    quotes_match = has_double_quotes(source, is_html) == has_double_quotes(proposal, is_html)
    if not quotes_match:
        score -= 20
    checks.append({"name": "quotes_consistent", "passed": quotes_match})

    if hard_failures:
        score = 0
    # Failed integrity checks always block. Borderline heuristic signals require review.
    minimum = int(os.getenv("QUALITY_MIN_SCORE", "85"))
    status = "FAIL" if hard_failures else ("PASS" if score >= minimum else "REVIEW")
    return {"status": status, "score": score, "threshold": minimum,
            "hard_failures": hard_failures, "checks": checks,
            "assessment_version": "integrity-v1",
            "score_scope": "integrity_only_not_linguistic_quality"}
