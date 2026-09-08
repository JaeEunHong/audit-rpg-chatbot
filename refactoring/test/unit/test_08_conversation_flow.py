from pathlib import Path

from stage_01_case_data import load_case_data
from stage_08_audit_pipeline import run_conversation_turn
from conversation_state import ConversationState


ROOT = Path(__file__).resolve().parents[3]


def graph():
    return load_case_data(
        ROOT / "data/llm_review_index.parquet",
        ROOT / "data/entity_master.parquet",
    )


def test_conversation_turn_filters_without_scoring():
    def parser_call(**kwargs):
        return (
            '{"request_type":"new",'
            '"mentioned_entities":[{"type":"contract","id":"SE108426"}],'
            '"references":[],"requested_concerns":[],'
            '"requested_details":["overview"],"requested_action":"lookup",'
            '"filled_values":{},"needs_clarification":false}'
        )

    result = run_conversation_turn(
        "Tell me about SE108426.",
        graph(),
        parser_call=parser_call,
        latest_messages=[],
        conversation_state=ConversationState(),
    )

    assert result["status"] == "ready_for_lookup"
    assert result["filtered_data"]["contracts"] == ["SE108426"]
    assert "score_delta" not in result


def test_conversation_turn_returns_targeted_clarification_for_missing_concern():
    def parser_call(**kwargs):
        return (
            '{"request_type":"new",'
            '"mentioned_entities":[{"type":"contract","id":"SE108426"}],'
            '"references":[],"requested_concerns":[],"requested_details":[], '
            '"requested_action":"explain","filled_values":{},'
            '"needs_clarification":false}'
        )

    result = run_conversation_turn(
        "Check SE108426.",
        graph(),
        parser_call=parser_call,
        latest_messages=[],
        conversation_state=ConversationState(),
    )

    assert result["status"] == "clarification"
    assert result["missing"] == ["concern"]
    assert result["clarification_type"] == "choose_concern"
    assert "question" not in result
    assert result["conversation_state"].pending_confirmation is not None
