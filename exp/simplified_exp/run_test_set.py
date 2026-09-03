from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.audit_rpg import load_case_data, load_env, normalize_ref_list, resolve_issue_key  # noqa: E402
from run_experiment import run_investigation  # noqa: E402


def empty_scope() -> dict[str, list[str]]:
    return {"contracts": [], "customers": [], "assets": [], "vins": []}


SCENARIOS = [
    {
        "name": "contract follow-up and fact lookup",
        "turns": [
            {"message": "Can we review SE105843?", "expected_status": "lookup", "expected_delta": 0},
            {"message": "What was the approval date?", "expected_status": "lookup", "expected_delta": 0},
            {"message": "What happened there?", "expected_status": "clarification", "expected_delta": 0},
        ],
    },
    {
        "name": "new finding then repeat",
        "turns": [
            {"message": "SE100016 has a passenger vehicle outside commercial vehicle financing.", "expected_status": "new_score", "expected_delta": 1},
            {"message": "That passenger vehicle is still a problem.", "expected_status": "repeat", "expected_delta": 0},
        ],
    },
    {
        "name": "customer switch to contract issue",
        "turns": [
            {"message": "Let's investigate CUST0038.", "expected_status": "lookup", "expected_delta": 0},
            {"message": "This customer has AML risk.", "expected_status": "new_score", "expected_delta": 1},
            {"message": "Now check SE100024 for overdue exposure at approval.", "expected_status": "new_score", "expected_delta": 1},
        ],
    },
    {
        "name": "asset and VIN context switches",
        "turns": [
            {"message": "Look at asset AST510028.", "expected_status": "lookup", "expected_delta": 0},
            {"message": "This asset looks overpriced.", "expected_status": "new_score", "expected_delta": 1},
            {"message": "Ignore that. What was the VIN on SE105843?", "expected_status": "lookup", "expected_delta": 0},
        ],
    },
    {
        "name": "merge creates explicit ambiguity",
        "turns": [
            {"message": "SE105843 was approved after the contract started.", "expected_status": "new_score", "expected_delta": 1},
            {"message": "Also check SE100018.", "expected_status": "lookup", "expected_delta": 0},
            {"message": "What happened there?", "expected_status": "clarification", "expected_delta": 0},
        ],
    },
    {
        "name": "two paired claims",
        "turns": [
            {"message": "SE100016 has a passenger vehicle, and SE100024 had overdue exposure at approval.", "expected_status": "new_score", "expected_delta": 2},
            {"message": "We already covered those two findings.", "expected_status": "clarification", "expected_delta": 0},
        ],
    },
    {
        "name": "customer issue plus contract examples",
        "turns": [
            {"message": "CUST0014 has AML risk and inflated pricing.", "expected_status": "needs_contract_examples", "expected_delta": 0},
            {"message": "The customer itself has AML risk.", "expected_status": "unsupported", "expected_delta": 0},
        ],
    },
    {
        "name": "large shared overdue batch",
        "turns": [
            {"message": "SE100021, SE100022, SE100023, SE100024 and SE100025 look like they had overdue exposure when approved.", "expected_status": "new_score", "expected_issue": "ACTIVE_OVERDUE_AT_APPROVAL", "expected_refs": ["SE100021", "SE100022", "SE100023", "SE100024", "SE100025"]},
            {"message": "Those contracts had the same overdue issue.", "expected_status": "repeat", "expected_delta": 0},
        ],
    },
    {
        "name": "unsupported issue remains unsupported",
        "turns": [
            {"message": "SE101096 appears to have AML risk.", "expected_status": "unsupported", "expected_delta": 0},
            {"message": "Why was that an AML problem?", "expected_status": "unsupported", "expected_delta": 0},
        ],
    },
    {
        "name": "customer contract overview",
        "turns": [
            {"message": "Let's go through CUST0014.", "expected_status": "lookup", "expected_delta": 0},
            {"message": "Which contracts do they have?", "expected_status": "lookup", "expected_delta": 0},
            {"message": "Now move to CUST2480.", "expected_status": "lookup", "expected_delta": 0},
        ],
    },
]


def record_for_ref(case_data: dict[str, Any], ref: str) -> dict[str, Any] | None:
    return case_data["contracts"].get(ref) or case_data["customers"].get(ref)


def expected_truth_delta(case_data: dict[str, Any], ledger: dict[str, dict[str, Any]], refs: list[str], issue_type: str) -> int:
    issue_key = resolve_issue_key(case_data, issue_type)
    if not issue_key:
        return 0
    delta = 0
    for ref in normalize_ref_list(case_data, refs):
        record = record_for_ref(case_data, ref)
        ledger_key = f"{ref}::{issue_key}"
        if record and bool(record.get(issue_key)) and ledger_key not in ledger:
            delta += 1
    return delta


def validate_findings(case_data: dict[str, Any], result: dict[str, Any]) -> list[str]:
    errors = []
    for finding in (result.get("score_result") or {}).get("findings", []):
        ref = str(finding.get("record_id") or "")
        issue_key = resolve_issue_key(case_data, str(finding.get("issue_type") or ""))
        record = record_for_ref(case_data, ref)
        truth = bool(record and issue_key and record.get(issue_key))
        status = str(finding.get("status") or "").lower()
        if status in {"new_score", "repeat"} and not truth:
            errors.append(f"invalid scored finding {ref}/{finding.get('issue_type')}")
        if status == "unsupported" and truth:
            errors.append(f"truth-table finding returned unsupported {ref}/{finding.get('issue_type')}")
    return errors


def run_scenario(scenario: dict[str, Any], case_data: dict[str, Any], model: str) -> dict[str, Any]:
    ledger: dict[str, dict[str, Any]] = {}
    scope = empty_scope()
    history: list[dict[str, Any]] = []
    failures: list[str] = []
    turns = []
    for index, turn in enumerate(scenario["turns"], start=1):
        message = turn["message"]
        ledger_before = dict(ledger)
        started = time.perf_counter()
        result = run_investigation(
            message,
            case_data,
            ledger,
            model,
            chat_history=history,
            active_investigation_scope=scope,
        )
        elapsed = round(time.perf_counter() - started, 3)
        scope = result.get("active_investigation_scope") or scope
        request = result.get("request") or {}
        score = result.get("score_result") or {}
        actual_status = result.get("status")
        actual_delta = int(score.get("score_delta") or 0)
        expected_delta = turn.get("expected_delta")
        if expected_delta is None and turn.get("expected_issue"):
            expected_delta = expected_truth_delta(case_data, ledger_before, turn["expected_refs"], turn["expected_issue"])
        turn_errors = validate_findings(case_data, result)
        if actual_status != turn.get("expected_status"):
            turn_errors.append(f"status expected {turn.get('expected_status')}, got {actual_status}")
        if expected_delta is not None and actual_delta != expected_delta:
            turn_errors.append(f"delta expected {expected_delta}, got {actual_delta}")
        if turn_errors:
            failures.extend([f"turn {index}: {error}" for error in turn_errors])
        print("\n" + "-" * 88)
        print(f"SCENARIO: {scenario['name']} | TURN: {index}")
        print(f"INPUT: {message}")
        print(f"PARSER MENTIONS: {json.dumps(request.get('entity_mentions', []), ensure_ascii=False)}")
        print(f"PARSER CLAIMS: {json.dumps(request.get('issue_claims', []), ensure_ascii=False)}")
        print(f"SCOPE: {json.dumps(scope, ensure_ascii=False)}")
        expected_text = turn.get("expected_status")
        if expected_delta is not None:
            expected_text += f"/{expected_delta}"
        print(f"RESULT: {actual_status}/{actual_delta} expected={expected_text} {'PASS' if not turn_errors else 'FAIL'}")
        print(f"REPLY: {str(result.get('reply') or '').replace(chr(10), ' | ')}")
        print(f"LATENCY: {elapsed}s")
        turns.append({"status": actual_status, "delta": actual_delta, "latency": elapsed, "errors": turn_errors})
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": result.get("reply", "")})
    return {"name": scenario["name"], "failures": failures, "turns": turns, "score": sum(turn["delta"] for turn in turns)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run multi-turn experimental audit scenarios against parquet truth.")
    parser.add_argument("--limit", type=int, default=len(SCENARIOS), help="Run only the first N scenarios.")
    parser.add_argument("--model", default="gpt-4.1", help="Compatibility argument; experimental parser/generator models are configured in run_experiment.py.")
    args = parser.parse_args()
    load_env()
    case_data = load_case_data()
    results = [run_scenario(scenario, case_data, args.model) for scenario in SCENARIOS[: max(0, args.limit)]]
    failures = [result for result in results if result["failures"]]
    passed = len(results) - len(failures)
    total_turns = sum(len(result["turns"]) for result in results)
    total_score = sum(result["score"] for result in results)
    print("\n" + "=" * 88)
    print(f"SUMMARY: {passed}/{len(results)} scenarios passed | turns={total_turns} | total_score={total_score}")
    if failures:
        print("FAILURES:")
        for result in failures:
            for failure in result["failures"]:
                print(f"- {result['name']}: {failure}")
    else:
        print("FAILURES: none")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())