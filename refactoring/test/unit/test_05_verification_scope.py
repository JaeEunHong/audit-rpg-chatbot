from pathlib import Path

from audit_types import AuditRequest
from stage_01_case_data import load_case_data
from stage_05_verification import verify_request


ROOT = Path(__file__).resolve().parents[3]


def test_customer_contract_issue_needs_specific_contract():
    case = load_case_data(ROOT / "data/llm_review_index.parquet", ROOT / "data/entity_master.parquet")
    request = AuditRequest(
        [{"mention_id": "customer_1", "kind": "customer", "text": "CUST0312"}],
        "secret", "explanation",
        [{"mention_id": "customer_1", "candidate_issue": "INFLATED PRICING", "rationale": "The pricing needs review."}],
        False, False,
    )
    result = verify_request(request, case, {})
    assert result["status"] == "needs_contract_examples"
