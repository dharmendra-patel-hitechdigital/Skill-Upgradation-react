"""Rule-based document analyser - the zero-credential fallback.

Why this exists
---------------
An AI feature that only works when someone has funded an OpenAI account is not a
working feature. This analyser implements the *same* :class:`DocumentAnalyzer`
protocol using nothing but the standard library, so:

* the service is demonstrable immediately after ``git clone``;
* CI tests the real pipeline end-to-end with no network, no mocks, no spend;
* an OpenAI outage or exhausted quota degrades output quality instead of taking
  the endpoint down (see ``LLM_PROVIDER=auto``).

What it actually does
---------------------
Classic pre-LLM IE techniques, which are genuinely effective on the structured
business documents this service targets:

* **Classification** - weighted keyword scoring against per-type signal sets.
* **Fields** - ``Label: value`` line parsing plus targeted patterns for the
  fields that matter on invoices and receipts (totals, tax, due dates, numbers).
* **Entities** - regex for the closed, well-formed classes (money, email, phone,
  dates, URLs) and suffix matching for organisations.
* **Summary** - extractive: sentences ranked by keyword density and position,
  then re-emitted in document order so the result reads coherently.
* **Q&A** - lexical retrieval. Scores sentences by overlap with the question's
  content words and returns the best-supported passage, honestly reporting
  ``answer_found=False`` when nothing clears the threshold.

It will not paraphrase or reason. It is a floor on quality, not a replacement.
"""
from __future__ import annotations

import re
import time
from collections import Counter

from app.schemas.document import (
    DocumentAnalysis,
    DocumentKind,
    EntityType,
    ExtractedEntity,
    ExtractedField,
)
from app.services.ai.base import AnalysisResult, AnswerResult

# --------------------------------------------------------------------- patterns
_PAGE_MARKER = re.compile(r"^\[page \d+\]$", re.MULTILINE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{1,}")

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
_URL = re.compile(r"\bhttps?://[^\s<>\"')]+", re.IGNORECASE)
# Phone: optional country code, then 7-14 digits with common separators.
_PHONE = re.compile(
    r"(?<!\w)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?"
    r"\d{3,4}[\s.-]?\d{3,4}(?:[\s.-]?\d{1,4})?(?!\w)"
)
_MONEY = re.compile(
    r"(?:(?:[$€£¥₹]|\b(?:USD|EUR|GBP|INR|JPY|AUD|CAD)\b)\s?\d[\d,]*(?:\.\d{1,2})?"
    r"|\d[\d,]*(?:\.\d{2})\s?(?:USD|EUR|GBP|INR|JPY|AUD|CAD|dollars|euros|rupees))",
    re.IGNORECASE,
)
# Real invoices state the currency once in the header and then print bare numbers
# ("Total Amount: 1488.00"), so a symbol-anchored pattern alone misses every
# figure that matters. These two work together: a line whose label is monetary,
# and a two-decimal number on it.
_MONEY_CONTEXT = re.compile(
    r"\b(total|subtotal|sub-total|amount|balance|due|price|cost|tax|vat|gst|"
    r"discount|paid|payment|charge|fee|salary|invoice|refund|deposit|withdrawal)\b",
    re.IGNORECASE,
)
_DECIMAL_AMOUNT = re.compile(r"(?<![\d.])\d[\d,]*\.\d{2}(?![\d.])")
_DATE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})\b",
    re.IGNORECASE,
)
_ORG = re.compile(
    r"\b(?:[A-Z][\w&.'-]*\s+){0,4}[A-Z][\w&.'-]*\s+"
    r"(?:Inc|Inc\.|LLC|L\.L\.C\.|Ltd|Ltd\.|Limited|LLP|PLC|GmbH|AG|S\.A\.|B\.V\.|Pty|"
    r"Corporation|Corp|Corp\.|Company|Co\.|Holdings|Group|Technologies|Solutions)\b"
)
# A "Label: value" line - the backbone of form and invoice extraction.
_LABEL_VALUE = re.compile(
    r"^[ \t]*([A-Za-z][A-Za-z0-9 /&()#.'-]{1,48}?)[ \t]*:[ \t]*(.{1,300}?)[ \t]*$",
    re.MULTILINE,
)
_IDENTIFIER = re.compile(r"\b(?=[A-Z0-9-]{6,24}\b)(?=[^\s]*\d)[A-Z0-9][A-Z0-9-]{4,23}\b")

# Shared building blocks for the labelled-amount patterns below. Factored out
# because all three repeat them verbatim, and an amount format fix should not
# have to be made in three places.
_AMOUNT_CAPTURE = r"([$€£¥₹]?\s?\d[\d,]*(?:\.\d{2})?)"
# An optional colon or dash plus horizontal whitespace - but never a newline, so
# a label cannot capture a number from the following line.
_LABEL_GAP = r"[^\S\n]*[:\-]?[^\S\n]*"

# Signals that a label line is worth promoting to a structured field.
_FIELD_LABEL_HINTS = (
    "invoice", "receipt", "order", "po", "purchase", "account", "customer",
    "client", "vendor", "supplier", "date", "due", "total", "subtotal", "amount",
    "balance", "tax", "vat", "gst", "discount", "currency", "payment", "terms",
    "reference", "number", "no", "id", "name", "email", "phone", "address",
    "quantity", "price", "description", "issued", "period", "status", "title",
    "policy", "contract", "effective", "expiry", "expires", "salary", "position",
)

# Per-type signals. Tuned so a single strong term (e.g. "invoice number") is
# enough, while generic words need corroboration.
_TYPE_SIGNALS: dict[DocumentKind, dict[str, int]] = {
    DocumentKind.INVOICE: {
        "invoice": 5, "invoice number": 6, "invoice no": 6, "bill to": 4,
        "amount due": 5, "due date": 3, "subtotal": 3, "purchase order": 3,
        "remit": 2, "payment terms": 3, "net 30": 3, "tax invoice": 6,
    },
    DocumentKind.RECEIPT: {
        "receipt": 6, "thank you for your purchase": 5, "cashier": 4,
        "change due": 5, "card ending": 3, "transaction id": 3, "subtotal": 2,
        "merchant": 3, "tendered": 4, "store": 1,
    },
    DocumentKind.CONTRACT: {
        "agreement": 5, "this agreement": 6, "hereby": 4, "whereas": 5,
        "party": 2, "parties": 3, "shall": 2, "terms and conditions": 4,
        "governing law": 5, "termination": 3, "confidentiality": 3,
        "in witness whereof": 6, "indemnif": 4, "effective date": 3,
    },
    DocumentKind.RESUME: {
        "curriculum vitae": 6, "resume": 5, "work experience": 5,
        "professional experience": 5, "education": 3, "skills": 3,
        "certifications": 3, "employment history": 5, "references": 2,
        "objective": 2, "linkedin.com/in": 4,
    },
    DocumentKind.REPORT: {
        "report": 4, "executive summary": 6, "introduction": 2,
        "methodology": 4, "findings": 4, "conclusion": 3,
        "recommendations": 4, "appendix": 3, "figure": 1, "table of contents": 3,
    },
    DocumentKind.LETTER: {
        "dear": 5, "sincerely": 5, "yours faithfully": 6, "yours truly": 5,
        "best regards": 4, "kind regards": 4, "re:": 2, "enclosed": 2,
    },
    DocumentKind.FORM: {
        "please complete": 5, "application form": 6, "check one": 4,
        "signature": 2, "date of birth": 4, "please print": 4,
        "for office use only": 6, "tick": 2, "fill in": 3,
    },
    DocumentKind.IDENTITY: {
        "passport": 6, "driver's license": 6, "driving licence": 6,
        "date of birth": 3, "nationality": 4, "identity card": 6,
        "place of birth": 4, "issuing authority": 5, "expiry date": 2,
    },
    DocumentKind.STATEMENT: {
        "statement": 4, "account statement": 6, "opening balance": 6,
        "closing balance": 6, "transaction": 2, "deposits": 3,
        "withdrawals": 4, "statement period": 6, "available balance": 5, "iban": 4,
    },
}

_STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are aren as at be because been
    before being below between both but by can cannot could couldn did didn do does
    doesn doing don down during each few for from further had hadn has hasn have haven
    having he her here hers herself him himself his how i if in into is isn it its
    itself just let me more most must my myself no nor not of off on once only or
    other ought our ours ourselves out over own same shan she should shouldn so some
    such than that the their theirs them themselves then there these they this those
    through to too under until up very was wasn we were weren what when where which
    while who whom why will with won would wouldn you your yours yourself yourselves
    shall may also per
    """.split()  # noqa: SIM905 - the block form stays readable and diff-friendly
)
# Note: business terms such as "total", "amount", "date", "number" and "invoice"
# are deliberately NOT stopwords. They look like noise in general prose, but in
# the documents this service targets they are the highest-signal words there are -
# filtering them out cripples both keyword ranking and question retrieval.

# Function words used to fingerprint a language. Small, but reliable on prose.
_LANGUAGE_MARKERS: dict[str, frozenset[str]] = {
    "en": frozenset({"the", "and", "of", "to", "in", "is", "that", "for", "with", "this"}),
    "es": frozenset({"el", "la", "de", "que", "y", "en", "los", "del", "para", "con"}),
    "fr": frozenset({"le", "la", "les", "de", "et", "des", "est", "pour", "dans", "une"}),
    "de": frozenset({"der", "die", "das", "und", "von", "mit", "ist", "den", "für", "nicht"}),
    "pt": frozenset({"o", "de", "que", "e", "do", "da", "em", "para", "com", "uma"}),
    "it": frozenset({"il", "di", "che", "e", "la", "per", "con", "del", "una", "sono"}),
    "nl": frozenset({"de", "het", "een", "van", "en", "is", "met", "voor", "niet", "dat"}),
}

_MAX_SUMMARY_SENTENCES = 4
_MIN_ANSWER_OVERLAP = 0.34


class HeuristicAnalyzer:
    """Deterministic, offline document analyser."""

    name = "heuristic"
    model = "rules-v1"

    async def analyze(
        self, text: str, *, filename: str, content_type: str
    ) -> AnalysisResult:
        started = time.perf_counter()
        body = _strip_page_markers(text)

        keywords = _top_keywords(body, limit=12)
        kind, kind_score = _classify(body, filename)
        entities = _find_entities(body)
        fields = _find_fields(body)
        summary = _summarise(body, keywords)
        language = _detect_language(body)

        warnings: list[str] = [
            "Analysed by the built-in rule-based engine. Configure "
            "ANTHROPIC_API_KEY or OPENAI_API_KEY for higher-quality "
            "classification, summaries and field extraction."
        ]
        if len(body.strip()) < 200:
            warnings.append("The document contained very little text.")

        analysis = DocumentAnalysis(
            document_type=kind,
            language=language,
            summary=summary,
            keywords=keywords,
            entities=entities,
            fields=fields,
            confidence=_overall_confidence(body, kind_score, fields, entities),
            warnings=warnings,
        )
        return AnalysisResult(
            analysis=analysis,
            provider=self.name,
            model=self.model,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    async def answer_question(
        self, text: str, question: str, *, filename: str
    ) -> AnswerResult:
        body = _strip_page_markers(text)
        sentences = _sentences(body)
        query_terms = _content_words(question)

        if not query_terms or not sentences:
            return AnswerResult(
                answer=(
                    "The document does not contain enough text to answer that. "
                    "Configure an OpenAI API key for full natural-language answers."
                ),
                answer_found=False,
                provider=self.name,
                model=self.model,
            )

        scored: list[tuple[float, int, str]] = []
        for index, sentence in enumerate(sentences):
            terms = _content_words(sentence)
            if not terms:
                continue
            overlap = len(query_terms & terms) / len(query_terms)
            if overlap > 0:
                # Slight bonus for shorter sentences: a 300-word paragraph that
                # happens to contain every query word is rarely the best answer.
                density = overlap * (1.0 + min(1.0, 20 / max(len(terms), 1)) * 0.15)
                scored.append((density, index, sentence))

        if not scored:
            return AnswerResult(
                answer=(
                    "I could not find anything about that in this document. "
                    "The text does not appear to mention those terms."
                ),
                answer_found=False,
                provider=self.name,
                model=self.model,
            )

        scored.sort(key=lambda row: (-row[0], row[1]))
        best_score = scored[0][0]
        top = sorted(scored[:3], key=lambda row: row[1])
        quotes = [sentence.strip() for _, _, sentence in top]

        if best_score < _MIN_ANSWER_OVERLAP:
            return AnswerResult(
                answer=(
                    "The document does not clearly answer that, but these passages "
                    "are the closest match."
                ),
                answer_found=False,
                quotes=quotes,
                provider=self.name,
                model=self.model,
            )

        return AnswerResult(
            answer=" ".join(quotes),
            answer_found=True,
            quotes=quotes,
            provider=self.name,
            model=self.model,
        )


# ------------------------------------------------------------------- internals
def _strip_page_markers(text: str) -> str:
    return _PAGE_MARKER.sub("", text)


def _sentences(text: str) -> list[str]:
    """Split into sentences, treating standalone lines as sentences too.

    Business documents are full of table rows and headings with no terminal
    punctuation; splitting on punctuation alone would return one giant blob.
    """
    out: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        out.extend(part.strip() for part in _SENTENCE_SPLIT.split(stripped) if part.strip())
    return out


def _content_words(text: str) -> set[str]:
    return {
        word.lower()
        for word in _WORD.findall(text)
        if len(word) > 2 and word.lower() not in _STOPWORDS
    }


def _top_keywords(text: str, *, limit: int) -> list[str]:
    counts = Counter(
        word.lower()
        for word in _WORD.findall(text)
        if len(word) > 3 and word.lower() not in _STOPWORDS
    )
    return [word for word, _ in counts.most_common(limit)]


def _classify(text: str, filename: str) -> tuple[DocumentKind, int]:
    """Score every document type and return the winner plus its raw score."""
    haystack = f"{text}\n{filename}".lower()
    scores: dict[DocumentKind, int] = {}
    for kind, signals in _TYPE_SIGNALS.items():
        score = sum(weight for term, weight in signals.items() if term in haystack)
        if score:
            scores[kind] = score

    if not scores:
        return DocumentKind.OTHER, 0
    best = max(scores.items(), key=lambda item: item[1])
    # A single weak keyword match is not a classification.
    return (best[0], best[1]) if best[1] >= 4 else (DocumentKind.OTHER, best[1])


def _find_entities(text: str) -> list[ExtractedEntity]:
    """Collect entities, de-duplicated, most-specific classes first."""
    found: list[ExtractedEntity] = []
    seen: set[tuple[str, str]] = set()

    def add(value: str, kind: EntityType, confidence: float) -> None:
        cleaned = value.strip().strip(".,;:()[]").strip()
        if len(cleaned) < 2 or len(cleaned) > 512:
            return
        key = (cleaned.lower(), kind.value)
        if key in seen:
            return
        seen.add(key)
        found.append(ExtractedEntity(text=cleaned, type=kind, confidence=confidence))

    # Ordered by precision: unambiguous formats first, fuzzy ones last.
    for match in _EMAIL.findall(text):
        add(match, EntityType.EMAIL, 0.97)
    for match in _URL.findall(text):
        add(match, EntityType.OTHER, 0.95)
    for match in _MONEY.findall(text):
        add(match, EntityType.MONEY, 0.92)
    # Bare two-decimal figures, but only on lines that are clearly about money -
    # this is what catches "Total Amount: 1488.00" without also flagging every
    # version number and quantity in the document.
    for line in text.split("\n"):
        if _MONEY_CONTEXT.search(line):
            for match in _DECIMAL_AMOUNT.findall(line):
                add(match, EntityType.MONEY, 0.8)
    for match in _DATE.findall(text):
        add(match, EntityType.DATE, 0.88)
    for match in _ORG.findall(text):
        add(match, EntityType.ORGANIZATION, 0.7)

    # Phone numbers overlap heavily with dates, amounts and IDs; only accept a
    # match that is not already claimed by a higher-precision class.
    claimed = {entity.text.lower() for entity in found}
    for match in _PHONE.findall(text):
        candidate = match.strip()
        digits = sum(character.isdigit() for character in candidate)
        if (
            7 <= digits <= 15
            and candidate.lower() not in claimed
            # Also reject a match that is merely a substring of something already
            # claimed - e.g. the digits inside an amount or an account number.
            and not any(candidate in owned for owned in claimed)
        ):
            add(candidate, EntityType.PHONE, 0.6)

    for match in _IDENTIFIER.findall(text):
        add(match, EntityType.IDENTIFIER, 0.55)

    return found[:100]


def _find_fields(text: str) -> list[ExtractedField]:
    """Pull ``Label: value`` pairs, keeping the ones that look business-relevant."""
    fields: list[ExtractedField] = []
    seen: set[str] = set()

    for label, value in _LABEL_VALUE.findall(text):
        label_clean = label.strip()
        value_clean = value.strip()
        if not value_clean or len(label_clean) < 2:
            continue
        # A "label" of five words is almost always a sentence containing a colon.
        if len(label_clean.split()) > 4:
            continue

        lowered = label_clean.lower()
        if not any(hint in lowered for hint in _FIELD_LABEL_HINTS):
            continue

        key = _snake_case(label_clean)
        if key in seen:
            continue
        seen.add(key)
        fields.append(
            ExtractedField(key=key, value=value_clean[:2048], confidence=0.75)
        )

    # Totals are frequently laid out as "TOTAL    1,240.50" with no colon, so
    # they need a dedicated pass or the single most important field is missed.
    for key, label in (
        # The leading \b on "total" is essential: without it the pattern matches
        # inside "Subtotal" and the grand total silently becomes the subtotal.
        ("total_amount", r"\b(?:grand\s+)?total(?:\s+due|\s+amount)?\b"),
        ("subtotal", r"sub[\s-]?total\b"),
        # The optional (...) group skips a parenthetical like "Tax (VAT 20%)".
        ("tax_amount", r"\b(?:tax|vat|gst)\b[^\S\n]*(?:\([^)]*\))?"),
    ):
        pattern = label + _LABEL_GAP + _AMOUNT_CAPTURE
        if key in seen:
            continue
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            seen.add(key)
            fields.append(
                ExtractedField(key=key, value=match.group(1).strip(), confidence=0.7)
            )

    return fields[:100]


def _snake_case(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", label.strip().lower()).strip("_")
    return (cleaned or "field")[:128]


def _summarise(text: str, keywords: list[str]) -> str:
    """Extractive summary: rank sentences, then re-emit in document order."""
    sentences = [s for s in _sentences(text) if len(s.split()) >= 5]
    if not sentences:
        stripped = text.strip()
        return (
            stripped[:400] + ("..." if len(stripped) > 400 else "")
            if stripped
            else "The document contained no readable prose."
        )

    keyword_set = set(keywords)
    scored: list[tuple[float, int, str]] = []
    for index, sentence in enumerate(sentences[:200]):
        words = _content_words(sentence)
        if not words:
            continue
        density = len(words & keyword_set) / len(words)
        # Opening sentences carry disproportionate information in business
        # documents (titles, parties, invoice headers).
        position_bonus = 0.35 if index < 3 else (0.15 if index < 8 else 0.0)
        scored.append((density + position_bonus, index, sentence))

    if not scored:
        return sentences[0][:400]

    scored.sort(key=lambda row: (-row[0], row[1]))
    chosen = sorted(scored[:_MAX_SUMMARY_SENTENCES], key=lambda row: row[1])

    summary = " ".join(_ensure_terminated(sentence) for _, _, sentence in chosen)
    return summary[:4000]


def _ensure_terminated(sentence: str) -> str:
    cleaned = sentence.strip()
    return cleaned if cleaned.endswith((".", "!", "?", ":")) else f"{cleaned}."


def _detect_language(text: str) -> str | None:
    """Guess the language from function-word overlap. Returns None if unsure."""
    tokens = [word.lower() for word in _WORD.findall(text[:20_000])]
    if len(tokens) < 25:
        return None

    counts = Counter(tokens)
    scores = {
        code: sum(counts[marker] for marker in markers)
        for code, markers in _LANGUAGE_MARKERS.items()
    }
    best_code, best_score = max(scores.items(), key=lambda item: item[1])
    # Require a real signal: OCR noise and number-heavy tables score near zero.
    return best_code if best_score >= max(3, len(tokens) * 0.01) else None


def _overall_confidence(
    text: str,
    kind_score: int,
    fields: list[ExtractedField],
    entities: list[ExtractedEntity],
) -> float:
    """Blend the available signals into a single self-assessment.

    Deliberately capped below 0.8: a rule-based engine should never claim the
    confidence of a real model, and downstream consumers use this to decide
    whether a human needs to review the document.
    """
    text_signal = min(1.0, len(text.strip()) / 2000)
    type_signal = min(1.0, kind_score / 12)
    field_signal = min(1.0, len(fields) / 8)
    entity_signal = min(1.0, len(entities) / 10)

    blended = (
        0.30 * text_signal
        + 0.30 * type_signal
        + 0.25 * field_signal
        + 0.15 * entity_signal
    )
    return round(min(0.78, max(0.05, blended)), 2)
