"""Unit tests for the pure ranking logic.

No network, no Databricks auth, no framework imports: everything here exercises
`server.ranking` directly.
"""

from __future__ import annotations

import pytest
from server.ranking import (
    FALLBACK_RATIONALE,
    FALLBACK_SCORE,
    MAX_QUESTION_CHARS,
    MAX_QUESTIONS,
    InvalidQuestions,
    build_prompt,
    clamp_index,
    clamp_score,
    order_questions,
    parse_scores,
    rank_from_response,
    validate_questions,
)

# --------------------------------------------------------------------------- #
# validate_questions
# --------------------------------------------------------------------------- #


def test_validate_strips_whitespace_and_drops_empties():
    assert validate_questions(["  a? ", "", "   ", "b?"]) == ["a?", "b?"]


def test_validate_rejects_non_list():
    with pytest.raises(InvalidQuestions, match="must be a list"):
        validate_questions("not a list")


def test_validate_rejects_non_string_entries():
    with pytest.raises(InvalidQuestions, match="must all be strings"):
        validate_questions(["ok?", 42])


def test_validate_rejects_all_empty():
    with pytest.raises(InvalidQuestions, match="at least one non-empty"):
        validate_questions(["", "   "])


def test_validate_rejects_empty_list():
    with pytest.raises(InvalidQuestions, match="at least one non-empty"):
        validate_questions([])


def test_validate_enforces_question_cap():
    assert len(validate_questions([f"q{i}?" for i in range(MAX_QUESTIONS)])) == MAX_QUESTIONS
    with pytest.raises(InvalidQuestions, match="at most"):
        validate_questions([f"q{i}?" for i in range(MAX_QUESTIONS + 1)])


def test_validate_enforces_length_cap():
    validate_questions(["x" * MAX_QUESTION_CHARS])
    with pytest.raises(InvalidQuestions, match="exceeds"):
        validate_questions(["x" * (MAX_QUESTION_CHARS + 1)])


# --------------------------------------------------------------------------- #
# clamp_score
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (7, 7.0),
        (7.49, 7.49),
        ("8.5", 8.5),
        ("score: 3", 3.0),
        ("9/10", 9.0),
        (-4, 0.0),  # clamped to floor
        (99, 10.0),  # clamped to ceiling
        ("-2.5", 0.0),
        (0, 0.0),
        (7.456, 7.46),  # rounded to 2dp
    ],
)
def test_clamp_score_coerces_and_clamps(value, expected):
    assert clamp_score(value) == expected


@pytest.mark.parametrize("value", [None, True, False, "", "no digits here", [], {}, object()])
def test_clamp_score_returns_none_for_unusable(value):
    assert clamp_score(value) is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_clamp_score_rejects_non_finite(value):
    assert clamp_score(value) is None


# --------------------------------------------------------------------------- #
# clamp_index
# --------------------------------------------------------------------------- #


def test_clamp_index_converts_one_based_to_zero_based():
    assert clamp_index(1, 3) == 0
    assert clamp_index(3, 3) == 2
    assert clamp_index("2", 3) == 1


@pytest.mark.parametrize("value", [0, 4, -1, None, True, "abc", []])
def test_clamp_index_rejects_out_of_range_or_unusable(value):
    assert clamp_index(value, 3) is None


# --------------------------------------------------------------------------- #
# parse_scores
# --------------------------------------------------------------------------- #


def test_parse_bare_json_array():
    text = '[{"index": 1, "score": 9, "rationale": "sharp"}, {"index": 2, "score": 2}]'
    scored = parse_scores(text, 2)
    assert [(s.score, s.matched) for s in scored] == [(9.0, True), (2.0, True)]
    assert scored[0].rationale == "sharp"


def test_parse_fenced_json():
    text = 'Sure!\n```json\n[{"index": 1, "score": 6.5, "rationale": "ok"}]\n```\nHope that helps.'
    scored = parse_scores(text, 1)
    assert scored[0].score == 6.5
    assert scored[0].rationale == "ok"


def test_parse_json_with_surrounding_prose():
    text = 'Here are my scores: [{"index": 1, "score": 4}] -- let me know.'
    assert parse_scores(text, 1)[0].score == 4.0


def test_parse_wrapped_object_shapes():
    for key in ("questions", "results", "rankings", "scores", "items", "data"):
        text = '{"' + key + '": [{"index": 1, "score": 8}]}'
        assert parse_scores(text, 1)[0].score == 8.0, key


def test_parse_single_object_not_in_list():
    assert parse_scores('{"index": 1, "score": 7, "rationale": "fine"}', 1)[0].score == 7.0


def test_parse_accepts_reason_as_rationale_alias():
    assert parse_scores('[{"index": 1, "score": 5, "reason": "meh"}]', 1)[0].rationale == "meh"


def test_parse_keys_by_index_not_position():
    """A model that answers out of order must still score the right question."""
    text = '[{"index": 2, "score": 9}, {"index": 1, "score": 1}]'
    scored = parse_scores(text, 2)
    assert [s.score for s in scored] == [1.0, 9.0]


def test_parse_falls_back_to_position_when_index_missing():
    text = '[{"score": 3}, {"score": 8}]'
    assert [s.score for s in parse_scores(text, 2)] == [3.0, 8.0]


def test_parse_spills_duplicate_index_into_free_slot():
    text = '[{"index": 1, "score": 9}, {"index": 1, "score": 4}]'
    scored = parse_scores(text, 2)
    assert scored[0].score == 9.0
    assert scored[1].score == 4.0
    assert all(s.matched for s in scored)


def test_parse_marks_unscored_questions_as_fallback():
    scored = parse_scores('[{"index": 1, "score": 9}]', 3)
    assert scored[0].matched is True
    assert [s.score for s in scored[1:]] == [FALLBACK_SCORE, FALLBACK_SCORE]
    assert all(s.matched is False for s in scored[1:])
    assert scored[1].rationale == FALLBACK_RATIONALE


def test_parse_skips_rows_without_a_usable_score():
    scored = parse_scores('[{"index": 1, "rationale": "no score"}, {"index": 2, "score": 6}]', 2)
    assert scored[0].matched is False
    assert scored[1].score == 6.0


@pytest.mark.parametrize("text", ["", "   ", "I cannot help with that.", "{{{not json", "null"])
def test_parse_garbage_yields_all_fallbacks(text):
    scored = parse_scores(text, 2)
    assert all(s.score == FALLBACK_SCORE and s.matched is False for s in scored)


def test_parse_ignores_extra_rows_beyond_question_count():
    scored = parse_scores('[{"score": 1}, {"score": 2}, {"score": 3}]', 2)
    assert [s.score for s in scored] == [1.0, 2.0]


def test_parse_zero_questions_returns_empty():
    assert parse_scores('[{"score": 5}]', 0) == []


# --------------------------------------------------------------------------- #
# order_questions
# --------------------------------------------------------------------------- #


def test_order_sorts_descending_and_assigns_ranks():
    questions = ["low?", "high?", "mid?"]
    scored = parse_scores(
        '[{"index": 1, "score": 2}, {"index": 2, "score": 9}, {"index": 3, "score": 5}]', 3
    )
    ranked = order_questions(questions, scored)
    assert [r.question for r in ranked] == ["high?", "mid?", "low?"]
    assert [r.rank for r in ranked] == [1, 2, 3]
    assert [r.score for r in ranked] == [9.0, 5.0, 2.0]


def test_order_breaks_ties_by_input_order():
    questions = ["first?", "second?", "third?"]
    scored = parse_scores(
        '[{"index": 1, "score": 5}, {"index": 2, "score": 5}, {"index": 3, "score": 5}]', 3
    )
    assert [r.question for r in order_questions(questions, scored)] == questions


def test_order_substitutes_fallback_rationale_when_blank():
    questions = ["q?"]
    scored = parse_scores('[{"index": 1, "score": 5, "rationale": "  "}]', 1)
    assert order_questions(questions, scored)[0].rationale == FALLBACK_RATIONALE


def test_ranked_question_as_dict_shape():
    questions = ["q?"]
    scored = parse_scores('[{"index": 1, "score": 5, "rationale": "fine"}]', 1)
    assert order_questions(questions, scored)[0].as_dict() == {
        "rank": 1,
        "question": "q?",
        "score": 5.0,
        "rationale": "fine",
    }


# --------------------------------------------------------------------------- #
# build_prompt
# --------------------------------------------------------------------------- #


def test_prompt_numbers_questions_one_based():
    prompt = build_prompt(["alpha?", "beta?"])
    assert "1. alpha?" in prompt
    assert "2. beta?" in prompt
    assert "Return exactly 2 objects" in prompt


def test_prompt_includes_context_when_given():
    assert "quarterly earnings" in build_prompt(["q?"], context="  quarterly earnings  ")


@pytest.mark.parametrize("context", [None, "", "   "])
def test_prompt_omits_context_section_when_blank(context):
    assert "Context the questions" not in build_prompt(["q?"], context=context)


# --------------------------------------------------------------------------- #
# rank_from_response
# --------------------------------------------------------------------------- #


def test_rank_from_response_end_to_end():
    questions = ["vague?", "specific?"]
    text = '[{"index": 1, "score": 2, "rationale": "too broad"}, {"index": 2, "score": 9}]'
    ranked, model_scored = rank_from_response(questions, text)
    assert model_scored == 2
    assert [r.question for r in ranked] == ["specific?", "vague?"]
    assert ranked[1].rationale == "too broad"


def test_rank_from_response_counts_only_model_scored():
    ranked, model_scored = rank_from_response(["a?", "b?"], '[{"index": 1, "score": 7}]')
    assert model_scored == 1
    assert len(ranked) == 2


def test_rank_from_response_survives_unusable_output():
    ranked, model_scored = rank_from_response(["a?", "b?"], "sorry, I can't do that")
    assert model_scored == 0
    assert [r.score for r in ranked] == [FALLBACK_SCORE, FALLBACK_SCORE]
    assert [r.rank for r in ranked] == [1, 2]
