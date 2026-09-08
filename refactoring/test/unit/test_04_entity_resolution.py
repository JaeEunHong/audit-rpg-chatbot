from pathlib import Path

from stage_01_case_data import load_case_data
from stage_04_entity_resolution import resolve_target


ROOT = Path(__file__).resolve().parents[3]


def case_data():
    return load_case_data(ROOT / "data/llm_review_index.parquet", ROOT / "data/entity_master.parquet")


def test_asset_resolves_to_contract_customer_assets_and_vins():
    target = resolve_target(case_data(), "asset", "AST510028")
    assert target["status"] == "resolved"
    assert target["contract_id"] == "SE105792"
    assert target["customer_id"] == "CUST0312"
    assert "AST510028" in target["asset_ids"]
    assert "YS20KCLEXP0510028" in target["vins"]


def test_vin_resolves_to_contract():
    target = resolve_target(case_data(), "vin", "YS20KCLEXP0510028")
    assert target["contract_id"] == "SE105792"


def test_unknown_asset_is_not_found():
    assert resolve_target(case_data(), "asset", "AST999999")["status"] == "not_found"
