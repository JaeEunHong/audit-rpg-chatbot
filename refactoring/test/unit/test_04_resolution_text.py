from pathlib import Path

from stage_01_case_data import load_case_data
from stage_04_entity_resolution import resolve_text


ROOT = Path(__file__).resolve().parents[3]


def test_text_resolution_accepts_spaced_asset_id():
    case = load_case_data(ROOT / "data/llm_review_index.parquet", ROOT / "data/entity_master.parquet")
    result = resolve_text(case, "asset AST 510028")
    assert result["refs"] == ["SE105792"]
    assert result["targets"][0]["input_id"] == "AST510028"
