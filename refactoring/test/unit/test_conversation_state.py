from conversation_state import ConversationState


def test_state_keeps_focus_and_limits_recent_turns():
    state = ConversationState(focus_entities=[{"type": "customer", "id": "CUST2480"}])
    for number in range(4):
        state.add_turn(f"question {number}", f"answer {number}")
    assert state.focus_entities == [{"type": "customer", "id": "CUST2480"}]
    assert [item["auditor"] for item in state.recent_turns] == [
        "question 1", "question 2", "question 3"
    ]


def test_state_keeps_pending_confirmation_until_resolved():
    state = ConversationState.from_dict({
        "entities": [{"type": "customer", "id": "CUST2480"}],
        "pending_request": {"kind": "issue", "question": "Which issue?"},
    })
    assert state.focus_entities == [{"type": "customer", "id": "CUST2480"}]
    assert state.pending_confirmation["kind"] == "issue"
