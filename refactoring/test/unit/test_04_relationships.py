from pathlib import Path

from stage_01_case_data import assets_for_customer, contracts_for_customer, load_case_data, vins_for_customer
from stage_04_entity_resolution import resolve_target


ROOT = Path(__file__).resolve().parents[3]


def data():
    return load_case_data(ROOT / "data/llm_review_index.parquet", ROOT / "data/entity_master.parquet")


def test_contract_provides_customer_assets_and_vins():
    case = data()
    target = resolve_target(case, "contract", "SE105792")
    record = target["record"]
    assert target["customer_id"] == "CUST0312"
    assert set(record["asset_ids"]) >= {"AST510028"}
    assert "YS20KCLEXP0510028" in record["vins"]


def test_customer_provides_contract_asset_and_vin_sets():
    case = data()
    contracts = contracts_for_customer(case, "CUST0312")
    assert contracts
    expected_assets = {asset for record in contracts for asset in record["asset_ids"]}
    expected_vins = {vin for record in contracts for vin in record["vins"]}
    assert set(assets_for_customer(case, "CUST0312")) == expected_assets
    assert set(vins_for_customer(case, "CUST0312")) == expected_vins


def test_asset_and_vin_share_the_same_contract_and_customer():
    case = data()
    asset = resolve_target(case, "asset", "AST510028")
    vin = resolve_target(case, "vin", "YS20KCLEXP0510028")
    assert asset["contract_id"] == vin["contract_id"] == "SE105792"
    assert asset["customer_id"] == vin["customer_id"] == "CUST0312"
