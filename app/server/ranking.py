"""Pure, dependency-free ranking logic.

Everything in this module is deliberately free of network calls, Databricks SDK
imports and framework imports so it can be unit-tested in isolation. The MCP
tools in :mod:`server.tools` own all I/O; this module owns all decisions.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

MAX_QUESTIONS = 25
MAX_QUESTION_CHARS = 500
MIN_SCORE = 0.0
MAX_SCORE = 10.0

#: Score used when the model returns nothing usable for a question.
FALLBACK_SCORE = 5.0
FALLBACK_RATIONALE = "No model score returned; assigned a neutral default."

#: Upper bound on how much model output is parsed. Generous next to the
#: endpoint's ``max_tokens``, and it bounds the salvage pass's worst case.
MAX_RESPONSE_CHARS = 20_000

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_INDEX_TEXT = re.compile(r"[0-9]{1,6}")
_WRAPPER_KEYS = ("questions", "results", "rankings", "scores", "items", "data")


class InvalidQuestions(ValueError):
    """Raised when the caller-supplied question list cannot be ranked."""


@dataclass(frozen=True)
class RankedQuestion:
    """One scored question, as returned to the MCP client."""

    rank: int
    question: str
    score: float
    rationale: str

    def as_dict(self) -> dict:
        return {
            "rank": self.rank,
            "question": self.question,
            "score": self.score,
            "rationale": self.rationale,
        }


@dataclass
class ScoredItem:
    """Intermediate parse result keyed back to the input list by index."""

    index: int
    score: float
    rationale: str = ""
    matched: bool = field(default=True)


def validate_questions(questions: object) -> list[str]:
    """Normalise and validate a caller-supplied list of questions.

    Strips surrounding whitespace, drops empties, and enforces the list-size and
    per-question length caps.

    Raises:
        InvalidQuestions: if the input is not a list of strings, is empty after
            cleaning, exceeds ``MAX_QUESTIONS``, or contains an over-long entry.
    """
    if not isinstance(questions, list):
        raise InvalidQuestions("questions must be a list of strings")

    cleaned: list[str] = []
    for raw in questions:
        if not isinstance(raw, str):
            raise InvalidQuestions(f"questions must all be strings, got {type(raw).__name__}")
        text = raw.strip()
        if not text:
            continue
        if len(text) > MAX_QUESTION_CHARS:
            raise InvalidQuestions(
                f"question exceeds {MAX_QUESTION_CHARS} characters: {text[:60]!r}..."
            )
        cleaned.append(text)

    if not cleaned:
        raise InvalidQuestions("questions must contain at least one non-empty string")
    if len(cleaned) > MAX_QUESTIONS:
        raise InvalidQuestions(f"at most {MAX_QUESTIONS} questions may be ranked per call")
    return cleaned


def _finite_float(value: object) -> float | None:
    """Convert a JSON scalar to a finite float, or ``None``. Never raises.

    Python's JSON decoder accepts ``NaN``, ``Infinity`` and literals like ``1e999``
    (which overflow to ``inf``), and arbitrarily long integers that overflow
    ``float()``. All of those are treated as unusable rather than propagated.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            match = _NUMBER.search(value)
            if not match:
                return None
            number = float(match.group(0))
        else:
            return None
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def clamp_score(value: object) -> float | None:
    """Coerce ``value`` to a float clamped into ``[MIN_SCORE, MAX_SCORE]``.

    Returns ``None`` when the value is not a usable finite number, so callers
    can tell "the model said 0" apart from "the model said nothing".
    """
    number = _finite_float(value)
    if number is None:
        return None
    return max(MIN_SCORE, min(MAX_SCORE, round(number, 2)))


def clamp_index(value: object, question_count: int) -> int | None:
    """Convert a model-reported 1-based index into a 0-based list offset.

    Only a genuinely integral, positive, in-range index is accepted: an ``int``,
    an integral finite ``float`` such as ``2.0``, or a plain digit string such as
    ``"2"``. Negative, fractional, non-finite and non-numeric values return
    ``None`` instead of being truncated or partially matched. Never raises.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not _INDEX_TEXT.fullmatch(text):
            return None
        number = int(text)
    else:
        return None
    if 1 <= number <= question_count:
        return number - 1
    return None


def _extract_json_payload(text: str) -> object | None:
    """Pull JSON out of a model response. Never raises.

    Tries, in order: a ```json fenced block, the whole response, then a salvage
    pass that decodes each complete JSON object in sequence. The salvage pass
    recovers rows from a truncated array (the model hit ``max_tokens``) and from
    JSON wrapped in prose.
    """
    stripped = (text or "").strip()[:MAX_RESPONSE_CHARS]
    if not stripped:
        return None

    fenced = re.search(r"```(?:json)?\s*(.+?)```", stripped, re.DOTALL)
    candidates = [fenced.group(1).strip()] if fenced else []
    candidates.append(stripped)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (ValueError, RecursionError):
            pass

    decoder = json.JSONDecoder()
    source = candidates[0]
    objects: list[dict] = []
    pos = source.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(source, pos)
        except (ValueError, RecursionError):
            pos = source.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        pos = source.find("{", end)
    return objects or None


def _coerce_rows(payload: object) -> list[dict]:
    """Normalise the shapes a model may wrap its rows in into a flat list.

    A dict is treated as a wrapper only when it carries no ``score`` of its own,
    so a row that happens to include a list-valued field is never discarded.
    """
    if isinstance(payload, dict):
        if "score" not in payload:
            for key in _WRAPPER_KEYS:
                nested = payload.get(key)
                if isinstance(nested, list):
                    return [row for row in nested if isinstance(row, dict)]
        return [payload]
    if isinstance(payload, list):
        rows: list[dict] = []
        for item in payload:
            if isinstance(item, dict):
                rows.extend(_coerce_rows(item))
        return rows
    return []


def parse_scores(response_text: str, question_count: int) -> list[ScoredItem]:
    """Parse model output into one :class:`ScoredItem` per input question.

    A score is only ever attached to the question the model attributed it to.
    The index policy, applied to rows with a usable score:

    1. A row with a valid explicit ``index`` claims that question. The first
       claim wins; a later row repeating the same index is discarded - it is
       never moved onto a different question.
    2. A row whose ``index`` key is present but invalid (``null``, out of
       range, negative, fractional, non-finite or non-numeric) is discarded.
    3. Rows with NO ``index`` key are matched by position ONLY when no row in
       the response has an ``index`` key AND the row count equals the question
       count, i.e. the model plainly answered every question in order. Any
       other mix is ambiguous, so those rows are discarded.

    Questions left unclaimed get ``FALLBACK_SCORE`` with ``matched=False``.
    Never raises, whatever the model returned.
    """
    if question_count <= 0:
        return []

    rows = _coerce_rows(_extract_json_payload(response_text))
    # Key ABSENCE, not a None value: `{"index": null}` states an index and is invalid.
    positional = len(rows) == question_count and all("index" not in row for row in rows)

    items: dict[int, ScoredItem] = {}
    for position, row in enumerate(rows):
        score = clamp_score(row.get("score"))
        if score is None:
            continue
        index = position if positional else clamp_index(row.get("index"), question_count)
        if index is None or index in items:
            continue
        rationale = str(row.get("rationale") or row.get("reason") or "").strip()
        items[index] = ScoredItem(index=index, score=score, rationale=rationale)

    return [
        items.get(
            i,
            ScoredItem(index=i, score=FALLBACK_SCORE, rationale=FALLBACK_RATIONALE, matched=False),
        )
        for i in range(question_count)
    ]


def order_questions(questions: list[str], scored: list[ScoredItem]) -> list[RankedQuestion]:
    """Sort questions by descending score, breaking ties by input order.

    The tie-break keeps the ordering deterministic, which matters because the
    two models behind the 50/50 endpoint do not agree on absolute scores.
    """
    pairs = list(zip(questions, scored, strict=False))
    ordered = sorted(pairs, key=lambda pair: (-pair[1].score, pair[1].index))
    return [
        RankedQuestion(
            rank=position,
            question=question,
            score=item.score,
            rationale=item.rationale or FALLBACK_RATIONALE,
        )
        for position, (question, item) in enumerate(ordered, start=1)
    ]


def build_prompt(questions: list[str], context: str | None = None) -> str:
    """Build the scoring prompt sent to the 50/50 serving endpoint.

    Kept deliberately terse and schema-first: the two small instruct models
    behind the endpoint follow short, explicit instructions far more reliably
    than long ones.
    """
    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, start=1))
    context_line = (
        f"\nContext the questions should be judged against:\n{context.strip()}\n"
        if context and context.strip()
        else ""
    )
    return (
        "You score the quality of questions. For each numbered question, give a "
        f"score from {MIN_SCORE:g} (vague, unanswerable, or off-topic) to "
        f"{MAX_SCORE:g} (specific, answerable, and highly relevant), plus a "
        "rationale of at most 15 words.\n"
        f"{context_line}"
        f"\nQuestions:\n{numbered}\n\n"
        "Reply with JSON only, no prose, no code fence:\n"
        '[{"index": 1, "score": 7.5, "rationale": "..."}]\n'
        f"Return exactly {len(questions)} objects, one per question, in order."
    )


def rank_from_response(
    questions: list[str], response_text: str
) -> tuple[list[RankedQuestion], int]:
    """Turn raw model text into ranked questions plus a parsed-row count.

    The count lets the tool tell the caller how much of the ranking came from
    the model versus the neutral fallback.
    """
    scored = parse_scores(response_text, len(questions))
    return order_questions(questions, scored), sum(1 for item in scored if item.matched)
