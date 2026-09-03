from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.audit_rpg import load_case_data
from shared.audit_types import AuditRequest
from run_experiment import build_response_context, verify_investigation_request


def request(mentions, claims):
    return AuditRequest(mentions, None, None, claims, False, False)


def test_valid_single_claim_scores_and_exposes_matching_material():
    data = load_case_data()
    ledger = {}
    result = verify_investigation_request(
        request(
            [{"mention_id": "e1", "kind": "contract", "text": "SE100016"}],
            [{"mention_id": "e1", "candidate_issue": "NON COMMERCIAL VEHICLE RELATED ASSETS", "rationale": "It includes a passenger vehicle."}],
        ),
        data,
        ledger,
    )
    assert result["score_result"]["score_delta"] == 1
    assert result["approved_material"]["findings"][0]["record_id"] == "SE100016"


def test_unsupported_claim_does_not_expose_secret_material():
    data = load_case_data()
    result = verify_investigation_request(
        request(
            [{"mention_id": "e1", "kind": "contract", "text": "SE101096"}],
            [{"mention_id": "e1", "candidate_issue": "AML RISK", "rationale": "The customer may have AML risk."}],
        ),
        data,
        {},
    )
    assert result["score_result"]["score_delta"] == 0
    assert result["approved_material"] == {}


def test_two_mentions_keep_their_issue_pairing():
    data = load_case_data()
    result = verify_investigation_request(
        request(
            [
                {"mention_id": "e1", "kind": "contract", "text": "SE100016"},
                {"mention_id": "e2", "kind": "contract", "text": "SE100024"},
            ],
            [
                {"mention_id": "e1", "candidate_issue": "NON COMMERCIAL VEHICLE RELATED ASSETS", "rationale": "Passenger vehicle."},
                {"mention_id": "e2", "candidate_issue": "ACTIVE_OVERDUE_AT_APPROVAL", "rationale": "Overdue exposure was active."},
            ],
        ),
        data,
        {},
    )
    findings = result["score_result"]["findings"]
    assert {(item["record_id"], item["issue_type"]) for item in findings} == {
        ("SE100016", "NON COMMERCIAL VEHICLE RELATED ASSETS"),
        ("SE100024", "ACTIVE_OVERDUE_AT_APPROVAL"),
    }


def test_customer_contract_level_claim_requests_examples():
    data = load_case_data()
    result = verify_investigation_request(
        request(
            [{"mention_id": "e1", "kind": "customer", "text": "CUST0014"}],
            [{"mention_id": "e1", "candidate_issue": "INFLATED PRICING", "rationale": "Financed amounts look inflated."}],
        ),
        data,
        {},
    )
    assert result["status"] == "needs_contract_examples"


def test_context_keeps_only_approved_material():
    context = build_response_context({
        "status": "unsupported",
        "records": [],
        "score_result": {"score_delta": 0},
        "approved_material": {},
    })
    assert context.public_material == {}
    assert context.approved_material == {}


def test_lookup_context_exposes_public_narrative_only():
    context = build_response_context({
        "status": "lookup",
        "requested_content": "asset_details",
        "records": [{
            "record_type": "contract",
            "record_id": "SE105792",
            "contract_id": "SE105792",
            "customer_id": "CUST0312",
            "customer_name": "Alder & Brisk Malar Freight Ltd",
            "asset_mix_summary": "Three trucks",
            "brand_summary": "Scania and MAN",
            "public_narrative": "Three trucks, Scania and MAN.",
        }],
        "score_result": {},
        "approved_material": {},
    })

    assert context.requested_content == "asset_details"
    assert context.records[0]["public_narrative"] == "Three trucks, Scania and MAN."
    assert context.approved_material == {}


def test_multi_issue_score_context_keeps_all_matching_material():
    context = build_response_context({
        "status": "new_score",
        "records": [{
            "record_type": "contract",
            "record_id": "SE110459",
            "contract_id": "SE110459",
            "customer_id": "CUST2480",
            "customer_name": "Lund Enterprises",
            "public_narrative": "One passenger vehicle, Porsche.",
        }],
        "score_result": {"score_delta": 2},
        "approved_material": {"findings": [
            {
                "record_id": "SE110459",
                "issue_type": "NON COMMERCIAL VEHICLE RELATED ASSETS",
                "explanation_given_to_auditor": "The vehicle was treated as supporting broader business activity.",
            },
            {
                "record_id": "SE110459",
                "issue_type": "INTEREST RATE EXTREMELY LOW",
                "explanation_given_to_auditor": "The pricing was treated as temporary VIP support.",
            },
        ]},
    })

    assert "public_narrative" not in context.records[0]
    assert [item["issue_type"] for item in context.approved_material["findings"]] == [
        "NON COMMERCIAL VEHICLE RELATED ASSETS",
        "INTEREST RATE EXTREMELY LOW",
    ]

def test_customer_issue_scores_but_contract_issue_requests_contract_id():
    data = load_case_data()
    result = verify_investigation_request(
        request(
            [{"mention_id": "e1", "kind": "customer", "text": "CUST2480"}],
            [
                {"mention_id": "e1", "candidate_issue": "CUSTOMER IN TAX HAVEN", "rationale": "The customer is registered in Panama."},
                {"mention_id": "e1", "candidate_issue": "NON COMMERCIAL VEHICLE RELATED ASSETS", "rationale": "The customer has passenger vehicles."},
            ],
        ),
        data,
        {},
    )
    assert result["score_result"]["score_delta"] == 1
    assert result["status"] == "new_score"
    assert "contract ID" in result["clarification"]