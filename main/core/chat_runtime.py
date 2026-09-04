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
        max_output_tokens=6000,
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
        max_output_tokens=1200,
    )
    return response.output_text or "{}"


def _reply_data(result: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    filtered = result.get("filtered_data", {})
    scoring = result.get("scoring") or {}
    data: dict[str, Any] = {"status": scoring.get("status") or result.get("status")}
    if scoring:
        data["score_result"] = {"status": scoring.get("status"), "score": scoring.get("score", 0), "score_delta": scoring.get("score_delta", 0), "findings": scoring.get("findings", [])}
    data["requested_issue"] = (result.get("request") or {}).get("requested_concerns", [])
    data["missing"] = result.get("missing", [])
    data["clarification_type"] = result.get("clarification_type")
    data["customers"] = [{"id": i, "name": graph.get("customers", {}).get(i, {}).get("customer_name", "")} for i in filtered.get("customers", [])]
    data["contracts"] = [{"id": i} for i in filtered.get("contracts", [])]
    issues = filtered.get("customer_concerns", []) + filtered.get("contract_concerns", [])
    if issues:
        data["issues"] = issues
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
    if status_callback:
        status_callback("Mikael is typing...")
    generated = json.loads(_generator_call({"latest_auditor_message": message, "python_result": _reply_data(result, graph)}))
    result["reply"] = str(generated.get("speech") or "").strip()
    result["visual_extraction_text"] = image_text or ""
    return result
