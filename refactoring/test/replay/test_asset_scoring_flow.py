from pathlib import Path

from audit_types import AuditRequest
from stage_01_case_data import load_case_data
from stage_08_audit_pipeline import run_audit_pipeline


ROOT = Path(__file__).resolve().parents[3]


def test_asset_scoring_replay_keeps_canonical_contract():
    data = load_case_data(ROOT / "data/llm_review_index.parquet", ROOT / "data/entity_master.parquet")
    request = AuditRequest(
        [{"mention_id": "asset_1", "kind": "asset", "text": "AST510028"}],
        "secret", "explanation",
        [{"mention_id": "asset_1", "candidate_issue": "INFLATED PRICING", "rationale": "The pricing is inflated."}],
        False, False,
    )
    ledger = {}
    first = run_audit_pipeline(request, data, ledger)
    second = run_audit_pipeline(request, data, ledger)
    assert first["score_result"]["findings"][0]["record_id"] == "SE105792"
    assert second["status"] == "repeat"
