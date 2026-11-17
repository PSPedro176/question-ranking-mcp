"""Pure, dependency-free ranking logic.

Everything in this module is deliberately free of network calls, Databricks SDK
imports and framework imports so it can be unit-tested in isolation. The MCP
tools in :mod:`server.tools` own all I/O; this module owns all decisions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

MAX_QUESTIONS = 25
MAX_QUESTION_CHARS = 500
MIN_SCORE = 0.0
MAX_SCORE = 10.0

#: Score used when the model returns nothing usable for a question.
FALLBACK_SCORE = 5.0
FALLBACK_RATIONALE = "No model score returned; assigned a neutral default."


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


def clamp_score(value: object) -> float | None:
    """Coerce ``value`` to a float clamped into ``[MIN_SCORE, MAX_SCORE]``.

    Returns ``None`` when the value is not numeric at all, so callers can tell
    "the model said 0" apart from "the model said nothing".
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if not match:
            return None
        number = float(match.group(0))
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return max(MIN_SCORE, min(MAX_SCORE, round(number, 2)))


def _extract_json_payload(text: str) -> object | None:
    """Pull the first JSON object or array out of a model response.

    Handles bare JSON, ```json fenced blocks, and JSON with leading or trailing
    prose, which are the three shapes small instruct models actually emit.
    """
    stripped = text.strip()
    if not stripped:
        return None

    fenced = re.search(r"```(?:json)?\s*(.+?)```", stripped, re.DOTALL)
    candidates = [fenced.group(1).strip()] if fenced else []
    candidates.append(stripped)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass
        for opener, closer in (("[", "]"), ("{", "}")):
            start = candidate.find(opener)
            end = candidate.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(candidate[start : end + 1])
                except (json.JSONDecodeError, ValueError):
                    continue
    return None


def _coerce_rows(payload: object) -> list[dict]:
    """Normalise the many shapes a model may wrap its rows in into a list."""
    if isinstance(payload, dict):
        for key in ("questions", "results", "rankings", "scores", "items", "data"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [row for row in nested if isinstance(row, dict)]
        return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def parse_scores(response_text: str, question_count: int) -> list[ScoredItem]:
    """Parse model output into one :class:`ScoredItem` per input question.

    Rows are keyed back to the input list by the model's 1-based ``index``
    field; rows whose index is missing, duplicated or out of range fall back to
    positional order. Questions the model never scored get ``matched=False``.
    """
    if question_count <= 0:
        return []

    rows = _coerce_rows(_extract_json_payload(response_text or ""))
    items: dict[int, ScoredItem] = {}
    unplaced: list[tuple[float, str]] = []

    for position, row in enumerate(rows):
        score = clamp_score(row.get("score"))
        if score is None:
            continue
        rationale = str(row.get("rationale") or row.get("reason") or "").strip()

        index = clamp_index(row.get("index"), question_count)
        if index is None:
            index = position if position < question_count else None
        if index is None or index in items:
            unplaced.append((score, rationale))
            continue
        items[index] = ScoredItem(index=index, score=score, rationale=rationale)

    # Spill rows that could not be keyed into the first still-empty slots.
    for score, rationale in unplaced:
        free = next((i for i in range(question_count) if i not in items), None)
        if free is None:
            break
        items[free] = ScoredItem(index=free, score=score, rationale=rationale)

    return [
        items.get(
            i,
            ScoredItem(index=i, score=FALLBACK_SCORE, rationale=FALLBACK_RATIONALE, matched=False),
        )
        for i in range(question_count)
    ]


def clamp_index(value: object, question_count: int) -> int | None:
    """Convert a model-reported 1-based index into a 0-based list offset.

    Returns ``None`` if the value is absent or outside ``1..question_count``.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str):
        match = re.search(r"\d+", value)
        if not match:
            return None
        number = int(match.group(0))
    else:
        return None
    zero_based = number - 1
    if 0 <= zero_based < question_count:
        return zero_based
    return None


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
