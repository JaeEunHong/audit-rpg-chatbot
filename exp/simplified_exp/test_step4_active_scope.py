from __future__ import annotations

import run_experiment
from shared.audit_rpg import load_case_data
from shared.audit_types import AuditRequest


def test_follow_up_uses_one_active_contract_for_lookup():
    result = run_experiment.verify_investigation_request(AuditRequest([], None, "overview", [], True, False), load_case_data(), {}, "What happened there?", {"contracts": ["SE105843"], "customers": [], "assets": [], "vins": []})
    assert result["status"] == "lookup"
    assert [record["record_id"] for record in result["records"]] == ["SE105843"]


def test_follow_up_uses_one_active_contract_for_scoring():
    result = run_experiment.verify_investigation_request(AuditRequest([], None, None, [{"mention_id": "active", "candidate_issue": "ACTIVE_OVERDUE_AT_APPROVAL", "rationale": "The approval had overdue exposure."}], True, False), load_case_data(), {}, "Why was that approved?", {"contracts": ["SE105843"], "customers": [], "assets": [], "vins": []})
    assert result["status"] == "new_score"
    assert result["score_result"]["score_delta"] == 1


def test_multiple_active_contracts_clarify_instead_of_guessing():
    result = run_experiment.verify_investigation_request(AuditRequest([], None, "overview", [], True, False), load_case_data(), {}, "What happened there?", {"contracts": ["SE105843", "SE100018"], "customers": [], "assets": [], "vins": []})
    assert result["status"] == "clarification"


def test_empty_active_scope_keeps_existing_clarification():
    result = run_experiment.verify_investigation_request(AuditRequest([], None, "overview", [], True, False), load_case_data(), {}, "What happened there?", {"contracts": [], "customers": [], "assets": [], "vins": []})
    assert result["status"] == "clarification"


def test_explicit_entity_overrides_active_scope():
    request = AuditRequest([{"mention_id": "e1", "kind": "contract", "text": "SE100016"}], None, None, [{"mention_id": "e1", "candidate_issue": "NON COMMERCIAL VEHICLE RELATED ASSETS", "rationale": "Passenger vehicle."}], True, False)
    result = run_experiment.verify_investigation_request(request, load_case_data(), {}, "SE100016 has a passenger vehicle.", {"contracts": ["SE105843"], "customers": [], "assets": [], "vins": []})
    assert result["status"] == "new_score"
    assert result["score_result"]["findings"][0]["record_id"] == "SE100016"