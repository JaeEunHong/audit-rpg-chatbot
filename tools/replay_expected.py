import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main" / "core"))

from chat_runtime import run_chat_turn
from conversation_state import ConversationState
from app_support import load_env


graph = json.loads((ROOT / "main" / "output" / "case_graph.json").read_text(encoding="utf-8"))
load_env(ROOT / ".env")
old = (ROOT / "replay_results.md").read_text(encoding="utf-8")
questions = [
    m.group(1).strip()
    for m in re.finditer(r"### User\n\n(.+?)(?=\n\n### Mikael)", old)
]
expectations = [
    "MISSING OR WEAK APPROVAL NARRATIVE; explicit group; score only if confirmed majority",
    "CONTRACT APPROVED AFTER START DATE; explicit group; majority result from data",
    "CONTRACT APPROVED AFTER START DATE; explicit group; majority result from data",
    "Multiple concerns (approval narrative/authority); clarification or one clearly selected issue",
    "INTEREST RATE EXTREMELY LOW; one concern across 15 explicit contracts; never mixed_issue",
    "DOWN PAYMENT TOO LOW; explicit group; majority result from data",
    "APPROVAL BY ROLE THAT DOESN'T EXIST; explicit group; majority result from data",
    "MISSING OR WEAK APPROVAL NARRATIVE; explicit group; process as explicit records",
    "MISSING OR WEAK APPROVAL NARRATIVE; explicit group; report confirmed subset if majority",
    "ACTIVE_OVERDUE_AT_APPROVAL; explicit group; majority result from data",
    "Customer relation plus weak narrative; answer/score only after scope is clear",
    "Customer all contracts; all_related_contracts; score from all five contracts",
    "No issue stated; ambiguous_issue clarification without listing issue candidates",
    "INFLATED PRICING; explicit contract/asset; unsupported should use public narrative only",
    "APPROVAL AUTHORITY concern; explicit group; majority result from data",
    "CONTRACT APPROVED AFTER START DATE; explicit group; same issue, never mixed_issue",
    "Multiple concerns; clarification or cautious investigation, no invented issue",
    "Customer-level issue across named customers; resolve each customer without focus leakage",
    "Customer all contracts; all_related_contracts; evaluate both customer scopes",
    "Invalid entities; not_found; do not reuse previous focus",
]

state = ConversationState()
messages = []
ledger = {}
out = ["# 20-turn LLM replay with data-based expectations\n"]

for index, question in enumerate(questions):
    result = run_chat_turn(
        question,
        graph,
        state,
        messages,
        ledger,
        team="replay",
        record_work=False,
    )
    messages.append({"role": "user", "content": question})
    messages.append({"role": "assistant", "content": result.get("reply", "")})
    scoring = result.get("scoring") or {}
    evidence = result.get("evidence") or {}
    out.extend([
        f"## Turn {index + 1}",
        "",
        "### User",
        "",
        question,
        "",
        "### Expected from current graph/data",
        "",
        expectations[index],
        "",
        "### Mikael",
        "",
        result.get("reply", ""),
        "",
        "### Actual routing",
        "",
        f"- Status: `{result.get('status')}`",
        f"- State: `{result.get('state')}`",
        f"- Scope: `{result.get('scope_intent') or (result.get('request') or {}).get('scope_intent')}`",
        f"- Starting entities: `{len((result.get('request') or {}).get('starting_points') or [])}`",
        f"- Issue: `{(result.get('request') or {}).get('requested_concerns')}`",
        f"- Score status: `{scoring.get('status')}`",
        f"- Score eligible: `{scoring.get('score_eligible')}`",
        f"- Score delta: `{scoring.get('score_delta')}`",
        f"- Confirmed: `{sum(item.get('status') in {'new_score', 'repeat'} for item in scoring.get('findings', []))}` / Unsupported: `{sum(item.get('status') == 'unsupported' for item in scoring.get('findings', []))}`",
        f"- Clarification type: `{result.get('clarification_type')}`",
        "",
    ])

(ROOT / "replay_results_expected.md").write_text("\n".join(out), encoding="utf-8")
print("wrote replay_results_expected.md")
