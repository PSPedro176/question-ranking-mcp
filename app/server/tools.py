"""MCP tool surface for question ranking.

Three tools, deliberately: one that ranks, one that scores a single question
against a candidate set, and one that reports identity and wiring. All decision
logic lives in :mod:`server.ranking`; these functions only do I/O and shaping.
"""

from __future__ import annotations

from server import llm, utils
from server.ranking import (
    InvalidQuestions,
    build_prompt,
    rank_from_response,
    validate_questions,
)


def load_tools(mcp_server) -> None:
    """Register every question-ranking tool on the given FastMCP server."""

    @mcp_server.tool
    def rank_questions(questions: list[str], context: str | None = None) -> dict:
        """Rank questions from best to worst by quality and answerability.

        Scores each question from 0 (vague, unanswerable, or off-topic) to 10
        (specific, answerable, highly relevant) using the Databricks serving
        endpoint that splits traffic 50/50 across two open-source models, then
        returns them sorted best-first.

        Args:
            questions: The questions to rank. 1-25 entries, each at most 500
                characters.
            context: Optional description of the topic, document or audience the
                questions should be judged against. Scores are relevance-aware
                when supplied and quality-only when omitted.

        Returns:
            dict with:
                - ranked (list): One entry per question, best first, each with
                  ``rank`` (1-based), ``question``, ``score`` (0-10) and
                  ``rationale``.
                - endpoint (str): The serving endpoint that produced the scores.
                - model_scored (int): How many questions the model scored
                  directly; any remainder received a neutral 5.0 fallback.
                - question_count (int): Number of questions ranked.

        Errors are returned as ``{"error": ..., "message": ...}`` rather than
        raised, so an MCP client gets a usable response instead of a transport
        failure.
        """
        try:
            cleaned = validate_questions(questions)
        except InvalidQuestions as exc:
            return {"error": "invalid_questions", "message": str(exc)}

        try:
            response_text = llm.complete(build_prompt(cleaned, context))
        except Exception as exc:  # noqa: BLE001 - surfaced to the MCP client
            return {
                "error": "endpoint_query_failed",
                "message": f"Could not query the ranking endpoint: {exc}",
            }

        ranked, model_scored = rank_from_response(cleaned, response_text)
        return {
            "ranked": [item.as_dict() for item in ranked],
            "endpoint": llm.endpoint_name(),
            "model_scored": model_scored,
            "question_count": len(cleaned),
        }

    @mcp_server.tool
    def best_question(questions: list[str], context: str | None = None) -> dict:
        """Pick the single strongest question from a candidate set.

        A convenience wrapper over ``rank_questions`` for the common "which of
        these should I actually ask?" case. Use ``rank_questions`` instead when
        the full ordering or the per-question scores matter.

        Args:
            questions: Candidate questions. 1-25 entries, each at most 500
                characters.
            context: Optional topic or audience the questions are judged against.

        Returns:
            dict with ``question``, ``score``, ``rationale`` and ``runner_up``
            (the second-best question, or ``None`` when only one was supplied),
            or ``{"error": ..., "message": ...}`` on failure.
        """
        result = rank_questions(questions, context)
        if "error" in result:
            return result

        ranked = result["ranked"]
        top = ranked[0]
        return {
            "question": top["question"],
            "score": top["score"],
            "rationale": top["rationale"],
            "runner_up": ranked[1]["question"] if len(ranked) > 1 else None,
            "endpoint": result["endpoint"],
        }

    @mcp_server.tool
    def whoami() -> dict:
        """Report the calling user and this server's endpoint wiring.

        Doubles as a health check: a successful response confirms the MCP
        transport works, the on-behalf-of-user token reached the server, and the
        ranking endpoint name is configured.

        Returns:
            dict with ``status``, ``user`` (display name and username, or an
            ``error`` key if the user token was unavailable) and ``endpoint``
            (the configured ranking endpoint, or ``None`` if unset).
        """
        try:
            current = utils.get_user_authenticated_workspace_client().current_user.me()
            user = {"display_name": current.display_name, "user_name": current.user_name}
        except Exception as exc:  # noqa: BLE001 - identity is best-effort here
            user = {"error": str(exc)}

        try:
            endpoint = llm.endpoint_name()
        except RuntimeError:
            endpoint = None

        return {"status": "healthy", "user": user, "endpoint": endpoint}
