from __future__ import annotations

import base64
import io
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openai import OpenAI
from PIL import Image, ImageEnhance, ImageOps

from conversation_state import ConversationState
from audit_types import EvidencePackage
from stage_02_visual_extraction import extract_visible_entities
from stage_08_audit_pipeline import run_conversation_turn


ROOT = Path(__file__).resolve().parent
PARSER_PROMPT = ROOT / "prompts" / "stage_03_request_parser_prompt.md"
GENERATOR_PROMPT = ROOT / "prompts" / "stage_07_response_generator_prompt.md"
PARSER_MODEL = "gpt-4.1"
GENERATOR_MODEL = "gpt-4.1-mini"
MAX_GENERATOR_CONTEXT_CHARS = 12000
PORTRAIT_OPTIONS = [
    "looks_good", "amused", "determined", "concerned", "doubtful", "skeptical",
    "defensive", "frustrated", "what_is_this", "tired", "thinking",
    "checking_details", "examining_data", "analysing",
]

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
            "issues": {"type": "array", "items": {"type": "string"}},
            "issue": {"type": ["string", "null"]},
            "request": {"type": "string", "enum": ["overview", "lookup", "check", "compare", "explain", "small_talk", "unknown"]},
            "selection": {"type": ["object", "null"], "additionalProperties": False, "properties": {"mode": {"type": "string", "enum": ["first", "next"]}, "type": {"type": "string", "enum": ["customer", "contract"]}, "count": {"type": "integer"}}, "required": ["mode", "type", "count"]},
        },
        "required": ["entities", "references", "issues", "issue", "request", "selection"],
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
        max_output_tokens=3000,
    )
    return response.output_text or "{}"


def _vision_call(image: str) -> str:
    image = _prepare_visual_image(image)
    response = _client().responses.create(
        model=os.getenv("AUDIT_VISUAL_MODEL", "gpt-5.6"),
        instructions=(ROOT / "prompts" / "stage_02_visual_extraction_prompt.md").read_text(encoding="utf-8"),
        input=[{"role": "user", "content": [{"type": "input_text", "text": "Extract the complete visible table."}, {"type": "input_image", "image_url": image}]}],
        max_output_tokens=6000,
    )
    if response.output_text:
        return response.output_text
    parts = []
    for item in response.output or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                parts.append(text)
    return "\n".join(parts)


def _prepare_visual_image(image: str) -> str:
    """Improve screenshots whose visible text is small before sending them to vision."""
    if not image.startswith("data:image/") or "," not in image:
        return image
    header, encoded = image.split(",", 1)
    try:
        source = Image.open(io.BytesIO(base64.b64decode(encoded)))
    except Exception:
        return image
    width, height = source.size
    if width >= 2400 or height >= 1800:
        return image
    scale = min(2.0, 2400 / max(width, height))
    if scale <= 1.05:
        return image
    enhanced = source.convert("RGB").resize(
        (round(width * scale), round(height * scale)), Image.Resampling.LANCZOS
    )
    enhanced = ImageOps.autocontrast(enhanced, cutoff=1)
    enhanced = ImageEnhance.Contrast(enhanced).enhance(1.12)
    enhanced = ImageEnhance.Sharpness(enhanced).enhance(1.35)
    output = io.BytesIO()
    enhanced.save(output, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _build_generator_instructions(
    context: dict[str, Any], policy: dict[str, Any]
) -> str:
    evidence = context.get("evidence") or {}
    issue_contexts = evidence.get("issues") or []
    explanation_length_instruction = ""
    short_reaction_instruction = ""
    latest_message = str(context.get("latest_auditor_message") or "").strip()
    if evidence.get("status") == "repeat" and len(latest_message.split()) <= 8:
        short_reaction_instruction = (
            "- The auditor is making a short reaction, not asking for a new explanation. "
            "Reply briefly in one or two spoken sentences. Do not repeat the full "
            "narrative or evidence.\n"
        )
    if (
        evidence.get("status") in {"new_score", "partial_confirmed", "repeat"}
        and issue_contexts
        and any(
            item.get("auditor_explanation") or item.get("policy_reason")
            for item in issue_contexts
        )
    ):
        explanation_length_instruction = (
            "- This is a scored group with supplied explanation context. "
            "Write 3-5 complete spoken sentences. Do not stop after one or "
            "two sentences; connect the finding, the supplied explanation, "
            "and a reflective qualifier.\n"
        )
    return f"""
CURRENT RESPONSE INSTRUCTIONS (follow these separately from the evidence):
- tone: {policy.get('tone', 'confident')}
- attitude: {policy.get('attitude_style', 'professional and controlled')}
- rhythm: {policy.get('rhythm_level', 0)}
- allowed moods: {', '.join(policy.get('allowed_moods') or [])}
{explanation_length_instruction}
{short_reaction_instruction}

Use these instructions to shape how Mikael speaks. Do not mention these fields.
Do not let narrative wording override the current tone. Preserve every fact
and finding from the evidence, but create fresh spoken dialogue.
If a portrait override is supplied, return that portrait exactly.
"""
def _generator_call(
    context: dict[str, Any], policy: dict[str, Any], max_output_tokens: int = 1200
) -> str:
    dynamic_instructions = _build_generator_instructions(context, policy)
    response = _client().responses.create(
        model=os.getenv("AUDIT_GENERATOR_MODEL", GENERATOR_MODEL),
        instructions=GENERATOR_PROMPT.read_text(encoding="utf-8") + dynamic_instructions,
        input=json.dumps(context, ensure_ascii=False),
        text={"format": {"type": "json_schema", "name": "mikael_response", "strict": True, "schema": {"type": "object", "additionalProperties": False, "properties": {"speech": {"type": "string"}, "mood": {"type": "string", "enum": ["Embarrassed / Caught", "Professional / Controlled", "Guarded / Hesitant", "Defensive / Cornered", "Reluctant / Defeated", "Annoyed / Dismissive"]}, "portrait": {"type": "string", "enum": PORTRAIT_OPTIONS}}, "required": ["speech", "mood", "portrait"]}}},
        max_output_tokens=max_output_tokens,
    )
    return response.output_text or "{}"


def _generator_json(context: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Parse LLM2 output, retrying with more room if JSON was truncated."""
    try:
        return json.loads(_generator_call(context, policy))
    except (json.JSONDecodeError, TypeError):
        try:
            return json.loads(_generator_call(context, policy, max_output_tokens=2400))
        except (json.JSONDecodeError, TypeError):
            return json.loads(_generator_call(context, policy, max_output_tokens=3600))


def _narrowing_reply(result: dict[str, Any], graph: dict[str, Any]) -> str | None:
    # Once scoring has completed, Mikael must answer about that result. Do not
    # replace a valid scored response with a scope clarification.
    scoring = result.get("scoring") or {}
    if scoring.get("status") in {"new_score", "repeat", "unsupported"}:
        return None
    filtered = result.get("filtered_data", {})
    contracts = list(filtered.get("contracts", []))
    customers = list(filtered.get("customers", []))
    if len(contracts) <= 300 and (contracts or len(customers) <= 300):
        return None
    if contracts:
        return f"I have {len(contracts)} contracts in this set. I can check them, but not all at once. Please narrow it to a smaller group of contract IDs."
    return f"I have {len(customers)} customers in this set. I can check them, but not all at once. Please narrow it to a smaller group of customer IDs."


def _recent_dialogue_for_generator(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return a small style-only window for LLM2, never used for decisions."""
    dialogue: list[dict[str, str]] = []
    completed = [item for item in messages if item.get("role") in {"user", "assistant"}]
    for index in range(max(0, len(completed) - 4), len(completed) - 1, 2):
        auditor = str(completed[index].get("content") or "").strip()
        mikael = str(completed[index + 1].get("content") or "").strip() if index + 1 < len(completed) else ""
        if auditor or mikael:
            dialogue.append({"auditor": auditor[:500], "mikael": mikael[:700]})
    return dialogue[-2:]


def _response_policy(result: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    attitude = result.get("attitude") or {}
    status = evidence.get("status")
    trace = result.get("attitude_trace") or {}
    first_confirmed = (
        status == "new_score"
        and not bool(getattr(result.get("conversation_state"), "has_scored_finding", False))
        and int(trace.get("new_finding_count") or 0) > 0
    )
    if first_confirmed:
        tone = "embarrassed"
    elif status == "mixed_issue" or result.get("state") == "mixed_issue":
        tone = "annoyed_guarded"
    elif status == "partial_confirmed":
        tone = "guarded"
    elif status in {"repeat", "not_found"}:
        tone = "annoyed_confident"
    elif status == "unsupported":
        tone = "annoyed_confident"
    else:
        conversation_state = result.get("conversation_state")
        previous_tone = getattr(conversation_state, "response_tone", None)
        tone = (
            previous_tone
            if previous_tone and status not in {"new_score", "unsupported"}
            else attitude.get("stage", "confident")
        )
    mood_map = {
        "embarrassed": ["Embarrassed / Caught"],
        "confident": ["Professional / Controlled"],
        "guarded": ["Guarded / Hesitant"],
        "defensive": ["Defensive / Cornered"],
        "nervous": ["Reluctant / Defeated"],
        "defeated": ["Reluctant / Defeated"],
        "annoyed_confident": ["Annoyed / Dismissive"],
        "annoyed_guarded": ["Annoyed / Dismissive"],
    }
    style_map = {
        "confident": ("direct and slightly dismissive", 0),
        "embarrassed": ("caught off guard and awkwardly qualifying the answer", 1),
        "guarded": ("cautious and unwilling to commit too quickly", 2),
        "annoyed_confident": ("impatient, clipped, and dismissive", 0),
        "annoyed_guarded": ("irritated but cautious about the scope", 2),
        "nervous": ("uncertain, self-correcting, and increasingly uneasy", 3),
        "defeated": ("tired, reluctant, and no longer trying to defend the decision", 4),
    }
    attitude_style, rhythm_level = style_map.get(
        tone, ("professional and controlled", 0)
    )
    return {
        "tone": tone,
        "allowed_moods": mood_map.get(tone, ["Professional / Controlled"]),
        "attitude_style": attitude_style,
        "rhythm_level": rhythm_level,
        "must_not_overclaim": True,
    }


def _easter_egg_presentation(
    result: dict[str, Any],
    filtered: dict[str, Any],
    scoring: dict[str, Any],
    graph: dict[str, Any],
) -> dict[str, Any] | None:
    """Return presentation overrides only for an exact confirmed scope."""
    if scoring.get("status") not in {"new_score", "repeat", "partial_confirmed"}:
        return None
    if not any(
        item.get("status") in {"new_score", "repeat"}
        for item in scoring.get("findings", [])
    ):
        return None

    contracts = list(filtered.get("contracts", []))
    customers = list(filtered.get("customers", []))
    if len(contracts) == 1:
        record = graph.get("contracts", {}).get(contracts[0], {})
    elif len(customers) == 1 and not contracts:
        record = graph.get("customers", {}).get(customers[0], {})
    else:
        return None

    if not record.get("is_easter_egg"):
        return None
    portrait = str(record.get("easter_egg_portrait") or "").strip()
    if not portrait:
        return None
    return {
        "active": True,
        "portrait_override": portrait,
        "target_id": record.get("contract_id") or record.get("customer_id"),
    }


def _evidence(result: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any] | None:
    if result.get("action") == "small_talk" or result.get("state") == "small_talk":
        return None
    filtered = result.get("filtered_data", {})
    scoring = result.get("scoring") or {}
    contracts = list(filtered.get("contracts", []))
    customers = list(filtered.get("customers", []))
    issues = filtered.get("customer_concerns", []) + filtered.get("contract_concerns", [])
    action = result.get("action") or result.get("request", {}).get("requested_action")
    group_request = len(customers) + len(contracts) > 1
    entity_ids = [
        {"type": "customer", "id": customer_id}
        for customer_id in customers
    ] + [
        {"type": "contract", "id": contract_id}
        for contract_id in contracts
    ]
    all_findings_confirmed = False
    data: dict[str, Any] = {
        "status": scoring.get("status") or result.get("status"),
        "action": action,
        "contract_count": len(contracts),
        "customer_count": len(customers),
        "entity_ids": entity_ids,
        "entity_count": len(entity_ids),
        "contract_ids_sample": contracts[:5],
        "customer_names": [
            graph.get("customers", {}).get(i, {}).get("customer_name", "")
            for i in customers[:10]
        ],
    }
    if result.get("attitude"):
        data["attitude"] = result["attitude"]
        data["attitude_trace"] = result.get("attitude_trace")
    selection = (result.get("request") or {}).get("selection")
    if selection:
        data["selection"] = selection
    if scoring:
        findings = scoring.get("findings", [])
        all_findings_confirmed = bool(findings) and all(
            item.get("status") in {"new_score", "repeat"}
            for item in findings
        )
        finding_groups: dict[str, dict[str, list[str]]] = {}
        for finding in findings:
            status = str(finding.get("status") or "unknown")
            group = finding_groups.setdefault(status, {"customers": [], "contracts": []})
            customer_id = str(finding.get("customer_id") or "")
            contract_id = str(finding.get("contract_id") or "")
            if customer_id and customer_id not in group["customers"]:
                group["customers"].append(customer_id)
            if contract_id and contract_id not in group["contracts"]:
                group["contracts"].append(contract_id)
        mixed_findings = any(
            item.get("status") == "unsupported" for item in findings
        ) and any(
            item.get("status") in {"new_score", "repeat"} for item in findings
        )
        confirmed_contracts = {
            str(item.get("contract_id"))
            for item in findings
            if item.get("status") in {"new_score", "repeat"} and item.get("contract_id")
        }
        data["score_result"] = {
            "status": scoring.get("status"),
            "score": scoring.get("score", 0),
            "score_delta": scoring.get("score_delta", 0),
            "finding_count": len(findings) if not mixed_findings else None,
            "finding_sample": findings[:3] if action == "explain" or not group_request else [],
            "findings_by_status": finding_groups if action == "explain" or not group_request else None,
            "group_result": "mixed" if mixed_findings else "confirmed" if all_findings_confirmed else "unsupported",
        }
    if len(entity_ids) > 100:
        narrative_entities = [
            ("customer", customer_id, graph.get("customers", {}).get(customer_id, {}))
            for customer_id in customers
        ] + [
            ("contract", contract_id, graph.get("contracts", {}).get(contract_id, {}))
            for contract_id in contracts
        ]
        data["narrative_sample"] = [
            {
                "type": entity_type,
                "id": entity_id,
                "public_narrative": str(record.get("public_description") or ""),
            }
            for entity_type, entity_id, record in narrative_entities[:10]
            if record.get("public_description")
        ]
        data["narrative_sample_is_partial"] = True
    data["requested_issue"] = (result.get("request") or {}).get("requested_concerns", [])
    data["response_policy"] = _response_policy(result, data)
    presentation = _easter_egg_presentation(result, filtered, scoring, graph)
    if presentation:
        data["easter_egg_presentation"] = presentation
        data["response_policy"]["portrait_override"] = presentation["portrait_override"]
    data["issue_candidates"] = result.get("issue_candidates", [])
    data["context_routing"] = {
        "state": result.get("state"),
        "clarification_type": result.get("clarification_type"),
    }
    data["missing"] = result.get("missing", [])
    data["clarification_type"] = result.get("clarification_type")
    if issues and (
        action == "explain"
        or scoring.get("status") in {"new_score", "repeat", "mixed_issue"}
    ):
        issue_contexts = []
        seen_issue_contexts = set()
        for item in issues:
            policy_reason = str(item.get("why_it_violates_policy") or "").strip()
            auditor_explanation = str(
                item.get("explanation_for_auditor") or ""
            ).strip()
            # The secret narrative is issue-level context. It is repeated on
            # every customer/contract row in the source data, so keep one
            # canonical explanation per issue for the response model.
            key = item.get("name")
            if key in seen_issue_contexts:
                continue
            seen_issue_contexts.add(key)
            issue_contexts.append({
                "name": item.get("name"),
                "confirmed": item.get("confirmed"),
                "issue_description": graph.get("concern_catalog", {}).get(
                    item.get("name"), {}
                ).get("description", ""),
                "policy_reason": policy_reason,
                "auditor_explanation": auditor_explanation,
            })
        data["issues"] = issue_contexts
    decision = result.get("decision_result") or {}
    data["evidence_package"] = asdict(EvidencePackage(
        status=str(data.get("status") or "not_run"),
        issue=((result.get("request") or {}).get("requested_concerns") or [None])[0],
        selected_count=len(contracts) + len(customers),
        confirmed_count=int(decision.get("confirmed_count") or 0),
        unsupported_count=int(decision.get("unsupported_count") or 0),
        score_delta=int(decision.get("score_delta") or 0),
        entity_samples=entity_ids[:10],
        public_narrative_samples=data.get("narrative_sample", []),
        secret_narrative_samples=data.get("issues", []),
        tone=(data.get("response_policy") or {}).get("tone"),
    ))
    return data


def run_chat_turn(message: str, graph: dict[str, Any], state: ConversationState, messages: list[dict[str, Any]], ledger: dict[str, Any], image_data_urls: list[str] | None = None, status_callback: Any = None, team: str = "default") -> dict[str, Any]:
    image_text = None
    if image_data_urls:
        if status_callback:
            status_callback("Mikael is looking at the screenshot.")
        image_text = extract_visible_entities(image_data_urls[0], _vision_call)
    elif messages:
        image_text = str(messages[-1].get("image_text") or "").strip() or None
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
        team=team,
    )
    result["evidence"] = _evidence(result, graph)
    request = result.get("request") or {}
    result["action"] = result.get("action") or request.get("requested_action")
    if result.get("action") == "small_talk":
        result["evidence"] = None
    narrowing_reply = _narrowing_reply(result, graph)
    if narrowing_reply:
        result["scoring"] = None
        result["reply"] = narrowing_reply
        result["mood"] = "Guarded / Hesitant"
        result["visual_extraction_text"] = image_text or ""
        return result
    if status_callback:
        status_callback("Mikael is typing...")
    generator_evidence = result["evidence"]
    generator_policy: dict[str, Any] = {}
    if isinstance(generator_evidence, dict):
        # The activity log keeps the complete evidence. The response model only
        # needs counts, score status, attitude, and a small narrative sample.
        generator_evidence = dict(generator_evidence)
        generator_policy = dict(generator_evidence.pop("response_policy", {}) or {})
        package = generator_evidence.get("evidence_package")
        if isinstance(package, dict):
            package = dict(package)
            package.pop("tone", None)
            package.pop("secret_narrative_samples", None)
            generator_evidence["evidence_package"] = package
        generator_evidence["entity_ids"] = list(generator_evidence.get("entity_ids", []))[:10]
        if result.get("status") == "clarification":
            generator_evidence.pop("narrative_sample", None)
        else:
            generator_evidence["narrative_sample"] = list(generator_evidence.get("narrative_sample", []))[:6]
    reply_context = {
        "latest_auditor_message": message,
        "evidence": generator_evidence,
        "recent_dialogue": _recent_dialogue_for_generator(messages),
    }
    serialized_context = json.dumps(reply_context, ensure_ascii=False)
    if len(serialized_context) > MAX_GENERATOR_CONTEXT_CHARS:
        result["reply"] = "I have too much detail here to review reliably at once. Could you narrow it down to a smaller group of records?"
        result["mood"] = "Guarded / Hesitant"
        result["visual_extraction_text"] = image_text or ""
        return result
    try:
        generated = _generator_json(reply_context, generator_policy)
    except (json.JSONDecodeError, TypeError):
        result["reply"] = "Sorry, I didn’t catch that. Could you say it again?"
        result["visual_extraction_text"] = image_text or ""
        return result
    result["reply"] = str(generated.get("speech") or "").strip()
    policy = (result.get("evidence") or {}).get("response_policy") or {}
    allowed_moods = policy.get("allowed_moods") or ["Professional / Controlled"]
    result["mood"] = generated.get("mood") if generated.get("mood") in allowed_moods else allowed_moods[0]
    state.response_tone = str(policy.get("tone") or "confident")
    if (
        result.get("status") == "new_score"
        and int((result.get("decision_result") or {}).get("confirmed_count") or 0) > 0
    ):
        state.has_scored_finding = True
    result["portrait"] = (
        policy.get("portrait_override")
        or generated.get("portrait")
        or "looks_good"
    )
    result["visual_extraction_text"] = image_text or ""
    return result
