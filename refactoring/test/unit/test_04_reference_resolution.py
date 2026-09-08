from pathlib import Path

from stage_01_case_data import load_case_data
from stage_04_entity_resolution import expand_conversation_references


ROOT = Path(__file__).resolve().parents[3]


def test_all_contracts_reference_is_expanded_from_previous_message():
    case = load_case_data(
        ROOT / "data/llm_review_index.parquet",
        ROOT / "data/entity_master.parquet",
    )
    references = [{
        "text": "those contracts",
        "source_message": 8,
        "selection": {"mode": "all", "type": "contract"},
    }]
    messages = [{
        "message_number": 8,
        "speaker": "mikael",
        "content": "Contracts SE108426 and SE108427 were discussed.",
    }]

    result = expand_conversation_references(case, references, messages)

    assert result == [
        {"type": "contract", "id": "SE108426"},
        {"type": "contract", "id": "SE108427"},
    ]


def test_reference_can_be_resolved_from_previous_mikael_message():
    case = load_case_data(
        ROOT / "data/llm_review_index.parquet",
        ROOT / "data/entity_master.parquet",
    )
    result = expand_conversation_references(case, [{
        "text": "that contract",
        "source_message": 2,
        "selection": {"mode": "one", "type": "contract"},
    }], [{
        "message_number": 2,
        "speaker": "mikael",
        "content": "Contract SE108426 belongs to customer CUST0941.",
    }])

    assert result == [{"type": "contract", "id": "SE108426"}]
