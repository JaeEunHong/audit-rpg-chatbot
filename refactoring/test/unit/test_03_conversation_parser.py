from stage_03_request_parser import merge_pending_request, parse_conversation_request


def test_parser_receives_auditor_context_and_preserves_plural_reference():
    received = {}

    def parser_call(**kwargs):
        received.update(kwargs)
        return '{"request_type":"new","mentioned_entities":[],' \
               '"references":[{"text":"those contracts","source_message":8,' \
               '"selection":{"mode":"all","type":"contract"}}],' \
               '"requested_concerns":[],"requested_details":["customer"],' \
               '"requested_action":"lookup","filled_values":{},' \
               '"needs_clarification":false}'

    result = parse_conversation_request(
        "Check the customers for those contracts.",
        parser_call,
        latest_messages=[{"message_number": i, "speaker": "auditor", "content": str(i)} for i in range(12)],
        active_context={"entities": [{"type": "contract", "id": "SE108426"}]},
        known_concern_names=["AML RISK"],
    )

    assert len(received["latest_messages"]) == 10
    assert result["references"][0]["selection"]["mode"] == "all"
    assert result["requested_action"] == "lookup"


def test_clarification_answer_fills_only_the_pending_gap():
    pending = {
        "starting_points": ["contract:SE108426"],
        "missing": ["concern"],
        "requested_action": "explain",
    }
    result = merge_pending_request(pending, {
        "request_type": "continue",
        "filled_values": {"concern": "INFLATED PRICING"},
    })

    assert result["starting_points"] == ["contract:SE108426"]
    assert result["requested_concerns"] == ["INFLATED PRICING"]
    assert result["missing"] == []
    assert result["needs_clarification"] is False


def test_parser_keeps_only_unique_current_entities():
    def parser_call(**kwargs):
        return (
            '{"request_type":"new","mentioned_entities":['
            '{"type":"contract","id":"se108426"},'
            '{"type":"contract","id":"SE108426"}],'
            '"references":[],"requested_concerns":[],"requested_details":[],'
            '"requested_action":"lookup","filled_values":{},'
            '"needs_clarification":false}'
        )

    result = parse_conversation_request(
        "Tell me about SE108426.", parser_call, latest_messages=[]
    )

    assert result["mentioned_entities"] == [
        {"type": "contract", "id": "SE108426"}
    ]
