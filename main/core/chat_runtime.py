from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from openai import OpenAI

from conversation_state import ConversationState
from stage_02_visual_extraction import extract_visible_entities
from stage_08_audit_pipeline import run_conversation_turn


ROOT = Path(__file__).resolve().parent
PARSER_PROMPT = ROOT / "prompts" / "stage_03_request_parser_prompt.md"
GENERATOR_PROMPT = ROOT / "prompts" / "stage_07_response_generator_prompt.md"
PARSER_MODEL = "gpt-4.1"
GENERATOR_MODEL = "gpt-4.1-mini"
MAX_GENERATOR_CONTEXT_CHARS = 12000

PARSER_SCHEMA = {
    "type": "json_schema",
    "name": "conversation_request",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "entities": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {"type": {"type": "string", "enum": ["customer", "contract", "asset", "vin"]}, "id": {"type": "string"}}, "required": ["type", "id"]}},
            "references": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {"text": {"type": "string"}, "source_message": {"type": "integer"}, "selection": {"type": ["object", "null"], "additionalProperties": False, "properties": {"mode": {"type": "string", "enum": ["one", "all", "first", "last"]}, "type": {"type": "string"}, "count": {"type": ["integer", "null"]}}, "required": ["mode", "type", "count"]}}, "required": ["text", "source_message", "selection"]}},
            "issue": {"type": ["string", "null"]},
            "request": {"type": "string", "enum": ["overview", "lookup", "check", "compare", "explain", "unknown"]},
        },
        "required": ["entities", "references", "issue", "request"],
    },
}


def _client() -> OpenAI:
    return OpenAI()


def _parser_call(**payload: Any) -> str:
    response = _client().responses.create(
        model=os.getenv("AUDIT_PARSER_MODEL", PARSER_MODEL),
        instructions=PARSER_PROMPT.read_text(encoding="utf-8"),
        input=json.dumps(payload, ensure_ascii=False),
        text={"format": PARSER_SCHEMA},
        max_output_tokens=10000,
    )
    return response.output_text or "{}"


def _vision_call(image: str) -> str:
    response = _client().responses.create(
        model=os.getenv("AUDIT_VISUAL_MODEL", "gpt-5.6"),
        instructions=(ROOT / "prompts" / "stage_02_visual_extraction_prompt.md").read_text(encoding="utf-8"),
        input=[{"role": "user", "content": [{"type": "input_text", "text": "Extract the complete visible table."}, {"type": "input_image", "image_url": image}]}],
        max_output_tokens=6000,
    )
    return response.output_text or ""


def _generator_call(context: dict[str, Any]) -> str:
    response = _client().responses.create(
        model=os.getenv("AUDIT_GENERATOR_MODEL", GENERATOR_MODEL),
        instructions=GENERATOR_PROMPT.read_text(encoding="utf-8"),
        input=json.dumps(context, ensure_ascii=False),
        text={"format": {"type": "json_schema", "name": "mikael_response", "strict": True, "schema": {"type": "object", "additionalProperties": False, "properties": {"speech": {"type": "string"}}, "required": ["speech"]}}},
        max_output_tokens=500,
    )
    return response.output_text or "{}"


def _narrowing_reply(result: dict[str, Any], graph: dict[str, Any]) -> str | None:
    filtered = result.get("filtered_data", {})
    contracts = list(filtered.get("contracts", []))
    customers = list(filtered.get("customers", []))
    if len(contracts) <= 50 and (contracts or len(customers) <= 50):
        return None
    if contracts:
        return f"I have {len(contracts)} contracts in this set. I can check them, but not all at once. Please narrow it to a smaller group of contract IDs."
    return f"I have {len(customers)} customers in this set. I can check them, but not all at once. Please narrow it to a smaller group of customer IDs."


def _evidence(result: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any] | None:
    if result.get("action") == "small_talk" or result.get("state") == "small_talk":
        return None
    filtered = result.get("filtered_data", {})
    scoring = result.get("scoring") or {}
    contracts = list(filtered.get("contracts", []))
    customers = list(filtered.get("customers", []))
    issues = filtered.get("customer_concerns", []) + filtered.get("contract_concerns", [])
    data: dict[str, Any] = {
        "status": scoring.get("status") or result.get("status"),
        "action": result.get("action") or result.get("request", {}).get("requested_action"),
        "contract_count": len(contracts),
        "customer_count": len(customers),
        "contract_ids_sample": contracts[:5],
        "customer_names": [
            graph.get("customers", {}).get(i, {}).get("customer_name", "")
            for i in customers[:10]
        ],
    }
    if scoring:
        findings = scoring.get("findings", [])
        data["score_result"] = {
            "status": scoring.get("status"),
            "score": scoring.get("score", 0),
            "score_delta": scoring.get("score_delta", 0),
            "finding_count": len(findings),
            "finding_sample": findings[:3],
        }
    data["requested_issue"] = (result.get("request") or {}).get("requested_concerns", [])
    data["missing"] = result.get("missing", [])
    data["clarification_type"] = result.get("clarification_type")
    if issues:
        data["issues"] = [{
            "name": item.get("name"),
            "confirmed": item.get("confirmed"),
            "explanation": item.get("explanation_for_auditor"),
        } for item in issues[:10]]
    return data


def run_chat_turn(message: str, graph: dict[str, Any], state: ConversationState, messages: list[dict[str, Any]], ledger: dict[str, Any], image_data_urls: list[str] | None = None, status_callback: Any = None) -> dict[str, Any]:
    image_text = None
    if image_data_urls:
        if status_callback:
            status_callback("Mikael is looking at the screenshot.")
        image_text = extract_visible_entities(image_data_urls[0], _vision_call)
    if status_callback:
        status_callback("Mikael is checking the system.")
    result = run_conversation_turn(
        message,
        graph,
        parser_call=_parser_call,
        latest_messages=messages[-10:],
        conversation_state=state,
        known_concern_names=[{"name": name, **definition} for name, definition in graph.get("concern_catalog", {}).items()],
        image_text=image_text,
        ledger=ledger,
    )
    result["evidence"] = _evidence(result, graph)
    request = result.get("request") or {}
    result["action"] = result.get("action") or request.get("requested_action")
    if result.get("action") == "small_talk":
        result["reply"] = "I understand."
        result["visual_extraction_text"] = image_text or ""
        return result
    narrowing_reply = _narrowing_reply(result, graph)
    if narrowing_reply:
        result["scoring"] = None
        result["reply"] = narrowing_reply
        result["visual_extraction_text"] = image_text or ""
        return result
    if status_callback:
        status_callback("Mikael is typing...")
    reply_context = {"latest_auditor_message": message, "evidence": result["evidence"]}
    serialized_context = json.dumps(reply_context, ensure_ascii=False)
    if len(serialized_context) > MAX_GENERATOR_CONTEXT_CHARS:
        result["reply"] = "I have too much detail here to review reliably at once. Could you narrow it down to a smaller group of records?"
        result["visual_extraction_text"] = image_text or ""
        return result
    try:
        generated = json.loads(_generator_call(reply_context))
    except (json.JSONDecodeError, TypeError):
        result["reply"] = "Sorry, I didn’t catch that. Could you say it again?"
        result["visual_extraction_text"] = image_text or ""
        return result
    result["reply"] = str(generated.get("speech") or "").strip()
    result["visual_extraction_text"] = image_text or ""
    return result
