from stage_05_verification import check_filtered_request


def filtered(*, contract="SE105792"):
    return {
        "customers": ["CUST0312"] if contract else [],
        "contracts": [contract] if contract else [],
        "assets": ["AST510028"] if contract else [],
        "vins": [],
    }


def test_known_entity_without_request_has_missing_issue_state():
    result = check_filtered_request(filtered(), None, [], ["AML RISK"])
    assert result["state"] == "missing_issue"


def test_known_entity_overview_has_lookup_state():
    result = check_filtered_request(filtered(), "overview", [])
    assert result["state"] == "lookup"


def test_known_entity_with_issue_has_scoring_state():
    result = check_filtered_request(filtered(), "assess", ["INFLATED PRICING"])
    assert result["state"] == "ready_for_scoring"


def test_no_entity_without_issue_has_combined_missing_state():
    result = check_filtered_request(filtered(contract=None), None, [])
    assert result["state"] == "missing_entity_and_issue"


def test_no_entity_with_issue_has_missing_entity_state():
    result = check_filtered_request(filtered(contract=None), "assess", ["AML RISK"])
    assert result["state"] == "missing_entity"
