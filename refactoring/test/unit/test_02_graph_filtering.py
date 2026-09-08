from pathlib import Path

from stage_01_case_data import load_case_data
from stage_04_entity_resolution import filter_related_data


ROOT = Path(__file__).resolve().parents[3]


def graph():
    return load_case_data(
        ROOT / "data/llm_review_index.parquet",
        ROOT / "data/entity_master.parquet",
    )


def test_contract_start_filters_customer_asset_vin_and_customer_concern():
    result = filter_related_data(graph(), {
        "start_from": [{"type": "contract", "id": "SE108426"}],
        "look_for": [{"type": "customer", "id": "CUST0941"}],
        "concerns": ["CONTRACT APPROVED AFTER START DATE"],
        "action": "lookup",
    })

    assert result["customers"] == ["CUST0941"]
    assert result["contracts"] == ["SE108426"]
    assert result["assets"] == ["AST513523"]
    assert result["vins"] == ["YS2T9C5UES0513523"]
    assert result["customer_concerns"][0]["name"] == (
        "CONTRACT APPROVED AFTER START DATE"
    )
    assert result["contract_concerns"][0]["name"] == (
        "CONTRACT APPROVED AFTER START DATE"
    )
    assert "score" not in result


def test_overview_filter_does_not_return_unrequested_concerns():
    result = filter_related_data(graph(), {
        "start_from": [{"type": "contract", "id": "SE108426"}],
        "requested_concerns": [],
    })

    assert result["customer_concerns"] == []
    assert result["contract_concerns"] == []


def test_fifty_contracts_keep_all_records_and_filter_the_requested_issue():
    case = graph()
    contract_ids = sorted(case["contracts"])[:50]
    result = filter_related_data(case, {
        "start_from": [
            {"type": "contract", "id": contract_id}
            for contract_id in contract_ids
        ],
        "requested_concerns": ["MISSING OR WEAK APPROVAL NARRATIVE"],
    })

    assert len(result["contracts"]) == 50
    assert [item["contract_id"] for item in result["contract_concerns"]] == [
        "SE100003", "SE100006", "SE100013", "SE100021",
        "SE100029", "SE100042", "SE100046",
    ]
