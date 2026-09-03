from __future__ import annotations

import run_experiment
from shared.audit_rpg import load_case_data
from shared.audit_types import AuditRequest


def request(mentions=None, claims=None, content=None, action="follow", follow=True, small_talk=False):
    return AuditRequest(mentions or [], None, content, claims or [], follow, small_talk, action)


def advance(scope, audit_request, data):
    return run_experiment.apply_context_action_to_scope(audit_request, data, scope)


def verify(audit_request, scope, data, text):
    return run_experiment.verify_investigation_request(audit_request, data, {}, text, scope)


def empty_scope():
    return {"contracts": [], "customers": [], "assets": [], "vins": []}


def test_basic_follow_up_reuses_one_contract():
    data = load_case_data()
    scope = advance(empty_scope(), request([{"mention_id": "e1", "kind": "contract", "text": "SE105843"}], action="replace", follow=False), data)
    result = verify(request(content="overview"), scope, data, "What happened there?")
    assert scope["contracts"] == ["SE105843"]
    assert result["status"] == "lookup"
    assert result["records"][0]["record_id"] == "SE105843"


def test_explanation_follow_up_reuses_same_issue_target():
    data = load_case_data()
    scope = {"contracts": ["SE105843"], "customers": [], "assets": [], "vins": []}
    result = verify(request(claims=[{"mention_id": "active", "candidate_issue": "CONTRACT APPROVED AFTER START DATE", "rationale": "Approval was late."}], content="explanation"), scope, data, "Why?")
    assert result["score_result"]["findings"][0]["record_id"] == "SE105843"


def test_lookup_follow_up_does_not_create_claims():
    data = load_case_data()
    result = verify(request(content="overview"), {"contracts": ["SE105843"], "customers": [], "assets": [], "vins": []}, data, "What was the interest rate?")
    assert result["status"] == "lookup"
    assert result["score_result"] == {}


def test_customer_follow_up_uses_active_customer():
    data = load_case_data()
    result = verify(request(claims=[{"mention_id": "active", "candidate_issue": "AML RISK", "rationale": "AML concern."}]), {"contracts": [], "customers": ["CUST0038"], "assets": [], "vins": []}, data, "What about AML risk?")
    assert result["score_result"]["findings"][0]["record_id"] == "CUST0038"


def test_asset_follow_up_resolves_parent_contract():
    data = load_case_data()
    result = verify(request(content="overview"), {"contracts": [], "customers": [], "assets": ["AST510028"], "vins": []}, data, "Can you explain that?")
    assert result["status"] == "lookup"
    assert result["records"][0]["record_id"] == data["asset_to_contract"]["AST510028"]


def test_vin_follow_up_resolves_parent_contract():
    data = load_case_data()
    vin = next(vin for vin in data["vin_to_contract"] if vin.endswith("0510030"))
    result = verify(request(content="overview"), {"contracts": [], "customers": [], "assets": [], "vins": [vin]}, data, "Why?")
    assert result["status"] == "lookup"
    assert result["records"][0]["record_id"] == data["vin_to_contract"][vin]


def test_merge_keeps_both_contracts_and_follow_up_clarifies():
    data = load_case_data()
    first = request([{"mention_id": "e1", "kind": "contract", "text": "SE105843"}], action="replace", follow=False)
    scope = advance(empty_scope(), first, data)
    second = request([{"mention_id": "e2", "kind": "contract", "text": "SE100018"}], action="merge", follow=False)
    scope = advance(scope, second, data)
    result = verify(request(content="overview"), scope, data, "What happened there?")
    assert scope["contracts"] == ["SE105843", "SE100018"]
    assert result["status"] == "clarification"


def test_replace_removes_old_contract_context():
    data = load_case_data()
    scope = {"contracts": ["SE105843"], "customers": [], "assets": [], "vins": []}
    scope = advance(scope, request([{"mention_id": "e1", "kind": "customer", "text": "CUST0038"}], action="replace", follow=False), data)
    result = verify(request(claims=[{"mention_id": "active", "candidate_issue": "AML RISK", "rationale": "AML concern."}]), scope, data, "What about AML risk?")
    assert scope == {"contracts": [], "customers": ["CUST0038"], "assets": [], "vins": []}
    assert result["score_result"]["findings"][0]["record_id"] == "CUST0038"


def test_small_talk_follow_does_not_change_scope():
    data = load_case_data()
    scope = {"contracts": ["SE105843"], "customers": [], "assets": [], "vins": []}
    updated = advance(scope, request(action="follow", small_talk=True), data)
    assert updated == scope
    result = verify(request(content="overview"), updated, data, "What happened there?")
    assert result["records"][0]["record_id"] == "SE105843"


def test_explicit_entity_overrides_active_scope():
    data = load_case_data()
    scope = {"contracts": ["SE105843"], "customers": [], "assets": [], "vins": []}
    explicit = request(
        [{"mention_id": "e1", "kind": "contract", "text": "SE100016"}],
        [{"mention_id": "e1", "candidate_issue": "NON COMMERCIAL VEHICLE RELATED ASSETS", "rationale": "Passenger vehicle."}],
        follow=True,
    )
    result = verify(explicit, scope, data, "SE100016 has a passenger vehicle.")
    assert result["score_result"]["findings"][0]["record_id"] == "SE100016"