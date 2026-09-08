from pathlib import Path

from audit_types import AuditRequest
from stage_01_case_data import load_case_data
from stage_05_verification import verify_request
from stage_06_scoring import score_entities


ROOT = Path(__file__).resolve().parents[3]


def test_asset_issue_scores_owning_contract_once():
    data = load_case_data(ROOT / "data/llm_review_index.parquet", ROOT / "data/entity_master.parquet")
    request = AuditRequest(
        entity_mentions=[{"mention_id": "asset_1", "kind": "asset", "text": "AST510028"}],
        requested_access="secret", requested_content="explanation",
        issue_claims=[{"mention_id": "asset_1", "candidate_issue": "INFLATED PRICING", "rationale": "The price is inflated."}],
        follow_active_context=False, small_talk=False,
    )
    ledger = {}
    first = verify_request(request, data, ledger)
    second = verify_request(request, data, ledger)
    assert first["status"] == "new_score"
    assert first["score_result"]["score_delta"] == 1
    assert first["score_result"]["findings"][0]["contract_id"] == "SE105792"
    assert second["status"] == "repeat"
    assert second["score_result"]["score_delta"] == 0


def test_entity_scoring_counts_requested_contract_once():
    data = load_case_data(ROOT / "data/llm_review_index.parquet", ROOT / "data/entity_master.parquet")
    ledger = {}
    entities = [{"type": "contract", "id": "SE105792"}]
    first = score_entities(data, ledger, entities, "INFLATED PRICING")
    second = score_entities(data, ledger, entities, "INFLATED PRICING")
    assert first["status"] == "new_score"
    assert first["score_delta"] == 1
    assert second["status"] == "repeat"
    assert second["score_delta"] == 0
