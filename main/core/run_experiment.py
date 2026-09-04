from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audit_rpg import (  # noqa: E402
    DEFAULT_MODEL,
    build_spoken_identification,
    get_openai_client,
    image_to_data_url,
    load_case_data,
    load_env,
    normalize_ref_list,
    resolve_issue_key,
    resolve_record_references_from_text,
    response_text,
    update_score,
)
from audit_types import AuditRequest, ResponseContext  # noqa: E402


def zoom_table_id_image(data_url: str) -> str | None:
    """Create a readable crop of the left table columns for row-ID extraction."""
    try:
        from PIL import Image

        _header, encoded = data_url.split(",", 1)
        image = Image.open(BytesIO(base64.b64decode(encoded)))
        crop_width = max(320, int(image.width * 0.42))
        crop = image.crop((0, 0, min(crop_width, image.width), image.height))
        crop = crop.resize((crop.width * 2, crop.height * 2))
        output = BytesIO()
        crop.save(output, format="PNG")
        return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except Exception:
        return None
MAX_RECORDS = 5
MAX_SHARED_ISSUE_BATCH_RECORDS = 50
MAX_CLAIMS = 10
MAX_FINDINGS_FOR_FULL_CONTEXT = 30
PARSER_MODEL = "gpt-4.1"
VISUAL_MODEL = "gpt-5.6"
GENERATOR_MODEL = "gpt-4.1-mini"
CUSTOMER_SCOPED_ISSUES = {
    "AML RISK",
    "CUSTOMER IN TAX HAVEN",
    "CONNECTED CUSTOMER EXPOSURE HIDDEN BY SEPARATE CUSTOMER IDS",
}


def issue_is_customer_scoped(case_data: dict[str, Any], issue_type: str) -> bool:
    issue_key = resolve_issue_key(case_data, issue_type)
    return any(resolve_issue_key(case_data, name) == issue_key for name in CUSTOMER_SCOPED_ISSUES)


def contracts_for_customer(case_data: dict[str, Any], customer_id: str) -> list[dict[str, Any]]:
    return [
        record
        for record in case_data["contracts"].values()
        if str(record.get("customer_id") or "").upper() == customer_id.upper()
    ]


def format_previous_visible_dialogue(
    chat_history: list[dict[str, Any]] | None,
    latest_message: str,
    max_messages: int = 10,
) -> str:
    visible: list[str] = []
    skipped_latest_user = False
    for item in chat_history or []:
        role = item.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = item.get("content")
        has_image = bool(item.get("images"))
        if not isinstance(text, str) or not text.strip():
            text = ""
        if not text and not has_image:
            continue
        if role == "user" and not skipped_latest_user and text == latest_message:
            skipped_latest_user = True
            continue
        label = "Auditor" if role == "user" else "Mikael (Auditee)"
        if has_image:
            text = f"{text}" + chr(10) + "<Image attached>" if text else "<Image attached>"
        visual_text = str(item.get("visual_extraction_text") or "").strip()
        if visual_text:
            prefix = f"{text}" + chr(10) if text else ""
            text = prefix + "Visual extraction table - [screenshot attached]:" + chr(10) + visual_text
        visible.append(f"{label}: {text}")
    return "\n".join(visible[-max_messages:])


def build_identifier_only_scope(
    case_data: dict[str, Any],
    active_refs: dict[str, list[str]] | None,
) -> dict[str, list[str]]:
    scope = {
        "contracts": [],
        "customers": [],
        "assets": [],
        "vins": [],
    }
    for kind in scope:
        values = (active_refs or {}).get(kind, [])
        scope[kind] = [str(value) for value in values if str(value).strip()]
    return scope


def pre_extracted_entities_from_visual_table(
    case_data: dict[str, Any],
    table_text: str,
) -> list[dict[str, str]]:
    lines = [
        line.strip()
        for line in str(table_text or "").splitlines()
        if line.strip().startswith("|")
    ]
    recognized_headers = ("contractid", "customerid", "customername", "assetid", "vin")
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if any(header in line.lower().replace(" ", "").replace("_", "") for header in recognized_headers)
        ),
        None,
    )
    if header_index is None:
        return []

    headers = [
        cell.strip().lower().replace(" ", "").replace("_", "")
        for cell in lines[header_index].strip("|").split("|")
    ]
    entities: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add_entity(kind: str, value: str) -> None:
        value = value.strip()
        if not value or value in {"-", "—"}:
            return
        key = (kind, value)
        if key in seen:
            return
        seen.add(key)
        entities.append({
            "mention_id": f"e{len(entities) + 1}",
            "kind": kind,
            "text": value,
        })

    for line in lines[header_index + 1:]:
        values = [cell.strip() for cell in line.strip("|").split("|")]
        if values and all(not value.replace("-", "").replace(":", "").strip() for value in values):
            continue
        row = dict(zip(headers, values))
        for ref in normalize_ref_list(case_data, [row.get("contractid", "")]):
            if ref in case_data.get("contracts", {}):
                add_entity("contract", ref)
        for ref in normalize_ref_list(case_data, [row.get("customerid", "")]):
            if ref in case_data.get("customers", {}):
                add_entity("customer", ref)
        for ref in normalize_ref_list(case_data, [row.get("assetid", "")]):
            if ref in case_data.get("asset_to_contract", {}):
                add_entity("asset", ref)
        vin = row.get("vin", "").replace(" ", "").replace("-", "").replace("_", "").upper()
        if vin in case_data.get("vin_to_contract", {}):
            add_entity("vin", vin)
        add_entity("name", row.get("customername", ""))

    return entities


def audit_request_from_value(value: dict[str, Any]) -> AuditRequest:
    return AuditRequest(
        entity_mentions=list(value.get("entity_mentions") or []),
        requested_access=value.get("requested_access"),
        requested_content=value.get("requested_content"),
        issue_claims=list(value.get("issue_claims") or []),
        follow_active_context=bool(value.get("follow_active_context")),
        small_talk=bool(value.get("small_talk")),
        context_action=str(value.get("context_action") or "follow"),
    )


def parser_review_required(
    request: AuditRequest,
    case_data: dict[str, Any],
    message: str,
) -> bool:
    if not request.entity_mentions or request.small_talk:
        return False
    if request.requested_content not in {"explanation", "policy"}:
        return False
    explicit_refs = resolve_record_references_from_text(case_data, message).get("refs", [])
    return not request.issue_claims or not explicit_refs

def visual_extraction_quality(
    case_data: dict[str, Any],
    table_text: str,
) -> dict[str, Any]:
    lines = [line.strip() for line in str(table_text or "").splitlines() if line.strip().startswith("|")]
    header_index = next((index for index, line in enumerate(lines) if any(header in line.lower().replace(" ", "").replace("_", "") for header in ("contractid", "customerid", "customername", "assetid", "vin"))), None)
    quality = {"has_contract_column": False, "valid_contract_count": 0, "blank_contract_rows": 0, "invalid_contract_ids": [], "unclear_customer_names": []}
    if header_index is None:
        return quality
    headers = [cell.strip().lower().replace(" ", "").replace("_", "") for cell in lines[header_index].strip("|").split("|")]
    quality["has_contract_column"] = "contractid" in headers
    for line in lines[header_index + 1:]:
        values = [cell.strip() for cell in line.strip("|").split("|")]
        if not values or all(not value.replace("-", "").replace(":", "").strip() for value in values):
            continue
        row = dict(zip(headers, values))
        contract_value = row.get("contractid", "").strip()
        if "contractid" in headers:
            if not contract_value or contract_value in {"-", "?", "\ufffd"}:
                quality["blank_contract_rows"] += 1
            elif normalize_ref_list(case_data, [contract_value]):
                quality["valid_contract_count"] += 1
            else:
                quality["invalid_contract_ids"].append(contract_value)
        customer_name = row.get("customername", "").strip()
        if customer_name and any(marker in customer_name for marker in ("\ufffd", "?")):
            quality["unclear_customer_names"].append(customer_name)
    return quality


def visual_quality_clarification(request: AuditRequest, quality: dict[str, Any] | None, case_data: dict[str, Any]) -> str | None:
    if not quality:
        return None
    if quality.get("blank_contract_rows") or quality.get("invalid_contract_ids"):
        return "I cannot reliably read every ContractID in that screenshot. Please upload a sharper crop or paste the ContractIDs as text."
    customer_issue = any(issue_is_customer_scoped(case_data, str(claim.get("candidate_issue") or "")) for claim in request.issue_claims)
    if customer_issue and quality.get("unclear_customer_names"):
        return "Some CustomerNames are unclear in the screenshot. Please upload a sharper crop or provide the CustomerIDs."
    return None
def parse_investigation_request(
    message: str,
    case_data: dict[str, Any],
    model: str,
    image_path: str | None = None,
    image_data_urls: list[str] | None = None,
    chat_history: list[dict[str, Any]] | None = None,
    active_investigation_scope: dict[str, list[str]] | None = None,
    visual_text_out: list[str] | None = None,
    visual_quality_out: list[dict[str, Any]] | None = None) -> AuditRequest:
    load_env()
    client = get_openai_client()
    image_urls = []
    if image_path:
        path = Path(image_path)
        image_urls.append(image_to_data_url(path.name, path.read_bytes()))
    image_urls.extend(str(url) for url in (image_data_urls or []) if url)
    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": "Current auditor message:\n" + message},
        {
            "type": "input_text",
            "text": "Recent visible dialogue:\n"
            + (format_previous_visible_dialogue(chat_history, message) or "(none)"),
        },
        {
            "type": "input_text",
            "text": "Active investigation scope:\n"
            + json.dumps(
                active_investigation_scope
                or {"contracts": [], "customers": [], "assets": [], "vins": []},
                ensure_ascii=False,
            ),
        },
    ]
    visual_entity_text = ""
    if image_urls:
        visual_prompt = """You are a visual entity extractor for an audit table.
Read the entire attached image and return plain text only.
Extract every readable row from top to bottom. Do not filter rows by the auditor's issue.
Use only values that are visibly present and readable. Never infer, complete, or invent a missing ID,
customer, asset, or VIN. Use these canonical column names when the corresponding column is visible:
ContractID for values beginning with SE and six digits; CustomerID for CUST plus four digits;
CustomerName for company or legal-entity names; AssetID for AST plus six digits; VIN for a
VIN-like 17-character alphanumeric value. A screenshot may contain any subset of these columns.
Do not force absent columns into the output and do not add blank placeholder columns.
Return one Markdown table in top-to-bottom order with only the visible canonical columns.
Use one row for every readable visible table row. Do not classify issues, score records, summarize
the table, or omit rows after the first few.        """
        visual_content: list[dict[str, Any]] = [
            {"type": "input_text", "text": "Extract all visible table entities."},
        ]
        visual_content.extend({"type": "input_image", "image_url": url, "detail": "high"} for url in image_urls)
        for url in image_urls:
            zoomed_url = zoom_table_id_image(url)
            if zoomed_url:
                visual_content.append({
                    "type": "input_text",
                    "text": "This is a zoomed table crop. Read every visible ContractID row; do not filter by issue.",
                })
                visual_content.append({"type": "input_image", "image_url": zoomed_url, "detail": "high"})
        visual_response = client.responses.create(
            model=VISUAL_MODEL,
            instructions=visual_prompt,
            input=[{"role": "user", "content": visual_content}],
            max_output_tokens=6000,
            reasoning={"effort": "none"},
        )
        visual_entity_text = response_text(visual_response).strip()
        if not visual_entity_text:
            raise RuntimeError("Visual entity extractor returned no output.")
        content.append({
            "type": "input_text",
            "text": "Visual extraction table - [screenshot attached]:\n" + visual_entity_text,
        })
        pre_entities = pre_extracted_entities_from_visual_table(case_data, visual_entity_text)
        content.append({
            "type": "input_text",
            "text": "Python-validated pre-extracted entities. Preserve every item and mention_id exactly:" + json.dumps(pre_entities, ensure_ascii=False),
        })
        if visual_text_out is not None:
            visual_text_out.append(visual_entity_text)
        if visual_quality_out is not None:
            visual_quality_out.append(visual_extraction_quality(case_data, visual_entity_text))
    schema = {
        "type": "json_schema",
        "name": "audit_request",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "entity_mentions": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "mention_id": {"type": "string"},
                        "kind": {"type": "string", "enum": ["contract", "customer", "asset", "vin", "name", "unknown"]},
                        "text": {"type": "string"},
                    },
                    "required": ["mention_id", "kind", "text"],
                }},
                "requested_access": {"type": ["string", "null"], "enum": ["public", "secret", None]},
                "requested_content": {"type": ["string", "null"], "enum": ["overview", "identity", "asset_details", "vin", "explanation", "policy", "scorecard", None]},
                "issue_claims": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "mention_id": {"type": "string"},
                        "candidate_issue": {"type": "string", "enum": case_data["issue_columns"] or [""]},
                        "rationale": {"type": "string"},
                    },
                    "required": ["mention_id", "candidate_issue", "rationale"],
                }},
                "follow_active_context": {"type": "boolean"},
                "small_talk": {"type": "boolean"},
                "context_action": {"type": "string", "enum": ["follow", "merge", "replace"]},
            },
            "required": ["entity_mentions", "requested_access", "requested_content", "issue_claims", "follow_active_context", "small_talk", "context_action"],
        },
    }
    prompt = ((Path(__file__).parent / "prompts" / "parser_prompt.md").read_text(encoding="utf-8")
              + "\n\nRuntime issue catalog:\n"
              + case_data.get("issue_catalog_text", "\n".join(f"- {x}" for x in case_data["issue_columns"])))
    response = client.responses.create(
        model=PARSER_MODEL,
        instructions=prompt,
        input=[{"role": "user", "content": content}],
        text={"format": schema},
        max_output_tokens=10000,
    )
    raw = response_text(response)
    if not raw:
        raise RuntimeError("Parser returned no structured output.")
    request = audit_request_from_value(json.loads(raw))
    if parser_review_required(request, case_data, message):
        review_instructions = prompt + """

PARSER REVIEW GATE:
The previous JSON is only a draft. Reconsider the latest Auditor message semantically before returning JSON.
Do not copy a previous Mikael fallback or clarification. If the latest message contains a concrete audit concern,
map it to the closest issue type in the runtime issue catalog and attach it to the relevant existing entity mentions.
If the latest message is only a lookup, visibility check, or genuinely vague statement, keep issue_claims empty.
When the latest message has no explicit record reference and uses pronouns such as "it", "that", or "your policy", bind the concern and target to the immediately preceding Auditor concern. Do not select every row from the visual table and do not revive an older issue from history.
Return the complete corrected audit_request JSON.
"""
        review_input = [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Latest Auditor message:\n" + message},
                {"type": "input_text", "text": "Recent visible dialogue:\n" + (format_previous_visible_dialogue(chat_history, message) or "(none)")},
                {"type": "input_text", "text": "Visual extraction table:\n" + (visual_entity_text or "(none)")},
                {"type": "input_text", "text": "Active investigation scope:\n" + json.dumps(active_investigation_scope or {"contracts": [], "customers": [], "assets": [], "vins": []}, ensure_ascii=False)},
                {"type": "input_text", "text": "Initial parser draft:\n" + json.dumps(request.__dict__, ensure_ascii=False)},
            ],
        }]
        review_response = client.responses.create(
            model=PARSER_MODEL,
            instructions=review_instructions,
            input=review_input,
            text={"format": schema},
            max_output_tokens=10000,
        )
        review_raw = response_text(review_response)
        if review_raw:
            request = audit_request_from_value(json.loads(review_raw))
    return request

def apply_context_action_to_scope(
    request: AuditRequest,
    case_data: dict[str, Any],
    current_scope: dict[str, list[str]] | None,
) -> dict[str, list[str]]:
    scope = build_identifier_only_scope(case_data, current_scope)
    if request.context_action == "follow":
        return scope

    resolved_scope = {
        "contracts": [],
        "customers": [],
        "assets": [],
        "vins": [],
    }
    for mention in request.entity_mentions:
        text = str(mention.get("text") or "")
        kind = str(mention.get("kind") or "").lower()
        if not text:
            continue
        resolution = resolve_record_references_from_text(case_data, text)
        if resolution.get("ambiguous"):
            continue

        if kind == "asset":
            values = [item.get("asset_id") for item in resolution.get("resolved_from_assets", [])]
            for value in values:
                if value and value in case_data.get("asset_to_contract", {}):
                    if value not in resolved_scope["assets"]:
                        resolved_scope["assets"].append(value)
        elif kind == "vin":
            values = [item.get("vin") for item in resolution.get("resolved_from_vins", [])]
            for value in values:
                if value and value in case_data.get("vin_to_contract", {}):
                    if value not in resolved_scope["vins"]:
                        resolved_scope["vins"].append(value)
        elif kind in {"customer", "name"}:
            values = list(resolution.get("customers", []))
            values.extend(item.get("customer_id") for item in resolution.get("resolved_from_names", []))
            for value in values:
                if value and value in case_data.get("customers", {}) and value not in resolved_scope["customers"]:
                    resolved_scope["customers"].append(value)
        else:
            for value in resolution.get("contracts", []):
                if value not in resolved_scope["contracts"]:
                    resolved_scope["contracts"].append(value)
            for value in resolution.get("customers", []):
                if value not in resolved_scope["customers"]:
                    resolved_scope["customers"].append(value)

    if request.context_action == "replace":
        return resolved_scope

    for kind in scope:
        for value in resolved_scope[kind]:
            if value not in scope[kind]:
                scope[kind].append(value)
    return scope


def active_scope_records(case_data: dict[str, Any], scope: dict[str, list[str]] | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in (scope or {}).get("contracts", []):
        record = case_data["contracts"].get(str(ref))
        if record and record.get("record_id") not in seen:
            records.append({"ref": str(ref), "record": record})
            seen.add(str(record.get("record_id")))
    for ref in (scope or {}).get("customers", []):
        record = case_data["customers"].get(str(ref))
        if record and record.get("record_id") not in seen:
            records.append({"ref": str(ref), "record": record})
            seen.add(str(record.get("record_id")))
    for ref in (scope or {}).get("assets", []):
        contract_ref = case_data.get("asset_to_contract", {}).get(str(ref))
        record = case_data["contracts"].get(contract_ref) if contract_ref else None
        if record and record.get("record_id") not in seen:
            records.append({"ref": str(contract_ref), "record": record})
            seen.add(str(record.get("record_id")))
    for ref in (scope or {}).get("vins", []):
        contract_ref = case_data.get("vin_to_contract", {}).get(str(ref))
        record = case_data["contracts"].get(contract_ref) if contract_ref else None
        if record and record.get("record_id") not in seen:
            records.append({"ref": str(contract_ref), "record": record})
            seen.add(str(record.get("record_id")))
    return records

def public_lookup_slice(record: dict[str, Any], requested_content: str | None) -> str:
    content = requested_content or "overview"
    narrative = str(record.get("public_narrative") or "")
    if content in {"overview", "identity"}:
        return build_spoken_identification(record)
    if content == "asset_details":
        return f"{record.get('asset_mix_summary') or 'The asset details are not listed here.'}. Brands: {record.get('brand_summary') or 'not specified'}."
    if content == "vin":
        vins = record.get("vins") or []
        return "VINs: " + ", ".join(str(vin) for vin in vins) if vins else "No VIN is listed here."
    if content == "date":
        matches = re.findall(r"(?:running|approved on) ([0-9]{4}-[0-9]{2}-[0-9]{2})(?: to ([0-9]{4}-[0-9]{2}-[0-9]{2}))?", narrative, flags=re.IGNORECASE)
        if matches:
            dates = [" to ".join(item for item in match if item) for match in matches]
            return "Dates: " + "; ".join(dates) + "."
        return "I don't have the relevant date in front of me."
    if content == "interest_rate":
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)% interest", narrative, flags=re.IGNORECASE)
        return f"Interest rate: {match.group(1)}%." if match else "I don't have the interest rate in front of me."
    if content == "exposure":
        match = re.search(r"customer exposure after approval was ([^.]+)", narrative, flags=re.IGNORECASE)
        return f"Exposure after approval: {match.group(1)}." if match else "I don't have the exposure figure in front of me."
    if content == "approval":
        match = re.search(r"approved on ([0-9]{4}-[0-9]{2}-[0-9]{2}) up to ([^.]+)", narrative, flags=re.IGNORECASE)
        return f"Approval: {match.group(1)}, up to {match.group(2)}." if match else "I don't have the approval details in front of me."
    return build_spoken_identification(record)

def verify_investigation_request(
    request: AuditRequest,
    case_data: dict[str, Any],
    ledger: dict[str, dict[str, Any]],
    original_text: str | None = None,
    active_investigation_scope: dict[str, list[str]] | None = None,
    visual_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality_clarification = visual_quality_clarification(request, visual_quality, case_data)
    if quality_clarification:
        return {
            "status": "clarification",
            "clarification": quality_clarification,
            "records": [],
            "score_result": {},
            "approved_material": {},
        }
    resolved: dict[str, list[dict[str, Any]]] = {}
    ambiguous: list[str] = []
    ambiguity_refs: list[str] = []
    missing: list[str] = []
    for mention in request.entity_mentions:
        mention_id = str(mention.get("mention_id") or "")
        text = str(mention.get("text") or "")
        result = resolve_record_references_from_text(case_data, text)
        if result.get("ambiguous"):
            ambiguous.extend(str(item) for item in result["ambiguous"])
        refs = normalize_ref_list(case_data, result.get("refs", []))
        if not refs:
            missing.append(text)
        for ref in refs:
            record = case_data["contracts"].get(ref) or case_data["customers"].get(ref)
            if record:
                resolved.setdefault(mention_id, []).append({"ref": ref, "record": record})

    latest_explicit_refs = []
    if original_text:
        latest_explicit_refs = normalize_ref_list(case_data, resolve_record_references_from_text(case_data, original_text).get("refs", []))
    # A readable contract ID is authoritative; supplementary customer names in a
    # screenshot may be ambiguous or OCR-corrupted without blocking the contracts.
    has_resolved_contract = any(
        target.get("record", {}).get("record_type") == "contract"
        for targets in resolved.values()
        for target in targets
    )
    if has_resolved_contract:
        missing = [
            str(mention.get("text") or "")
            for mention in request.entity_mentions
            if str(mention.get("mention_id") or "") not in resolved
            and str(mention.get("kind") or "").lower() not in {"name", "customer"}
        ]
        unresolved_non_name = any(
            str(mention.get("mention_id") or "") not in resolved
            and str(mention.get("kind") or "").lower() not in {"name", "customer"}
            for mention in request.entity_mentions
        )
        if not unresolved_non_name:
            ambiguous = []
    if not resolved and not ambiguous and not latest_explicit_refs:
        active_targets = active_scope_records(case_data, active_investigation_scope)
        if len(active_targets) == 1:
            active_key = 'active'
            resolved[active_key] = active_targets
            missing = []
            for claim in request.issue_claims:
                claim['mention_id'] = active_key
        elif len(active_targets) > 1:
            ambiguous.append('multiple active investigation records')
            ambiguity_refs = [str(item.get('ref') or item.get('record', {}).get('record_id') or '') for item in active_targets]

    if original_text and not resolved:
        original_resolution = resolve_record_references_from_text(case_data, original_text)
        original_refs = normalize_ref_list(case_data, original_resolution.get("refs", []))
        recovered = []
        for ref in original_refs:
            record = case_data["contracts"].get(ref) or case_data["customers"].get(ref)
            if record:
                recovered.append({"ref": ref, "record": record})
        if recovered:
            resolved["original"] = recovered
            missing = []

    if original_text and missing:
        original_resolution = resolve_record_references_from_text(case_data, original_text)
        original_refs = normalize_ref_list(case_data, original_resolution.get("refs", []))
        unresolved_mentions = [str(mention.get("mention_id") or "") for mention in request.entity_mentions if str(mention.get("mention_id") or "") not in resolved]
        if original_refs and len(unresolved_mentions) == 1:
            recovered = []
            for ref in original_refs:
                record = case_data["contracts"].get(ref) or case_data["customers"].get(ref)
                if record:
                    recovered.append({"ref": ref, "record": record})
            if recovered:
                resolved[unresolved_mentions[0]] = recovered
                missing = [item for item in missing if item != str(next((m.get("text") for m in request.entity_mentions if str(m.get("mention_id") or "") == unresolved_mentions[0]), ""))]

    if not resolved and not ambiguous and missing:
        return {
            "status": "not_found",
            "clarification": "I cannot find that record in the case data. Check the spelling or give me its ID.",
            "records": [],
            "score_result": {},
            "approved_material": {},
        }
    if ambiguous or missing or not resolved:
        return {
            "status": "clarification",
            "clarification": None,
            "clarification_kind": "ambiguous_entity" if ambiguous else ("missing_entity" if request.issue_claims else "missing_entity_and_issue"),
            "clarification_candidates": ambiguity_refs,
            "records": [],
            "score_result": {},
            "approved_material": {},
        }
    if not request.issue_claims and (request.requested_content in {"overview", "identity", "asset_details", "vin", "date", "interest_rate", "exposure", "approval"} or (request.requested_content is None and request.context_action in {"merge", "replace"})):
        lookup_records = []
        for targets in resolved.values():
            for target in targets:
                record = target["record"]
                if record.get("record_type") == "customer":
                    record = dict(record)
                    contracts = contracts_for_customer(case_data, str(record.get("customer_id") or ""))
                    example_ids = [str(contract["record_id"]) for contract in contracts[:3]]
                    record["customer_contract_summary"] = {
                        "count": len(contracts),
                        "examples": example_ids,
                    }
                lookup_records.append(record)
        return {
            "status": "lookup",
            "requested_content": request.requested_content,
            "records": lookup_records,
            "clarification": None,
            "score_result": {},
            "approved_material": {},
        }

    claims: list[dict[str, Any]] = []
    expanded_records: list[dict[str, Any]] = []
    unresolved_claims: list[dict[str, str]] = []
    for claim in request.issue_claims:
        mention_id = str(claim.get("mention_id") or "")
        targets = resolved.get(mention_id, [])
        issue_key = resolve_issue_key(case_data, str(claim.get("candidate_issue") or ""))
        if not targets or not issue_key:
            continue
        issue_type = case_data["issue_by_key"][issue_key]
        claim_refs: list[str] = []
        for target in targets:
            target_ref = target["ref"]
            if target["record"].get("record_type") == "customer" and not issue_is_customer_scoped(case_data, issue_type):
                customer_contracts = contracts_for_customer(case_data, target_ref)
                if len(customer_contracts) == 1:
                    target_ref = str(customer_contracts[0]["record_id"])
                    expanded_records.append(customer_contracts[0])
                else:
                    unresolved_claims.append({"customer_id": target_ref, "issue_type": issue_type})
                    continue
            if target_ref not in claim_refs:
                claim_refs.append(target_ref)
        if claim_refs:
            claims.append({
                "record_refs": claim_refs,
                "issue_type": issue_type,
                "rationale": str(claim.get("rationale") or "Auditor raised this concern."),
            })

    response_records = [target["record"] for targets in resolved.values() for target in targets]
    response_records.extend(expanded_records)

    target_refs = []
    issue_types = []
    for claim in claims:
        for ref in claim["record_refs"]:
            if ref not in target_refs:
                target_refs.append(ref)
        if claim["issue_type"] not in issue_types:
            issue_types.append(claim["issue_type"])

    shared_issue_batch = (
        1 < len(target_refs) <= MAX_SHARED_ISSUE_BATCH_RECORDS
        and len(issue_types) == 1
        and all(case_data["contracts"].get(ref, {}).get("record_type") == "contract" for ref in target_refs)
    )
    if shared_issue_batch:
        claims = [{
            "record_refs": target_refs,
            "issue_type": issue_types[0],
            "rationale": claims[0]["rationale"],
        }]
    elif len(target_refs) > MAX_RECORDS:
        return {
            "status": "clarification",
            "clarification": "That is too broad for one investigation. Narrow it to the contracts or issues you want checked.",
            "records": [],
            "score_result": {},
            "approved_material": {},
        }
    if not claims:
        return {
            "status": "needs_contract_examples" if unresolved_claims else "clarification",
            "clarification": None,
            "clarification_kind": "missing_entity" if unresolved_claims else "missing_issue",
            "records": response_records,
            "score_result": {},
            "approved_material": {},
        }
    if len(claims) > MAX_CLAIMS:
        return {
            "status": "clarification",
            "clarification": "Narrow the investigation to ten concerns or fewer.",
            "records": [],
            "score_result": {},
            "approved_material": {},
        }

    score = update_score(
        case_data,
        ledger,
        [],
        claims[0]["issue_type"],
        claims=claims,
    )
    if unresolved_claims and not score.get("score_delta", 0):
        return {
            "status": "needs_contract_examples",
            "records": response_records,
            "clarification": "I cannot check that across the whole customer book. Give me the contract ID and the concern together.",
            "score_result": {
                "status": score.get("status"),
                "issue_type": score.get("issue_type"),
                "score_summary": score.get("score_summary", {}),
                "score_delta": score.get("score_delta", 0),
                "findings": score.get("findings", []),
            },
            "approved_material": {},
        }
    approved = []
    for finding in score.get("findings", []):
        material = finding.get("issue_material")
        if finding.get("status") in {"new_score", "repeat"} and material:
            approved.append({
                "issue_type": finding.get("issue_type"),
                "why_it_violates_policy": material.get("why_it_violates_policy", ""),
                "explanation_given_to_auditor": material.get("explanation_given_to_auditor", ""),
            })
    finding_rows = [
        {"record_id": f.get("record_id"), "contract_id": f.get("contract_id"), "customer_id": f.get("customer_id"), "customer_name": f.get("customer_name"), "issue_type": f.get("issue_type"), "status": f.get("status").lower()}
        for f in score.get("findings", [])
    ]
    finding_summary: dict[str, dict[str, Any]] = {}
    for finding in finding_rows:
        summary = finding_summary.setdefault(finding["status"], {"count": 0, "sample_record_ids": []})
        summary["count"] += 1
        if len(summary["sample_record_ids"]) < 5:
            summary["sample_record_ids"].append(finding["record_id"])
    verified_count = sum(
        1 for finding in finding_rows if finding["status"] in {"new_score", "repeat"}
    )
    not_verified_count = len(finding_rows) - verified_count
    if verified_count and not_verified_count:
        verification_classification = "mixed"
    elif verified_count:
        verification_classification = "all_verified"
    else:
        verification_classification = "none_verified"
    claim_outcome = {
        "classification": verification_classification,
        "verified_count": verified_count,
        "not_verified_count": not_verified_count,
    }
    if len(approved) <= MAX_FINDINGS_FOR_FULL_CONTEXT:
        approved_material = {"findings": approved} if approved else {}
    else:
        by_issue: dict[str, dict[str, Any]] = {}
        for item in approved:
            issue_type = str(item.get("issue_type") or "Finding")
            summary = by_issue.setdefault(issue_type, {"count": 0, "sample_record_ids": [], "explanation": item.get("explanation_given_to_auditor", "")})
            summary["count"] += 1
            record_id = str(item.get("record_id") or "")
            if record_id and record_id not in summary["sample_record_ids"] and len(summary["sample_record_ids"]) < 3:
                summary["sample_record_ids"].append(record_id)
        approved_material = {"verified_findings_summary": {"total": len(approved), "by_issue": by_issue}}
    return {
        "status": score.get("status", "unsupported"),
        "records": response_records,
        "clarification": "I cannot check the contract or asset points across the whole customer book. Give me the contract ID and the concern together." if unresolved_claims else None,
        "score_result": {
            "status": score.get("status"),
            "issue_type": score.get("issue_type"),
            "score_summary": score.get("score_summary", {}),
            "score_delta": score.get("score_delta", 0),
            "findings": finding_rows,
            "finding_summary": finding_summary,
            "claim_outcome": claim_outcome,
        },
        "approved_material": approved_material,
    }


def build_response_context(result: dict[str, Any]) -> ResponseContext:
    response_mode = str(result.get("status") or "clarification")
    records = []
    for record in result.get("records", []):
        lookup_content = result.get("requested_content") or "overview"
        public_overview = (
            f"{record.get('customer_name')} has "
            f"{record['customer_contract_summary']['count']} contracts on record. "
            f"Examples include {', '.join(record['customer_contract_summary']['examples'])}."
            if lookup_content in {"overview", "identity"}
            and record.get("record_type") == "customer"
            and record.get("customer_contract_summary")
            else build_spoken_identification(record)
        )
        item = {
            "record_id": record.get("record_id"),
            "contract_id": record.get("contract_id"),
            "customer_id": record.get("customer_id"),
            "customer_name": record.get("customer_name"),
            "spoken_identification": build_spoken_identification(record),
            "public_overview": public_overview,
        }
        if response_mode in {"lookup", "unsupported"}:
            item["public_narrative"] = record.get("public_narrative", "")
        records.append(item)
    raw_score_result = result.get("score_result", {})
    score_result = {
        key: raw_score_result[key]
        for key in ("status", "issue_type", "score_delta", "claim_outcome")
        if key in raw_score_result
    }
    context_records = records if response_mode in {"lookup", "unsupported"} else []
    return ResponseContext(
        response_mode=response_mode,
        requested_content=result.get("requested_content") if response_mode == "lookup" else None,
        records=context_records,
        public_material={},
        score_result=score_result,
        approved_material=result.get("approved_material", {}),
        clarification=result.get("clarification"),
        clarification_kind=result.get("clarification_kind"),
        clarification_candidates=result.get("clarification_candidates"),
    )

def generate_mikael_response(
    context: ResponseContext,
    model: str,
    chat_history: list[dict[str, Any]] | None = None,
    latest_message: str | None = None,
    visual_entity_text: str = "") -> str:
    load_env()
    client = get_openai_client()
    prompt = (Path(__file__).parent / "prompts" / "generator_prompt.md").read_text(encoding="utf-8")
    context_payload = {
        key: value
        for key, value in context.__dict__.items()
        if value not in ({}, None, [], "")
    }
    prompt += "\n\nResponseContext:\n" + json.dumps(context_payload, ensure_ascii=False)
    mood_by_mode = {
        "new_score": "Defensive / Cornered",
        "repeat": "Annoyed / Dismissive",
        "unsupported": "Guarded / Hesitant",
        "needs_contract_examples": "Annoyed / Dismissive",
        "clarification": "Annoyed / Dismissive",
        "not_found": "Guarded / Hesitant",
        "lookup": "Professional / Controlled",
        "small_talk": "Annoyed / Dismissive",
    }
    prompt += "\n\nRequired mood: [MOOD:" + mood_by_mode.get(context.response_mode, "Professional / Controlled") + "]"
    latest_input = latest_message or "(not provided)"
    if visual_entity_text:
        latest_input += "\n<Image attached>\nVisual extraction table - [screenshot attached]:\n" + visual_entity_text
    response = client.responses.create(
        model=GENERATOR_MODEL,
        instructions=prompt,
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": "Latest auditor message: " + latest_input},
            {
                "type": "input_text",
                "text": "Recent dialogue for conversational continuity only:\\n"
                + (format_previous_visible_dialogue(chat_history, latest_message or "", max_messages=4) or "(none)"),
            },
        ]}],
        text={
            "format": {
                "type": "json_schema",
                "name": "mikael_response",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "mood": {
                            "type": "string",
                            "enum": [
                                "Professional / Controlled",
                                "Guarded / Hesitant",
                                "Defensive / Cornered",
                                "Reluctant / Defeated",
                                "Annoyed / Dismissive",
                            ],
                        },
                        "speech": {"type": "string"},
                    },
                    "required": ["mood", "speech"],
                },
            }
        },
        tool_choice="none",
        max_output_tokens=500,
    )
    raw = response_text(response)
    if not raw:
        raise RuntimeError("Mikael generator returned no response.")
    try:
        value = json.loads(raw)
        mood = value["mood"]
        speech = str(value["speech"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Mikael generator returned invalid structured output.") from exc
    return f"[MOOD:{mood}]\n{speech}"

def run_investigation(
    message: str,
    case_data: dict[str, Any],
    ledger: dict[str, dict[str, Any]],
    model: str,
    image_path: str | None = None,
    image_data_urls: list[str] | None = None,
    chat_history: list[dict[str, Any]] | None = None,
    active_investigation_scope: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    visual_text_parts: list[str] = []
    visual_quality_parts: list[dict[str, Any]] = []
    request = parse_investigation_request(
        message,
        case_data,
        model,
        image_path,
        image_data_urls,
        chat_history,
        active_investigation_scope,
        visual_text_parts,
        visual_quality_parts,
    )
    updated_scope = apply_context_action_to_scope(
        request,
        case_data,
        active_investigation_scope,
    )

    explicit_refs = normalize_ref_list(
        case_data,
        resolve_record_references_from_text(case_data, message).get("refs", []),
    )
    active_targets = active_scope_records(case_data, updated_scope)
    if (
        request.follow_active_context
        and not explicit_refs
        and len(active_targets) > 1
        and request.requested_content == "explanation"
    ):
        request.issue_claims = []
    result = (
        {"status": "small_talk", "records": [], "score_result": {}, "approved_material": {}, "clarification": None}
        if request.small_talk and not request.issue_claims and not request.requested_content
        else verify_investigation_request(request, case_data, ledger, message, updated_scope, visual_quality_parts[0] if visual_quality_parts else None)
    )
    result["active_investigation_scope"] = updated_scope
    result["visual_extraction_text"] = "\n\n".join(visual_text_parts).strip()
    context = build_response_context(result)
    reply = generate_mikael_response(context, model, chat_history, message, "\\n\\n".join(visual_text_parts))
    result["request"] = request.__dict__
    result["response_context"] = context.__dict__
    result["reply"] = reply
    result["latency_seconds"] = round(time.perf_counter() - started, 3)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one isolated audit investigation.")
    parser.add_argument("message")
    parser.add_argument("--image")
    parser.add_argument("--model", default=os.getenv("AUDIT_RPG_MODEL", DEFAULT_MODEL))
    args = parser.parse_args()
    case_data = load_case_data()
    result = run_investigation(args.message, case_data, {}, args.model, args.image)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
