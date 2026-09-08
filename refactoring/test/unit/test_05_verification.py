from pathlib import Path

from audit_types import AuditRequest
from stage_01_case_data import load_case_data
from stage_05_verification import verify_request


ROOT = Path(__file__).resolve().parents[3]


def test_known_asset_without_issue_is_missing_issue():
    data = load_case_data(ROOT / "data/llm_review_index.parquet", ROOT / "data/entity_master.parquet")
    request = AuditRequest(
        [{"mention_id": "asset_1", "kind": "asset", "text": "AST510028"}],
        "public", "explanation", [], False, False,
    )
    result = verify_request(request, data, {})
    assert result["status"] == "clarification"
    assert result["clarification_kind"] == "missing_issue"


def test_known_asset_overview_is_lookup():
    data = load_case_data(ROOT / "data/llm_review_index.parquet", ROOT / "data/entity_master.parquet")
    request = AuditRequest(
        [{"mention_id": "asset_1", "kind": "asset", "text": "AST510028"}],
        "public", "overview", [], False, False,
    )
    result = verify_request(request, data, {})
    assert result["status"] == "lookup"
    assert result["records"][0]["record_id"] == "SE105792"
