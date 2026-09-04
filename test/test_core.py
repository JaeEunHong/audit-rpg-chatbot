from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "main" / "core"
sys.path.insert(0, str(CORE))


def test_main_core_imports_and_prompts():
    import audit_types
    import conversation_state
    import stage_01_case_data
    import stage_03_request_parser
    import stage_06_scoring
    import stage_08_audit_pipeline

    assert (CORE / "prompts" / "stage_03_request_parser_prompt.md").exists()
    assert (CORE / "prompts" / "stage_07_response_generator_prompt.md").exists()
    assert hasattr(audit_types, "AuditRequest")
    assert hasattr(conversation_state, "ConversationState")
    assert hasattr(stage_01_case_data, "load_case_data")
    assert hasattr(stage_03_request_parser, "parse_conversation_request")
    assert hasattr(stage_06_scoring, "score_entities")
    assert hasattr(stage_08_audit_pipeline, "run_conversation_turn")
