from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "main" / "core"
sys.path.insert(0, str(CORE))


def test_main_core_imports_and_prompts():
    import audit_rpg
    import audit_types
    import run_experiment

    assert (CORE / "prompts" / "parser_prompt.md").exists()
    assert (CORE / "prompts" / "generator_prompt.md").exists()
    assert hasattr(audit_types, "AuditRequest")
    assert hasattr(audit_types, "ResponseContext")
