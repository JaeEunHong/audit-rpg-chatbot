from pathlib import Path

from audit_types import AuditRequest
from stage_01_case_data import load_case_data
from stage_08_audit_pipeline import run_audit_pipeline


ROOT = Path(__file__).resolve().parents[3]


def test_known_asset_without_issue_replays_as_missing_issue():
    case = load_case_data(ROOT / "data/llm_review_index.parquet", ROOT / "data/entity_master.parquet")
    request = AuditRequest(
        [{"mention_id": "a1", "kind": "asset", "text": "AST510028"}],
        "public", "explanation", [], False, False,
    )
    result = run_audit_pipeline(request, case, {})
    assert result["status"] == "clarification"
    assert result["clarification_kind"] == "missing_issue"
