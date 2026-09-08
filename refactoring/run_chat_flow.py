"""Interactive terminal runner for inspecting the LLM1 and graph flow."""

from __future__ import annotations

import json
import os
import sys
import argparse
from pathlib import Path
from typing import Any

from openai import OpenAI

CORE = Path(__file__).resolve().parent / "main" / "core"
PROMPT = CORE / "prompts" / "stage_03_request_parser_prompt.md"
GENERATOR_PROMPT = CORE / "prompts" / "stage_07_response_generator_prompt.md"
GRAPH = Path(__file__).resolve().parent / "output" / "case_graph.json"
PARSER_MODEL = "gpt-4.1"
GENERATOR_MODEL = "gpt-4.1-mini"
sys.path.insert(0, str(CORE))

from stage_04_entity_resolution import expand_conversation_references, filter_related_data  # noqa: E402
from stage_05_verification import check_filtered_request  # noqa: E402
from stage_03_request_parser import merge_pending_request, parse_conversation_request  # noqa: E402


DEBUG = False


PARSER_SCHEMA = {
    "type": "json_schema",
    "name": "conversation_request",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "enum": ["customer", "contract", "asset", "vin"]},
                        "id": {"type": "string"},
                    },
                    "required": ["type", "id"],
                },
            },
            "references": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "text": {"type": "string"},
                        "source_message": {"type": "integer"},
                        "selection": {
                            "type": ["object", "null"],
                            "additionalProperties": False,
                            "properties": {
                                "mode": {"type": "string", "enum": ["one", "all", "first", "last"]},
                                "type": {"type": "string"},
                                "count": {"type": ["integer", "null"]},
                            },
                            "required": ["mode", "type", "count"],
                        },
                    },
                    "required": ["text", "source_message", "selection"],
                },
            },
            "issue": {"type": ["string", "null"]},
            "request": {"type": "string", "enum": ["overview", "lookup", "check", "compare", "explain", "unknown"]},
        },
        "required": [
            "entities", "issue", "request", "references",
        ],
    },
}


def load_local_env() -> None:
    env_file = Path(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def call_llm1(**payload: Any) -> str:
    prompt_input = {
        "current_message": payload["current_message"],
        "latest_messages": payload["latest_messages"],
        "active_context": payload["active_context"],
        "pending_request": payload["pending_request"],
        "known_concern_names": payload["known_concern_names"],
        "image_text": payload.get("image_text"),
        "retry_instruction": payload.get("retry_instruction", ""),
    }
    client = OpenAI()
    raw = ""
    for token_limit in (1600, 3200, 6000):
        response = client.responses.create(
            model=os.getenv("AUDIT_PARSER_MODEL", PARSER_MODEL),
            instructions=PROMPT.read_text(encoding="utf-8"),
            input=json.dumps(prompt_input, ensure_ascii=False, indent=2),
            text={"format": PARSER_SCHEMA},
            max_output_tokens=token_limit,
        )
        raw = response.output_text or ""
        try:
            json.loads(raw)
            break
        except json.JSONDecodeError:
            print(f"LLM1 output incomplete at max_tokens={token_limit}; retrying.")
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        print("LLM1 did not return complete JSON; returning clarification state.")
        raw = json.dumps({
            "request_type": "new",
            "mentioned_entities": [],
            "references": [],
            "requested_concerns": [],
            "requested_details": [],
            "requested_action": None,
            "filled_values": {
                "concern": None,
                "action": None,
                "starting_point": None,
            },
            "needs_clarification": True,
        })
    if DEBUG:
        print("\n--- LLM1 raw output ---")
        print(raw)
    return raw


def call_llm2(message: str, result: dict[str, Any]) -> str:
    response = OpenAI().responses.create(
        model=os.getenv("AUDIT_GENERATOR_MODEL", GENERATOR_MODEL),
        instructions=GENERATOR_PROMPT.read_text(encoding="utf-8"),
        input=json.dumps({
            "latest_auditor_message": message,
            "python_result": result,
        }, ensure_ascii=False, indent=2),
        text={"format": {
            "type": "json_schema",
            "name": "mikael_response",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "speech": {"type": "string"}
                },
                "required": ["speech"]
            }
        }},
        max_output_tokens=600,
    )
    return json.loads(response.output_text)["speech"].strip()


def print_json(title: str, value: Any) -> None:
    print(f"\n--- {title} ---")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> None:
    global DEBUG
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    DEBUG = args.debug
    load_local_env()
    if not GRAPH.exists():
        raise SystemExit("case_graph.json is missing. Run stage_00_build_case_graph.py first.")
    case_data = json.loads(GRAPH.read_text(encoding="utf-8"))
    concern_names = sorted(case_data.get("concern_catalog", {}))
    messages: list[dict[str, Any]] = []
    active_context: dict[str, Any] = {}
    pending: dict[str, Any] | None = None

    print("LLM1 debug chat. Commands: /reset, /quit")
    while True:
        message = input("\nauditor> ").strip()
        if message == "/quit":
            return
        if message == "/reset":
            messages.clear()
            active_context = {}
            pending = None
            print("Conversation state reset.")
            continue
        if not message:
            continue

        parsed = parse_conversation_request(
            message,
            call_llm1,
            latest_messages=messages,
            active_context=active_context,
            pending_request=pending,
            known_concern_names=concern_names,
        )
        if DEBUG:
            print_json("parsed request", parsed)

        request = merge_pending_request(pending, parsed)
        request["starting_points"] = list(request.get("starting_points") or [])
        request["starting_points"].extend(
            {"type": item.get("type"), "id": item.get("id")}
            for item in parsed.get("mentioned_entities", [])
        )
        request["starting_points"].extend(expand_conversation_references(
            case_data, parsed.get("references", []), messages
        ))
        filtered = filter_related_data(case_data, request)
        state = check_filtered_request(
            filtered,
            request.get("requested_action"),
            request.get("requested_concerns", []),
            sorted(case_data.get("concern_catalog", {})),
        )
        output = {**state, "filtered_data": filtered}
        if DEBUG:
            print_json("merged request", request)
            print_json("python filter result", output)

        speech = call_llm2(message, output)
        print(f"\nMikael> {speech}")

        pending = request if state["status"] == "clarification" else None
        active_context = {
            "entities": [
                {"type": "customer", "id": item}
                for item in filtered["customers"]
            ] + [
                {"type": "contract", "id": item}
                for item in filtered["contracts"]
            ]
        }
        messages.append({
            "message_number": len(messages) + 1,
            "speaker": "auditor",
            "content": message,
        })
        messages.append({
            "message_number": len(messages) + 1,
            "speaker": "mikael",
            "content": speech,
        })


def _context_sentence(filtered: dict[str, Any]) -> str:
    contracts = filtered.get("contracts", [])
    customers = filtered.get("customers", [])
    if len(contracts) == 1 and len(customers) == 1:
        return f"The selected contract is {contracts[0]} for customer {customers[0]}."
    if customers:
        return f"The selected customer is {customers[0]}."
    return "The requested records were not identified."


if __name__ == "__main__":
    main()
