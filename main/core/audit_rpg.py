from __future__ import annotations

import base64
import copy
import json
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher, get_close_matches
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from openai import OpenAI


def response_request_options(model_name: str, force_final: bool) -> dict[str, Any]:
    options: dict[str, Any] = {"prompt_cache_key": PROMPT_CACHE_KEY}
    efforts = REASONING_EFFORT_BY_MODEL.get(model_name)
    if efforts:
        options["reasoning"] = {"effort": efforts["final" if force_final else "tool_selection"]}
    return options


def existing_case_path(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


DATA_PATH = existing_case_path(
    Path("data/llm_review_index.parquet"),
    Path("outputs/nordovia_real_data/llm_review_index.parquet"),
)
ENTITY_MASTER_PATH = existing_case_path(
    Path("data/entity_master.parquet"),
    Path("outputs/nordovia_real_data/entity_master.parquet"),
)
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
MODEL_ENV_VAR = "AUDIT_RPG_MODEL"
DEFAULT_MODEL = "gpt-5-mini"
MAX_TOOL_CALLS = 4
MAX_OUTPUT_TOKENS = 1600
MAX_OUTPUT_RETRY_TOKENS = 5000
MAX_OUTPUT_FINAL_RETRY_TOKENS = 10000
FINAL_CONTEXT_ENV_VAR = "AUDIT_RPG_FINAL_CONTEXT"
RECENT_HISTORY_LIMIT = 12
MAX_RECORDS_PER_SCORE_TURN = 5
MAX_SHARED_ISSUE_BATCH_RECORDS = 50
MAX_CLAIMS_PER_SCORE_TURN = 10
PROMPT_CACHE_KEY = "nordovia-audit-rpg-v1"
REASONING_EFFORT_BY_MODEL = {
    "gpt-5-mini": {"tool_selection": "low", "final": "minimal"},
}

MOOD_LABELS = {
    "Professional / Controlled",
    "Guarded / Hesitant",
    "Defensive / Cornered",
    "Reluctant / Defeated",
    "Annoyed / Dismissive",
}
MOOD_RE = re.compile(r"^\[MOOD:(Professional / Controlled|Guarded / Hesitant|Defensive / Cornered|Reluctant / Defeated|Annoyed / Dismissive)\]", re.MULTILINE)
MOOD_PREFIXES = {
    "professional": "Professional / Controlled",
    "guarded": "Guarded / Hesitant",
    "defensive": "Defensive / Cornered",
    "reluctant": "Reluctant / Defeated",
    "annoyed": "Annoyed / Dismissive",
}
INTERVIEW_STATE_BY_MOOD = {
    "Professional / Controlled": {"mood": "Controlled"},
    "Guarded / Hesitant": {"mood": "Guarded"},
    "Defensive / Cornered": {"mood": "Defensive"},
    "Reluctant / Defeated": {"mood": "Reluctant"},
    "Annoyed / Dismissive": {"mood": "Dismissive"},
}


def interview_state_for_mood(mood: str) -> dict[str, str]:
    return INTERVIEW_STATE_BY_MOOD.get(mood, INTERVIEW_STATE_BY_MOOD["Professional / Controlled"])


def extract_mood(reply: str) -> str:
    match = MOOD_RE.search(reply or "")
    return match.group(1) if match else "Professional / Controlled"


def normalize_mood_reply(reply: str) -> str:
    text = (reply or "").strip()
    if MOOD_RE.match(text):
        return text

    mood = "Professional / Controlled"
    if text.lower().startswith("[mood:"):
        first_line, _, rest = text.partition("\n")
        first_line_lower = first_line.lower()
        for marker, label in MOOD_PREFIXES.items():
            if marker in first_line_lower:
                mood = label
                break
        if "]" in first_line:
            text = first_line.split("]", 1)[1].strip()
            if rest:
                text = (text + "\n" + rest).strip()
        else:
            text = rest.strip()

    if not text:
        text = "I need a more concrete contract or customer reference."
    return f"[MOOD:{mood}]\n{text}"


PUBLIC_COLUMNS = {"NarrativeType", "CustomerID", "ContractID", "Issue", "PublicNarrative", "SecretNarrative", "AnomalyTags"}
CONTRACT_RE = re.compile(r"\bSE\d{6}\b", re.IGNORECASE)
CUSTOMER_RE = re.compile(r"\bCUST\d{4}\b", re.IGNORECASE)
ASSET_RE = re.compile(r"\bAST\d{6}\b", re.IGNORECASE)
VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    record_id: str
    issue_type: str
    status: str
    reason: str

    @property
    def key(self) -> str:
        return f"{self.record_id}::{normalize_key(self.issue_type)}"


def load_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@lru_cache(maxsize=1)
def get_openai_client() -> OpenAI:
    return OpenAI()


def normalize_key(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    return re.sub(r"_+", "_", text)


def parse_customer_name(public_narrative: str) -> str:
    match = re.search(r"Customer\s+CUST\d{4},\s*(.*?),\s*operates in", public_narrative)
    return match.group(1).strip() if match else ""


def parse_secret_material(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    items: list[dict[str, str]] = []
    for item in parsed:
        if isinstance(item, dict):
            issue_type = str(item.get("Issue type", "")).strip()
            if issue_type:
                items.append(
                    {
                        "issue_type": issue_type,
                        "why_it_violates_policy": str(item.get("Why it violates policy", "")).strip(),
                        "explanation_given_to_auditor": str(item.get("Explanation given to auditor", "")).strip(),
                    }
                )
    return items

def build_brief_overview(record: dict[str, Any]) -> str:
    if record["record_type"] == "contract":
        narrative = record["public_narrative"]
        contract_type = "contract"
        value = ""
        type_match = re.search(r" is (.*?) of (SEK [0-9.]+m)", narrative)
        if type_match:
            contract_type = type_match.group(1)
            value = type_match.group(2)
        assets = ""
        asset_match = re.search(r"Assets: (.*?);", narrative)
        if asset_match:
            assets = asset_match.group(1)
        parts = [f"{record['record_id']} is {record['customer_name']}"]
        contract_part = contract_type
        if value:
            contract_part += f", {value}"
        parts.append(contract_part)
        if assets:
            parts.append(f"assets: {assets}")
        return "; ".join(parts) + "."

    narrative = record["public_narrative"]
    business = ""
    business_match = re.search(r"operates in (.*?), primarily as (.*?)\.", narrative)
    if business_match:
        business = f"; {business_match.group(1)}, {business_match.group(2)}"
    return f"{record['customer_id']} is {record['customer_name']}{business}."


def parse_asset_counts(public_narrative: str) -> list[str]:
    match = re.search(r"Assets: (.*?);", public_narrative)
    if not match:
        return []
    asset_types: list[str] = []
    for part in match.group(1).split(","):
        item = part.strip()
        count_match = re.match(r"(\d+)\s+(.+)", item)
        if not count_match:
            asset_types.append(item)
            continue
        count = int(count_match.group(1))
        name = count_match.group(2).strip()
        singular = re.sub(r"s$", "", name)
        asset_types.extend([singular] * count)
    return asset_types


def parse_brand_list(public_narrative: str) -> list[str]:
    match = re.search(r"brands? (.*?);", public_narrative, re.IGNORECASE)
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def build_asset_details(public_narrative: str, asset_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    asset_types = parse_asset_counts(public_narrative)
    brands = parse_brand_list(public_narrative)
    details: list[dict[str, str]] = []
    for index, row in enumerate(asset_rows):
        details.append(
            {
                "asset_id": row.get("asset_id", ""),
                "vin": row.get("vin", ""),
                "asset_type": asset_types[index] if index < len(asset_types) else "",
                "brand": brands[index] if index < len(brands) else "",
            }
        )
    return details


def load_entity_master(path: Path = ENTITY_MASTER_PATH) -> dict[str, Any]:
    empty = {"by_contract": {}, "asset_to_contract": {}, "vin_to_contract": {}, "customer_names": {}}
    if not path.exists():
        return empty
    df = pd.read_parquet(path)
    by_contract: dict[str, list[dict[str, str]]] = {}
    asset_to_contract: dict[str, str] = {}
    vin_to_contract: dict[str, str] = {}
    customer_names: dict[str, str] = {}
    for row in df.to_dict("records"):
        row_lower = {str(key).lower(): value for key, value in row.items()}
        contract_id = str(row_lower.get("contract_id") or "").upper().strip()
        customer_id = str(row_lower.get("customer_id") or "").upper().strip()
        customer_name = str(row_lower.get("customer_name") or "").strip()
        asset_id = str(row_lower.get("asset_id") or "").upper().strip()
        vin = normalize_compact_id(str(row_lower.get("vin") or ""))
        if customer_id and customer_name:
            customer_names[customer_id] = customer_name
        if contract_id:
            by_contract.setdefault(contract_id, []).append({"asset_id": asset_id, "vin": vin})
        if asset_id and contract_id:
            asset_to_contract[asset_id] = contract_id
        if vin and contract_id:
            vin_to_contract[vin] = contract_id
    return {"by_contract": by_contract, "asset_to_contract": asset_to_contract, "vin_to_contract": vin_to_contract, "customer_names": customer_names}


def load_case_data(path: Path = DATA_PATH, entity_path: Path = ENTITY_MASTER_PATH) -> dict[str, Any]:
    df = pd.read_parquet(path)
    entity_master = load_entity_master(entity_path)
    issue_columns = list(dict.fromkeys(str(col).strip() for col in df.columns if col not in PUBLIC_COLUMNS and str(col).strip()))
    issue_by_key = {normalize_key(col): col for col in issue_columns}
    issue_catalog_text = "\n".join(f"- {issue}" for issue in issue_columns)

    customer_rows = df[df["NarrativeType"] == "Customer"].copy()
    customer_names = {
        str(row.CustomerID): parse_customer_name(str(row.PublicNarrative))
        for row in customer_rows.itertuples(index=False)
    }
    customer_names.update({k: v for k, v in entity_master["customer_names"].items() if v})

    contracts: dict[str, dict[str, Any]] = {}
    customers: dict[str, dict[str, Any]] = {}

    for row_dict in df.to_dict("records"):
        customer_id = str(row_dict["CustomerID"])
        contract_id = str(row_dict["ContractID"] or "").strip()
        record_id = contract_id if contract_id else customer_id
        issue_material = parse_secret_material(row_dict["SecretNarrative"])
        asset_rows = entity_master["by_contract"].get(contract_id.upper(), []) if contract_id else []
        asset_details = build_asset_details(str(row_dict["PublicNarrative"]), asset_rows)
        record = {
            "record_id": record_id,
            "record_type": str(row_dict["NarrativeType"]).lower(),
            "contract_id": contract_id,
            "customer_id": customer_id,
            "customer_name": customer_names.get(customer_id, ""),
            "public_narrative": str(row_dict["PublicNarrative"]),
            "issue_present": bool(row_dict["Issue"]),
            "issue_types": [item["issue_type"] for item in issue_material],
            "issue_material": issue_material,
            "asset_details": asset_details,
            "asset_ids": [item["asset_id"] for item in asset_details if item.get("asset_id")],
            "vins": [item["vin"] for item in asset_details if item.get("vin")],
            "brand_summary": ", ".join(parse_brand_list(str(row_dict["PublicNarrative"]))) or "",
        }
        for issue_key, column_name in issue_by_key.items():
            record[issue_key] = bool(row_dict[column_name])
        if contract_id:
            contracts[contract_id.upper()] = record
        else:
            customers[customer_id.upper()] = record

    customer_name_candidates = []
    for customer_id, customer_name in customer_names.items():
        normalized_name = normalize_customer_name_for_match(customer_name)
        if normalized_name:
            customer_name_candidates.append((customer_id.upper(), customer_name, normalized_name, normalized_name.split()))

    return {
        "contracts": contracts,
        "customers": customers,
        "customer_names": customer_names,
        "customer_name_candidates": customer_name_candidates,
        "asset_to_contract": entity_master["asset_to_contract"],
        "vin_to_contract": entity_master["vin_to_contract"],
        "issue_columns": issue_columns,
        "issue_catalog_text": issue_catalog_text,
        "issue_by_key": issue_by_key,
        "issue_keys": sorted(issue_by_key),
    }


NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def pluralize_asset_type(asset_type: str, count: int) -> str:
    if count == 1:
        return asset_type
    irregular = {"bus": "buses"}
    return irregular.get(asset_type, asset_type if asset_type.endswith("s") else f"{asset_type}s")


def format_asset_mix(asset_types: list[str]) -> str:
    counts: dict[str, int] = {}
    for asset_type in asset_types:
        if asset_type:
            counts[asset_type] = counts.get(asset_type, 0) + 1
    parts = []
    for asset_type, count in counts.items():
        count_text = NUMBER_WORDS.get(count, str(count))
        parts.append(f"{count_text} {pluralize_asset_type(asset_type, count)}")
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return parts[0] if parts else ""


def build_asset_mix_summary(record: dict[str, Any]) -> str:
    asset_types = [item.get("asset_type", "") for item in record.get("asset_details", [])]
    if not asset_types:
        asset_types = parse_asset_counts(record.get("public_narrative", ""))
    return format_asset_mix(asset_types)


def short_brand_summary(value: str | None, max_brands: int = 3) -> str:
    brands = [part.strip() for part in str(value or "").split(",") if part.strip()]
    if len(brands) > max_brands:
        return ""
    return ", ".join(brands)


def build_spoken_identification(record: dict[str, Any]) -> str:
    asset_summary = build_asset_mix_summary(record)
    brands = short_brand_summary(record.get("brand_summary", ""))
    asset_phrase = asset_summary
    if asset_phrase and brands:
        asset_phrase = f"{asset_phrase}, {brands}"

    if record["record_type"] == "contract":
        if asset_phrase:
            return f"{record['record_id']} is {record['customer_name']}, {asset_phrase}."
        return f"{record['record_id']} is {record['customer_name']}."

    return f"{record['customer_id']} is {record['customer_name']}."


def compact_record(record: dict[str, Any], include_public: bool = True) -> dict[str, Any]:
    result = {
        "record_id": record["record_id"],
        "record_type": record["record_type"],
        "contract_id": record["contract_id"],
        "customer_id": record["customer_id"],
        "customer_name": record["customer_name"],
        "brief_overview": build_brief_overview(record),
        "spoken_identification": build_spoken_identification(record),
        "asset_mix_summary": build_asset_mix_summary(record),
        "asset_ids": record.get("asset_ids", []),
        "vins": record.get("vins", []),
        "brand_summary": record.get("brand_summary", ""),
    }
    if include_public:
        result["public_narrative"] = record["public_narrative"]
    return result


def resolve_issue_key(case_data: dict[str, Any], issue_type: str) -> str | None:
    issue_key = normalize_key(issue_type)
    if issue_key in case_data["issue_by_key"]:
        return issue_key

    compact_keys = {key.replace("_", ""): key for key in case_data["issue_keys"]}
    compact_issue = issue_key.replace("_", "")
    if compact_issue in compact_keys:
        return compact_keys[compact_issue]

    matches = get_close_matches(issue_key, case_data["issue_keys"], n=1, cutoff=0.70)
    return matches[0] if matches else None



def normalize_compact_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def normalize_customer_name_for_match(value: str) -> str:
    text = str(value or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    suffixes = {"ltd", "limited", "ab", "gmbh", "as", "oy", "inc", "llc", "plc", "co", "company"}
    tokens = [token for token in text.split() if token not in suffixes and token != "and"]
    return " ".join(tokens)


def meaningful_name_tokens(value: str) -> list[str]:
    stopwords = {
        "what", "about", "look", "looks", "seems", "seem", "talk", "lets", "let", "can", "we", "the",
        "this", "that", "these", "those", "customer", "customers", "contract", "contracts", "asset", "assets",
        "with", "for", "and", "or", "to", "of", "in", "on", "issue", "problem", "risk", "approval", "approved",
        "price", "pricing", "inflated", "overpriced", "over", "under", "rate", "interest", "financed", "amount",
        "finance", "vehicle", "vehicles", "passenger", "commercial", "normal", "car", "cars", "private", "policy",
        "policies", "violation", "violate", "violates", "breach", "clause", "proof", "evidence", "document", "memo",
        "narrative", "note", "weak", "thin", "missing", "generic", "default", "overdue", "arrears", "dpd", "past",
        "due", "aml", "kyc", "pep", "sanctions", "owner", "beneficial", "role", "title", "authority", "mandate",
        "matrix", "down", "payment", "deposit", "collateral", "mv", "curve", "residual", "market", "value",
        "snapshot", "reconcile", "recovered", "cleared", "hard", "start", "date", "after", "before", "low",
        "high", "zero", "similar", "models", "model", "kind", "type", "attached", "compare", "check", "review",
        "have", "has", "had", "having", "are", "is", "was", "were", "no", "not", "tax", "haven", "havens",
        "approvals", "narratives", "notes", "memos", "rationales", "justifications",
    }
    return [token for token in normalize_customer_name_for_match(value).split() if token not in stopwords and len(token) >= 3]


def should_match_customer_names(text: str, has_resolved_id_events: bool) -> bool:
    tokens = meaningful_name_tokens(text)
    if len(tokens) < 2:
        return False
    if has_resolved_id_events:
        return len(tokens) <= 8
    return len(tokens) <= 10

def short_context(text: str, start: int, end: int, width: int = 18) -> str:
    return text[max(0, start - width): min(len(text), end + width)]


def add_unique_ref(refs: list[str], ref: str) -> None:
    if ref and ref not in refs:
        refs.append(ref)


def record_candidate(events: list[dict[str, Any]], start: int, kind: str, value: str, raw: str) -> None:
    events.append({"start": start, "kind": kind, "value": value, "raw": raw})


def valid_contract_id(case_data: dict[str, Any], digits: str) -> str | None:
    contract_id = f"SE{str(digits).zfill(6)}"
    return contract_id if contract_id in case_data["contracts"] else None


def valid_customer_id(case_data: dict[str, Any], digits: str) -> str | None:
    customer_id = f"CUST{str(digits).zfill(4)}"
    return customer_id if customer_id in case_data["customers"] else None


def valid_asset_id(case_data: dict[str, Any], digits: str) -> tuple[str, str] | None:
    asset_id = f"AST{str(digits).zfill(6)}"
    contract_id = case_data.get("asset_to_contract", {}).get(asset_id)
    return (asset_id, contract_id) if contract_id else None


def vin_suffix_matches(case_data: dict[str, Any], suffix: str) -> list[tuple[str, str]]:
    normalized = normalize_compact_id(suffix)
    if len(normalized) < 4:
        return []
    return [(vin, contract_id) for vin, contract_id in case_data.get("vin_to_contract", {}).items() if vin.endswith(normalized)]


def unsafe_bare_number_context(text: str, start: int, end: int) -> bool:
    context = short_context(text, start, end).lower()
    return bool(
        re.search(r"\b(sek|eur|amount|exposure|value|price|rate|percent|percentage|days?|dpd|contracts?|assets?|vin|ending|suffix|last)\b", context)
        or re.search(r"[%./-]", text[max(0, start - 2): min(len(text), end + 2)])
    )


def match_customer_names(case_data: dict[str, Any], text: str) -> list[dict[str, Any]]:
    text_tokens = meaningful_name_tokens(text)
    if not text_tokens:
        return []

    customers = case_data.get("customer_name_candidates") or []
    candidate_tokens = {
        token
        for _customer_id, _customer_name, _normalized_name, name_tokens in customers
        for token in name_tokens
    }
    if candidate_tokens and not any(token in candidate_tokens for token in text_tokens):
        return []
    if not customers:
        customers = []
        for customer_id, customer_name in case_data.get("customer_names", {}).items():
            normalized_name = normalize_customer_name_for_match(customer_name)
            if not normalized_name:
                continue
            customers.append((customer_id.upper(), customer_name, normalized_name, normalized_name.split()))

    matches_by_phrase: dict[str, list[tuple[str, str]]] = {}
    for n in range(min(6, len(text_tokens)), 1, -1):
        for index in range(0, len(text_tokens) - n + 1):
            phrase_tokens = text_tokens[index:index + n]
            phrase = " ".join(phrase_tokens)
            if len(phrase.replace(" ", "")) < 6:
                continue
            exact_matches = [
                (customer_id, customer_name)
                for customer_id, customer_name, normalized_name, _name_tokens in customers
                if phrase in normalized_name
            ]
            if exact_matches:
                matches_by_phrase.setdefault(phrase, []).extend(exact_matches)
                continue
            for customer_id, customer_name, _normalized_name, name_tokens in customers:
                windows = [" ".join(name_tokens[i:i + n]) for i in range(0, max(1, len(name_tokens) - n + 1))]
                if n >= 2 and any(SequenceMatcher(None, phrase, window).ratio() >= 0.88 for window in windows):
                    matches_by_phrase.setdefault(phrase, []).append((customer_id, customer_name))

    results: list[dict[str, Any]] = []
    seen_customers: set[str] = set()
    seen_ambiguous: set[str] = set()
    for phrase, matches in matches_by_phrase.items():
        unique_matches = []
        seen_match_ids: set[str] = set()
        for customer_id, customer_name in matches:
            if customer_id not in seen_match_ids:
                seen_match_ids.add(customer_id)
                unique_matches.append((customer_id, customer_name))
        if len(unique_matches) == 1:
            customer_id, customer_name = unique_matches[0]
            if customer_id not in seen_customers:
                seen_customers.add(customer_id)
                results.append({"input": phrase, "customer_id": customer_id, "customer_name": customer_name})
        elif phrase not in seen_ambiguous:
            seen_ambiguous.add(phrase)
            results.append(
                {
                    "input": phrase,
                    "ambiguous": [
                        {"customer_id": customer_id, "customer_name": customer_name}
                        for customer_id, customer_name in unique_matches[:6]
                    ],
                }
            )
    return results

def resolve_record_references_from_text(
    case_data: dict[str, Any],
    text: str,
    include_customer_names: bool = True,
) -> dict[str, Any]:
    source = str(text or "")
    events: list[dict[str, Any]] = []

    for match in re.finditer(r"\bS\s*E[\s_-]*(\d{6})\b", source, re.IGNORECASE):
        contract_id = valid_contract_id(case_data, match.group(1))
        record_candidate(events, match.start(), "contract", contract_id or f"SE{match.group(1)}", match.group(0))

    for match in re.finditer(r"\b(?:contract|ctr|agreement|lease)\s*(?:id)?\s*[:#-]?\s*(\d{6})\b", source, re.IGNORECASE):
        contract_id = valid_contract_id(case_data, match.group(1))
        record_candidate(events, match.start(), "contract", contract_id or f"SE{match.group(1)}", match.group(0))

    for match in re.finditer(r"\bC\s*U\s*S\s*T[\s_-]*(\d{1,4})\b", source, re.IGNORECASE):
        customer_id = valid_customer_id(case_data, match.group(1))
        record_candidate(events, match.start(), "customer", customer_id or f"CUST{str(match.group(1)).zfill(4)}", match.group(0))

    for match in re.finditer(r"\b(?:customer|cust|client)\s*(?:id)?\s*[:#-]?\s*(\d{1,4})\b", source, re.IGNORECASE):
        customer_id = valid_customer_id(case_data, match.group(1))
        record_candidate(events, match.start(), "customer", customer_id or f"CUST{str(match.group(1)).zfill(4)}", match.group(0))

    for match in re.finditer(r"\bA\s*S\s*T[\s_-]*(\d{6})\b", source, re.IGNORECASE):
        asset_match = valid_asset_id(case_data, match.group(1))
        value = asset_match[0] if asset_match else f"AST{match.group(1)}"
        record_candidate(events, match.start(), "asset", value, match.group(0))

    for match in re.finditer(r"\b(?:asset(?:\s*id)?|asset_id|equipment|unit)\s*[:#-]?\s*(\d{6})\b", source, re.IGNORECASE):
        asset_match = valid_asset_id(case_data, match.group(1))
        value = asset_match[0] if asset_match else f"AST{match.group(1)}"
        record_candidate(events, match.start(), "asset", value, match.group(0))

    for match in re.finditer(r"\b[A-HJ-NPR-Z0-9]{17}\b", source, re.IGNORECASE):
        record_candidate(events, match.start(), "vin", normalize_compact_id(match.group(0)), match.group(0))

    for match in re.finditer(r"\bvin\s*(?:id|number|no)?\s*[:#-]?\s*([A-HJ-NPR-Z0-9][A-HJ-NPR-Z0-9\s_-]{10,40}[A-HJ-NPR-Z0-9])", source, re.IGNORECASE):
        vin = normalize_compact_id(match.group(1))
        if len(vin) == 17:
            record_candidate(events, match.start(), "vin", vin, match.group(0))

    for match in re.finditer(r"\b(?:vin\s+ending|vin\s+suffix|ending|last\s+\d+\s+(?:is\s+)?)[:#-]?\s*([A-HJ-NPR-Z0-9]{4,10})\b", source, re.IGNORECASE):
        record_candidate(events, match.start(), "vin_suffix", normalize_compact_id(match.group(1)), match.group(0))

    for match in re.finditer(r"\b(\d{6})\b", source):
        if unsafe_bare_number_context(source, match.start(), match.end()):
            continue
        contract_id = valid_contract_id(case_data, match.group(1))
        asset_match = valid_asset_id(case_data, match.group(1))
        if contract_id and not asset_match:
            record_candidate(events, match.start(), "contract", contract_id, match.group(0))
        elif asset_match and not contract_id:
            record_candidate(events, match.start(), "asset", asset_match[0], match.group(0))
        elif contract_id and asset_match:
            record_candidate(events, match.start(), "ambiguous_number", match.group(1), match.group(0))

    has_id_events = bool(events)
    if include_customer_names and should_match_customer_names(source, has_id_events):
        name_matches = match_customer_names(case_data, source)
    else:
        name_matches = []

    for name_match in name_matches:
        if "ambiguous" in name_match:
            record_candidate(events, source.lower().find(str(name_match["input"]).lower()), "ambiguous_name", name_match, str(name_match["input"]))
        else:
            record_candidate(events, source.lower().find(str(name_match["input"]).lower()), "customer_name", name_match, str(name_match["input"]))

    refs: list[str] = []
    contracts: list[str] = []
    customers: list[str] = []
    resolved_from_assets: list[dict[str, str]] = []
    resolved_from_vins: list[dict[str, str]] = []
    resolved_from_names: list[dict[str, str]] = []
    unmatched: list[str] = []
    ambiguous: list[dict[str, Any]] = []

    for event in sorted(events, key=lambda item: max(0, int(item.get("start", 0)))):
        kind = event["kind"]
        value = event["value"]
        raw = str(event.get("raw") or value)
        if kind == "contract":
            if value in case_data["contracts"]:
                add_unique_ref(refs, value)
                add_unique_ref(contracts, value)
            elif value not in unmatched:
                unmatched.append(value)
        elif kind == "customer":
            if value in case_data["customers"]:
                add_unique_ref(refs, value)
                add_unique_ref(customers, value)
            elif value not in unmatched:
                unmatched.append(value)
        elif kind == "asset":
            contract_id = case_data.get("asset_to_contract", {}).get(value)
            if contract_id:
                add_unique_ref(refs, contract_id)
                add_unique_ref(contracts, contract_id)
                if not any(item.get("asset_id") == value for item in resolved_from_assets):
                    resolved_from_assets.append({"input": raw, "asset_id": value, "contract_id": contract_id})
            elif value not in unmatched:
                unmatched.append(value)
        elif kind == "vin":
            contract_id = case_data.get("vin_to_contract", {}).get(value)
            if contract_id:
                add_unique_ref(refs, contract_id)
                add_unique_ref(contracts, contract_id)
                if not any(item.get("vin") == value for item in resolved_from_vins):
                    resolved_from_vins.append({"input": raw, "vin": value, "contract_id": contract_id})
            elif value not in unmatched:
                unmatched.append(value)
        elif kind == "vin_suffix":
            matches = vin_suffix_matches(case_data, value)
            if len(matches) == 1:
                vin, contract_id = matches[0]
                add_unique_ref(refs, contract_id)
                add_unique_ref(contracts, contract_id)
                resolved_from_vins.append({"input": raw, "vin_suffix": value, "vin": vin, "contract_id": contract_id})
            elif len(matches) > 1:
                ambiguous.append({"input": raw, "type": "vin_suffix", "candidates": [{"vin": vin, "contract_id": contract_id} for vin, contract_id in matches[:6]]})
            elif value not in unmatched:
                unmatched.append(value)
        elif kind == "customer_name":
            customer_id = value["customer_id"]
            add_unique_ref(refs, customer_id)
            add_unique_ref(customers, customer_id)
            if not any(item.get("customer_id") == customer_id for item in resolved_from_names):
                resolved_from_names.append(value)
        elif kind == "ambiguous_name":
            ambiguous.append({"input": raw, "type": "customer_name", "candidates": value["ambiguous"]})
        elif kind == "ambiguous_number":
            ambiguous.append({"input": raw, "type": "number", "candidates": [f"SE{value}", f"AST{value}"]})

    return {
        "refs": refs,
        "contracts": contracts,
        "customers": customers,
        "resolved_from_assets": resolved_from_assets,
        "resolved_from_vins": resolved_from_vins,
        "resolved_from_names": resolved_from_names,
        "unmatched": unmatched,
        "ambiguous": ambiguous,
    }


def extract_reference_terms(*parts: Any) -> dict[str, list[str]]:
    text = " ".join(str(part or "") for part in parts)
    contract_ids = []
    customer_ids = []
    asset_ids = []
    vins = []
    for match in re.finditer(r"\bS\s*E[\s_-]*(\d{6})\b", text, re.IGNORECASE):
        add_unique_ref(contract_ids, f"SE{match.group(1)}")
    for match in re.finditer(r"\b(?:contract|ctr|agreement|lease)\s*(?:id)?\s*[:#-]?\s*(\d{6})\b", text, re.IGNORECASE):
        add_unique_ref(contract_ids, f"SE{match.group(1)}")
    for match in re.finditer(r"\bC\s*U\s*S\s*T[\s_-]*(\d{1,4})\b", text, re.IGNORECASE):
        add_unique_ref(customer_ids, f"CUST{str(match.group(1)).zfill(4)}")
    for match in re.finditer(r"\b(?:customer|cust|client)\s*(?:id)?\s*[:#-]?\s*(\d{1,4})\b", text, re.IGNORECASE):
        add_unique_ref(customer_ids, f"CUST{str(match.group(1)).zfill(4)}")
    for match in re.finditer(r"\bA\s*S\s*T[\s_-]*(\d{6})\b", text, re.IGNORECASE):
        add_unique_ref(asset_ids, f"AST{match.group(1)}")
    for match in re.finditer(r"\b(?:asset(?:\s*id)?|asset_id|equipment|unit)\s*[:#-]?\s*(\d{6})\b", text, re.IGNORECASE):
        add_unique_ref(asset_ids, f"AST{match.group(1)}")
    for match in re.finditer(r"\b[A-HJ-NPR-Z0-9]{17}\b", text, re.IGNORECASE):
        add_unique_ref(vins, normalize_compact_id(match.group(0)))
    return {"contract_ids": contract_ids, "customer_ids": customer_ids, "asset_ids": asset_ids, "vins": vins}


def find_records(
    case_data: dict[str, Any],
    query: str = "",
    contract_ids: list[str] | None = None,
    customer_ids: list[str] | None = None,
    customer_names: list[str] | None = None,
    asset_ids: list[str] | None = None,
    vins: list[str] | None = None,
    screenshot_refs: list[str] | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    resolver_parts = [query, " ".join(screenshot_refs or [])]
    resolver_parts.extend(f"contract {item}" for item in contract_ids or [])
    resolver_parts.extend(f"customer {item}" for item in customer_ids or [])
    resolver_parts.extend(f"asset {item}" for item in asset_ids or [])
    resolver_parts.extend(f"vin {item}" for item in vins or [])
    resolver_parts.extend(str(name) for name in customer_names or [])
    resolution = resolve_record_references_from_text(case_data, "\n".join(part for part in resolver_parts if str(part).strip()))

    matched_contracts: dict[str, dict[str, Any]] = {}
    matched_customers: dict[str, dict[str, Any]] = {}
    for ref in resolution["refs"]:
        if ref in case_data["contracts"]:
            matched_contracts[ref] = case_data["contracts"][ref]
        elif ref in case_data["customers"]:
            matched_customers[ref] = case_data["customers"][ref]

    customer_contracts: list[dict[str, Any]] = []
    if matched_customers and not matched_contracts:
        customer_id_set = set(matched_customers)
        for record in case_data["contracts"].values():
            if record["customer_id"].upper() in customer_id_set:
                customer_contracts.append(compact_record(record, include_public=False))
                if len(customer_contracts) >= limit:
                    break

    return {
        "contracts": [compact_record(record, include_public=False) for record in matched_contracts.values()],
        "customers": [compact_record(record, include_public=False) for record in matched_customers.values()],
        "customer_contracts_sample": customer_contracts,
        "customer_contracts_total": sum(
            1 for record in case_data["contracts"].values() if record["customer_id"].upper() in set(matched_customers)
        ) if matched_customers else 0,
        "primary_refs": resolution["refs"],
        "resolved_from_assets": resolution["resolved_from_assets"],
        "resolved_from_vins": resolution["resolved_from_vins"],
        "resolved_from_names": resolution["resolved_from_names"],
        "unmatched": resolution["unmatched"],
        "ambiguous": resolution["ambiguous"],
        "note": "Lookup resolution only. Do not infer approval quality, compliance, anomalies, missing issues, policy status, or evidence from this output. Asset IDs and VINs resolve to their contract first.",
    }

def get_records_from_refs(case_data: dict[str, Any], record_refs: list[str]) -> list[dict[str, Any]]:
    found = find_records(case_data, query=" ".join(record_refs))
    records: list[dict[str, Any]] = []
    for item in found["contracts"]:
        record = case_data["contracts"].get(item["contract_id"].upper())
        if record:
            records.append(record)
    if not records:
        for item in found["customers"]:
            record = case_data["customers"].get(item["customer_id"].upper())
            if record:
                records.append(record)
    return records


def get_case_material(case_data: dict[str, Any], record_refs: list[str], issue_type: str | None = None) -> dict[str, Any]:
    records = get_records_from_refs(case_data, record_refs)
    issue_key = resolve_issue_key(case_data, issue_type or "") if issue_type else None
    materials = []
    for record in records:
        item = compact_record(record)
        if issue_key:
            material = next(
                (entry for entry in record["issue_material"] if normalize_key(entry["issue_type"]) == issue_key),
                None,
            )
            if material:
                item["issue_material"] = material
            else:
                item["issue_material"] = None
        materials.append(item)
    return {
        "records": materials,
        "issue_type_resolved": case_data["issue_by_key"].get(issue_key) if issue_key else None,
        "policy_material_included": bool(issue_key),
        "note": "No hidden issue list is returned. Ask for a specific record and issue candidate to get policy or explanation material.",
    }


def check_score(
    case_data: dict[str, Any],
    ledger: dict[str, dict[str, Any]],
    record_refs: list[str],
    issue_type: str,
    rationale: str = "",
) -> dict[str, Any]:
    issue_key = resolve_issue_key(case_data, issue_type)
    if not issue_key:
        return {
            "status": "unsupported",
            "issue_type": issue_type,
            "findings": [],
            "reason": "Issue type was not recognized.",
        }

    records = get_records_from_refs(case_data, record_refs)
    findings: list[dict[str, Any]] = []
    for record in records:
        record_id = record["record_id"]
        truth = bool(record.get(issue_key))
        key = f"{record_id}::{issue_key}"
        if truth and key in ledger:
            status = "repeat"
            reason = "Finding already scored."
        elif truth:
            status = "new_score"
            reason = "Finding matches the record truth table."
        else:
            status = "unsupported"
            reason = "This issue is not true for that record in the case table."
        issue_assets = issue_asset_scope(record, issue_key)
        finding = {
            "record_id": record_id,
            "record_type": record["record_type"],
            "contract_id": record["contract_id"],
            "customer_id": record["customer_id"],
            "customer_name": record["customer_name"],
            "issue_type": case_data["issue_by_key"][issue_key],
            "issue_key": issue_key,
            "status": status,
            "score_delta": 1 if status == "new_score" else 0,
            "reason": reason,
            "rationale": rationale,
            "asset_ids": record.get("asset_ids", []),
            "vins": record.get("vins", []),
            "brand_summary": record.get("brand_summary", ""),
            "issue_asset_ids": [item["asset_id"] for item in issue_assets if item.get("asset_id")],
            "issue_brand_summary": ", ".join(unique_ordered([item.get("brand", "") for item in issue_assets if item.get("brand")])) or record.get("brand_summary", ""),
        }
        if truth:
            material = next(
                (entry for entry in record["issue_material"] if normalize_key(entry["issue_type"]) == issue_key),
                None,
            )
            if material:
                finding["issue_material"] = material
        findings.append(finding)

    if not findings:
        return {
            "status": "unsupported",
            "issue_type": case_data["issue_by_key"][issue_key],
            "findings": [],
            "reason": "No supported record reference was found.",
        }

    total_delta = sum(item["score_delta"] for item in findings)
    return {
        "status": "new_score" if total_delta else "repeat" if any(item["status"] == "repeat" for item in findings) else "unsupported",
        "issue_type": case_data["issue_by_key"][issue_key],
        "findings": findings,
        "score_delta": total_delta,
    }


def unique_ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def issue_asset_scope(record: dict[str, Any], issue_key: str) -> list[dict[str, str]]:
    details = record.get("asset_details", [])
    if issue_key == normalize_key("NON COMMERCIAL VEHICLE RELATED ASSETS"):
        targeted = [
            item for item in details
            if re.search(r"passenger|private|car|non[-\s]?commercial", item.get("asset_type", ""), re.IGNORECASE)
        ]
        return targeted or details
    return details


def count_records(findings: list[dict[str, Any]]) -> dict[str, int]:
    contract_ids = {item["contract_id"] for item in findings if item.get("contract_id")}
    customer_ids = {item["customer_id"] for item in findings if item.get("customer_id")}
    return {"contract_count": len(contract_ids), "customer_count": len(customer_ids)}


def score_one_claim(
    case_data: dict[str, Any],
    ledger: dict[str, dict[str, Any]],
    record_refs: list[str],
    issue_type: str,
    rationale: str = "",
) -> dict[str, Any]:
    if len(unique_ordered([str(ref).upper() for ref in record_refs or []])) > MAX_RECORDS_PER_SCORE_TURN and not shared_issue_batch_allowed(case_data, record_refs, [issue_type]):
        return {
            "status": "needs_narrowing",
            "issue_type": issue_type,
            "findings": [],
            "score_delta": 0,
            "reason": "Too many records for one live-meeting score check.",
        }
    checked = check_score(case_data, ledger, record_refs, issue_type, rationale)
    new_findings = []
    for finding in checked.get("findings", []):
        if finding["status"] == "new_score":
            key = f"{finding['record_id']}::{finding['issue_key']}"
            ledger[key] = finding
            new_findings.append(finding)

    scorecard = get_scorecard(ledger)
    new_counts = count_records(new_findings)
    matched_counts = count_records(checked.get("findings", []))
    checked["scorecard"] = scorecard
    checked["score_summary"] = {
        "score_delta": checked.get("score_delta", 0),
        "issue_type": checked.get("issue_type", issue_type),
        "new_contract_count": new_counts["contract_count"],
        "new_customer_count": new_counts["customer_count"],
        "matched_contract_count": matched_counts["contract_count"],
        "matched_customer_count": matched_counts["customer_count"],
        "total_score": scorecard["total_score"],
        "total_contract_count": scorecard["total_contract_count"],
        "total_customer_count": scorecard["total_customer_count"],
    }
    return checked


def score_multiple_claims(
    case_data: dict[str, Any],
    ledger: dict[str, dict[str, Any]],
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    results = []
    all_findings: list[dict[str, Any]] = []
    for claim in claims:
        result = score_one_claim(
            case_data,
            ledger,
            list(claim.get("record_refs") or []),
            str(claim.get("issue_type") or ""),
            str(claim.get("rationale") or ""),
        )
        results.append(result)
        all_findings.extend(result.get("findings", []))
    scorecard = get_scorecard(ledger)
    delta = sum(int(result.get("score_delta") or 0) for result in results)
    new_findings = [item for item in all_findings if item.get("status") == "new_score"]
    matched_counts = count_records(all_findings)
    new_counts = count_records(new_findings)
    return {
        "status": "new_score" if delta else "repeat" if any(item.get("status") == "repeat" for item in all_findings) else "unsupported",
        "issue_type": "multiple" if len(claims) != 1 else results[0].get("issue_type", ""),
        "findings": all_findings,
        "claim_results": results,
        "score_delta": delta,
        "scorecard": scorecard,
        "score_summary": {
            "score_delta": delta,
            "issue_type": "multiple" if len(claims) != 1 else results[0].get("issue_type", ""),
            "new_contract_count": new_counts["contract_count"],
            "new_customer_count": new_counts["customer_count"],
            "matched_contract_count": matched_counts["contract_count"],
            "matched_customer_count": matched_counts["customer_count"],
            "total_score": scorecard["total_score"],
            "total_contract_count": scorecard["total_contract_count"],
            "total_customer_count": scorecard["total_customer_count"],
        },
    }


def update_score(
    case_data: dict[str, Any],
    ledger: dict[str, dict[str, Any]],
    record_refs: list[str],
    issue_type: str,
    rationale: str = "",
    claims: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fallback_refs = normalize_ref_list(case_data, record_refs)
    if claims:
        prepared = build_claims_from_model_claims(case_data, claims, fallback_refs, issue_type, rationale)
    else:
        prepared = build_scope_aware_claims(case_data, fallback_refs, [issue_type], rationale)

    if prepared.get("status") == "ready":
        scored = score_multiple_claims(case_data, ledger, prepared["claims"])
        if prepared.get("incompatible"):
            scored["incompatible"] = prepared["incompatible"]
        return scored
    if prepared.get("status") in {"needs_narrowing", "needs_contract_examples", "ambiguous_refs"}:
        prepared.setdefault("issue_type", issue_type)
        prepared.setdefault("scorecard", get_scorecard(ledger))
        return prepared
    return score_one_claim(case_data, ledger, record_refs, issue_type, rationale)

def get_scorecard(ledger: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_issue: dict[str, list[dict[str, Any]]] = {}
    for finding in ledger.values():
        by_issue.setdefault(finding["issue_type"], []).append(
            {
                "record_id": finding["record_id"],
                "contract_id": finding["contract_id"],
                "customer_id": finding["customer_id"],
                "customer_name": finding["customer_name"],
            }
        )

    by_issue_type = {}
    for issue_type, findings in by_issue.items():
        counts = count_records(findings)
        by_issue_type[issue_type] = {
            "findings": findings,
            "score": len(findings),
            "contract_count": counts["contract_count"],
            "customer_count": counts["customer_count"],
        }

    all_findings = list(ledger.values())
    total_counts = count_records(all_findings)
    return {
        "total_score": len(ledger),
        "total_contract_count": total_counts["contract_count"],
        "total_customer_count": total_counts["customer_count"],
        "by_issue_type": by_issue_type,
        "scored_keys": sorted(ledger),
    }


def image_to_data_url(file_name: str, content: bytes) -> str:
    ext = Path(file_name).suffix.lower().lstrip(".") or "png"
    mime = "image/jpeg" if ext in {"jpg", "jpeg"} else "image/png"
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def build_user_content(text: str, image_data_urls: list[str] | None = None) -> list[dict[str, str]]:
    content: list[dict[str, str]] = [{"type": "input_text", "text": text or ""}]
    for data_url in image_data_urls or []:
        content.append({"type": "input_image", "image_url": data_url})
    return content


def to_response_input(chat_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recent = chat_history[-RECENT_HISTORY_LIMIT:]
    items: list[dict[str, Any]] = []
    for message in recent:
        role = "assistant" if message["role"] == "assistant" else "user"
        if role == "user":
            items.append(
                {
                    "role": "user",
                    "content": build_user_content(message.get("content", ""), message.get("images", [])),
                }
            )
        else:
            items.append({"role": "assistant", "content": [{"type": "output_text", "text": message.get("content", "")}]})
    return items


def ordered_matches(pattern: re.Pattern[str], text: str) -> list[str]:
    seen: set[str] = set()
    matches: list[str] = []
    for match in pattern.finditer(text):
        value = match.group(0).upper()
        if value not in seen:
            seen.add(value)
            matches.append(value)
    return matches


def latest_user_text(chat_history: list[dict[str, Any]]) -> str:
    for message in reversed(chat_history):
        if message.get("role") == "user":
            return str(message.get("content", ""))
    return ""


def active_record_refs(case_data: dict[str, Any], chat_history: list[dict[str, Any]], include_latest: bool = True) -> list[str]:
    source = chat_history if include_latest else chat_history[:-1]
    user_messages = [str(message.get("content", "")) for message in source if message.get("role") == "user"]
    text = "\n".join(user_messages)
    return resolve_record_references_from_text(case_data, text, include_customer_names=False)["refs"][-6:]


def explicit_record_refs_from_text(case_data: dict[str, Any], text: str) -> list[str]:
    return resolve_record_references_from_text(case_data, text)["refs"]


def explicit_record_resolution_from_text(case_data: dict[str, Any], text: str) -> dict[str, Any]:
    return resolve_record_references_from_text(case_data, text)


def normalize_ref_list(case_data: dict[str, Any], refs: list[str] | None) -> list[str]:
    if not refs:
        return []
    return resolve_record_references_from_text(case_data, "\n".join(str(ref) for ref in refs))["refs"]


ISSUE_HINT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("LOW INTEREST RATE DESPITE SIGNIFICANT OVERDUES", re.compile(r"\b(low|soft|discounted|cheap|too\s+low)\b.{0,80}\b(rate|pricing|interest)\b.{0,120}\b(overdue|arrears|dpd|payment\s+issues?)\b|\b(overdue|arrears|dpd|payment\s+issues?)\b.{0,120}\b(low|soft|discounted|cheap|too\s+low)\b.{0,80}\b(rate|pricing|interest)\b|\binterest\s+rate\b.{0,120}\boverdue\b", re.IGNORECASE)),
    ("RECOVERED_OVERDUE_NOT_DISCLOSED", re.compile(r"\brecovered\s+(overdue|arrears)\b|\b(past|previous|recent|cleared)\s+(overdue|arrears|payment\s+issue)\b.{0,100}\b(not\s+disclosed|not\s+mentioned|omitted|missing)\b|\barrears\s+cleared\b.{0,100}\b(not\s+disclosed|not\s+mentioned|omitted|missing)\b", re.IGNORECASE)),
    ("ACTIVE_OVERDUE_AT_APPROVAL", re.compile(r"\b(overdue|arrears|dpd|days\s+past\s+due|past\s+due)\b.{0,100}\bapprov(?:al|ed|als)\b|\bapprov(?:al|ed|als)\b.{0,100}\b(overdue|arrears|dpd|days\s+past\s+due|past\s+due)\b|\boverdues?\b.{0,80}\b(?:when|at)\b.{0,40}\b(?:the\s+)?(?:contract\s+)?approved\b", re.IGNORECASE)),
    ("AML RISK", re.compile(r"\b(aml|anti[-\s]?money|money\s+laundering|kyc|beneficial\s+owner|pep|sanctions?\s+screening|suspicious\s+ownership)\b", re.IGNORECASE)),
    ("APPROVAL BY ROLE THAT DOESN'T EXIST", re.compile(r"\b(?:invalid|non[-\s]?existent|nonexistent|not\s+valid|not\s+recognized|unrecognized)\b.{0,80}\b(approval\s+)?(role|title)\b|\b(approval\s+)?(role|title)\b.{0,80}\b(doesn['?]?t\s+exist|does\s+not\s+exist|not\s+in|not\s+valid|not\s+recognized|unrecognized|authority\s+matrix)\b|\bauthority\s+matrix\b.{0,80}\b(role|title)\b", re.IGNORECASE)),
    ("APPROVAL IS ACTUALLY FOR ANOTHER CUSTOMER", re.compile(r"\bapproval\b.{0,100}\b(another|different|wrong)\s+(customer|entity)\b|\b(approval\s+)?(memo|pack|paperwork|evidence|document)\b.{0,100}\b(names?|belongs\s+to|for)\b.{0,60}\b(another|different|wrong)\s+(customer|entity)\b|\bcopy[-\s]?paste\b.{0,80}\bapproval\b|\bmismatch(?:ed)?\b.{0,80}\bapproval\b", re.IGNORECASE)),
    ("CONNECTED CUSTOMER EXPOSURE HIDDEN BY SEPARATE CUSTOMER IDS", re.compile(r"\b(connected|related)\s+customers?\b|\bseparate\s+customer\s+ids?\b|\bsplit\s+(?:customer\s+)?ids?\b|\bhidden\s+(?:connected\s+|group\s+)?exposure\b|\bunderstated\s+exposure\b|\bgroup\s+exposure\b.{0,80}\bnot\s+consolidated\b|\bsame\s+beneficial\s+owner\b", re.IGNORECASE)),
    ("CONTRACT APPROVED AFTER START DATE", re.compile(r"\b(start(?:ed)?|start\s+date|began)\b.{0,100}\b(before|earlier\s+than|prior\s+to|came\s+first)\b.{0,70}\bapproval\b|\bstart\s+date\s+came\s+first\b|\bapproval\b.{0,90}\b(after|later\s+than|post[-\s]?dates?)\b.{0,70}\b(start(?:ed)?|start\s+date|began)\b|\blate\s+approval\b|\bafter[-\s]?the[-\s]?fact\s+approval\b|\bbackdated\s+approval\b|\bsame\s+timing\s+issue\b", re.IGNORECASE)),
    ("CUSTOMER IN TAX HAVEN", re.compile(r"\btax\s+havens?\b|\boffshore\s+jurisdiction\b|\bhigh[-\s]?risk\s+jurisdiction\b|\bjurisdiction\b.{0,80}\b(escalat|risk|tax\s+havens?|offshore)\b|\b(?:domiciled|registered)\b.{0,80}\b(tax\s+havens?|offshore|high[-\s]?risk)\b", re.IGNORECASE)),
    ("CUSTOMER RISK DETERIORATES BUT EXPOSURE KEEPS GROWING", re.compile(r"\b(risk|rating|credit\s+grade|risk\s+class)\b.{0,100}\b(deteriorat\w*|worsen\w*|declin\w*)\b.{0,120}\b(exposure|limits?|contracts?|approv)\b|\b(exposure|limits?)\b.{0,100}\b(grew|grow\w*|increas\w*|keeps?\s+growing)\b.{0,120}\b(risk|rating|grade)\b", re.IGNORECASE)),
    ("CUSTOMER_IN_DEFAULT_AT_APPROVAL", re.compile(r"\bdefault\b.{0,100}\bapprov(?:al|ed|als)\b|\bapprov(?:al|ed|als)\b.{0,100}\bdefault\b|\bdefault\s+status\b.{0,80}\b(block|approval)\b", re.IGNORECASE)),
    ("DOWN PAYMENT TOO LOW", re.compile(r"\b(down[-\s]?payment|deposit|upfront\s+payment|equity\s+contribution|customer\s+contribution)\b.{0,90}\b(low|too\s+low|below|insufficient|too\s+small)\b", re.IGNORECASE)),
    ("FINANCING ONLY NON TRATON BRANDS", re.compile(r"\b(only|all)\b.{0,80}\b(non[-\s]?traton|third[-\s]?party\s+brands?)\b|\bnon[-\s]?traton\b.{0,80}\bonly\b|\bno\s+(scania|man|traton)\b.{0,80}\bbrands?\b|\bbrand\s+mix\b.{0,80}\boutside\s+traton\b", re.IGNORECASE)),
    ("INFLATED PRICING", re.compile(r"\b(?:inflated|inflation)\b.{0,100}\b(price|pricing|value|amount|finance|financed|invoice)\b|\b(price|pricing|value|amount|finance|financed|invoice)\b.{0,120}\b(?:inflated|over\s*priced|overpriced|over[-\s]?valued|too\s+high|above\s+market|higher\s+than|many\s+times\s+higher|similar\s+models?)\b|\b(?:many\s+times|much|far)\s+higher\b.{0,80}\b(?:similar|comparable|market|model|models)\b|\bover\s*priced\b|\boverpriced\b|\bdealer\s+invoice\b.{0,80}\binflated\b", re.IGNORECASE)),
    ("INSUFFICIENT APPROVAL AUTHORITY", re.compile(r"\b(approval\s+)?(authority|mandate|level)\b.{0,90}\b(too\s+low|wrong|insufficient|not\s+enough|exceeded|outside)\b|\bwrong\s+authority\b|\bdelegated\s+authority\b|\bneeded\s+committee\s+approval\b|\bexceeded\s+mandate\b", re.IGNORECASE)),
    ("INTEREST RATE EXTREMELY LOW", re.compile(r"\binterest\s+rate\b.{0,90}\b(abnormally\s+low|extremely\s+low|too\s+low|below[-\s]?market|below|soft)\b|\b(pricing|rate)\b.{0,90}\b(too\s+soft|abnormally\s+low|below[-\s]?market|doesn['?]?t\s+match\s+risk|does\s+not\s+match\s+risk)\b", re.IGNORECASE)),
    ("NO APPROVAL RECORDED", re.compile(r"\bno\s+(credit\s+)?approval\b|\bapproval\b.{0,50}\b(missing|absent|not\s+recorded)\b|\bapproval\s+record\b.{0,50}\bmissing\b|\bcannot\s+find\s+(?:any\s+)?approval\b", re.IGNORECASE)),
    ("MISSING OR WEAK APPROVAL NARRATIVE", re.compile(r"\b(approval\s+)?(note|notes|narrative|narratives|rationale|rationales|commentary|write[-\s]?up|write[-\s]?ups|memo|memos|justification|justifications)\b.{0,100}\b(thin|weak|missing|generic|empty|poor|justify|justifies|explain|explains?)\b|\b(thin|weak|missing|generic|empty|poor|no\s+proper|doesn['?]?t\s+explain|does\s+not\s+explain)\b.{0,100}\b(approval\s+)?(note|notes|narrative|narratives|rationale|rationales|commentary|write[-\s]?up|write[-\s]?ups|memo|memos|justification|justifications|risk)\b", re.IGNORECASE)),
    ("MV CURVES DO NOT MATCH ASSET", re.compile(r"\b(mv|residual\s+value|market\s+value)\s+curves?\b.{0,100}\b(match|mismatch|wrong|asset|category|type)\b|\bwrong\s+curve\b|\bcurve\s+mismatch\b|\bbooked\b.{0,80}\bwrong\s+curve\b|\basset\s+category\b.{0,80}\bcurve\b", re.IGNORECASE)),
    ("NON COMMERCIAL VEHICLE RELATED ASSETS", re.compile(r"\b(passenger\s+(?:vehicle|car)|private[-\s]?use|non[-\s]?commercial|outside\s+(?:the\s+)?commercial|not\s+(?:normal\s+)?commercial\s+vehicle\s+financing|does(?:n['?]?t| not)\s+(?:sound|look)\s+like\s+normal\s+commercial|not\s+related\s+to\s+commercial\s+vehicles?|asset\s+is\s+the\s+issue|asset\s+issue|finance\s+a\s+car)\b", re.IGNORECASE)),
    ("PORTFOLIO SNAPSHOT DOES NOT RECONCILE TO CONTRACT-LEVEL DATA", re.compile(r"\breconcile\b|\bportfolio\s+snapshot\b|\bsnapshot\b.{0,100}\b(tie|match|contract[-\s]?level|inconsistent)\b|\b(portfolio|summary|exposure\s+total|numbers?)\b.{0,100}\b(differs?|mismatch|doesn['?]?t\s+match|does\s+not\s+match|doesn['?]?t\s+tie|does\s+not\s+tie)\b", re.IGNORECASE)),
    ("VAGUE HARD COLLATERAL", re.compile(r"\bhard\s+collateral\b.{0,100}\b(vague|weak|unclear|not\s+specific|not\s+described|generic)\b|\b(vague|weak|unclear|generic)\b.{0,100}\bhard\s+collateral\b", re.IGNORECASE)),
)

SCORE_SIGNAL_RE = re.compile(
    r"\b(against|breach|violat(?:e|es|ed|ing|ion)?|issue|problem|concern|finding|wrong|unsupported|ineligible|default|overdue|arrears|dpd|past\s+due|passenger|non[-\s]?commercial|aml|kyc|pep|sanctions?|beneficial\s+owner|authority|mandate|matrix|role|title|mismatch|different\s+customer|wrong\s+customer|connected|related|split|consolidated|tax\s+havens?|offshore|jurisdiction|risk|rating|grade|exposure|deposit|down[-\s]?payment|upfront|equity\s+contribution|non[-\s]?traton|third[-\s]?party|inflated|over\s*priced|overpriced|over[-\s]?valued|too\s+high|above\s+market|abnormal|abnormally|low|soft|weak|thin|missing|absent|justify|justification|rationale|write[-\s]?up|writeup|memo|mv\s+curve|residual\s+value\s+curve|market\s+value\s+curve|snapshot|reconcile|recovered|cleared|hard\s+collateral|price|pricing|financed\s+amount|similar\s+models?|higher\s+than|many\s+times\s+higher|should(?:n['?]?t| not)|does(?:n['?]?t| not)\s+(?:sound|look)|approved\s+even\s+though|appears\s+to\s+have|looks?\s+like|came\s+first|correct\?|explain|before)\b",
    re.IGNORECASE,
)
UNPINNED_FINDING_REQUEST_RE = re.compile(
    r"\b(?:what|which|tell\s+me|show\s+me|reveal)\b.{0,60}\b(?:real|actual|other|hidden|else)?\s*(?:issue|issues|problem|problems|finding|findings|wrong)\b",
    re.IGNORECASE,
)


BULK_REF_RE = re.compile(r"\b(?:every|all)\s+SE\d{6}[-\s]series\b|\bSE\d{6}\s+through\s+SE\d{6}\b|\bscore\s+the\s+lot\b|\bpasted\s+\d+\s+contract", re.IGNORECASE)
DIRECT_PUBLIC_FACT_RE = re.compile(
    r"\b(what\s+(?:asset|assets|vehicle|vehicles|kind|type)|what\s+kind|what\s+type|which\s+(?:asset|assets|vehicle|vehicles|brand|brands)|asset\s+(?:attached|mix|details?)|brand|brands|vin|asset\s*id|model|body\s+type|approval\s+(?:date|level)|who\s+approved|interest\s+rate|rate|down[-\s]?payment|deposit|collateral|exposure|status|performance|overdues?|start\s+date|end\s+date|contract\s+date)\b",
    re.IGNORECASE,
)
GENERAL_POLICY_RE = re.compile(
    r"\b(?:what|explain|tell\s+me|walk\s+me\s+through)\b.{0,80}\b(?:general\s+)?(?:policy|rule|rules|standard|criteria)\b|\b(?:policy|rule|rules|standard|criteria)\b.{0,80}\b(?:in\s+general|generally|on|for)\b",
    re.IGNORECASE,
)
RECORD_POLICY_JUDGMENT_RE = re.compile(
    r"\b(?:does|did|is|was|should|shouldn['?]?t|should\s+not)\b.{0,80}\b(?:violat\w*|against\s+(?:policy|rules?)|breach|allowed|approved|ineligible)\b|\b(?:policy|rules?)\b.{0,80}\b(?:violat\w*|breach|against)\b",
    re.IGNORECASE,
)
RECORD_VIOLATION_EXPLANATION_RE = re.compile(
    r"\bwhy\b.{0,80}\b(?:violat\w*|breach|against\s+policy|approve|approved|approval|allowed|ineligible)\b|\bwhy\s+(?:did|was)\b.{0,80}\b(?:approve|approved|approval)\b",
    re.IGNORECASE,
)


def issue_hint_from_text(case_data: dict[str, Any], text: str) -> str | None:
    for issue_type, pattern in ISSUE_HINT_PATTERNS:
        if pattern.search(text or "") and resolve_issue_key(case_data, issue_type):
            return issue_type
    return None


def general_policy_question(text: str) -> bool:
    latest = text or ""
    return bool(GENERAL_POLICY_RE.search(latest)) and not bool(RECORD_POLICY_JUDGMENT_RE.search(latest))


def record_policy_judgment_question(text: str) -> bool:
    return bool(RECORD_POLICY_JUDGMENT_RE.search(text or ""))


def record_violation_explanation_question(text: str) -> bool:
    return bool(RECORD_VIOLATION_EXPLANATION_RE.search(text or ""))


def asks_for_unpinned_finding(case_data: dict[str, Any], text: str) -> bool:
    return not issue_intents_from_text(case_data, text) and bool(
        UNPINNED_FINDING_REQUEST_RE.search(text or "")
    )


def recent_issue_hint_before_latest(case_data: dict[str, Any], chat_history: list[dict[str, Any]]) -> str | None:
    for message in reversed(chat_history[:-1]):
        if message.get("role") != "user":
            continue
        issue_type = issue_hint_from_text(case_data, str(message.get("content", "")))
        if issue_type:
            return issue_type
    return None


def issue_intents_from_text(case_data: dict[str, Any], text: str) -> list[str]:
    if general_policy_question(text):
        return []
    intents: list[str] = []
    for issue_type, pattern in ISSUE_HINT_PATTERNS:
        if not pattern.search(text or ""):
            continue
        issue_key = resolve_issue_key(case_data, issue_type)
        if issue_key:
            add_unique_ref(intents, case_data["issue_by_key"][issue_key])
    return intents


def issue_intent_for_turn(case_data: dict[str, Any], chat_history: list[dict[str, Any]]) -> str | None:
    intents = issue_intents_from_text(case_data, latest_user_text(chat_history))
    return intents[0] if intents else None


CUSTOMER_SCOPED_ISSUE_NAMES = {
    "AML RISK",
    "CUSTOMER IN TAX HAVEN",
    "CONNECTED CUSTOMER EXPOSURE HIDDEN BY SEPARATE CUSTOMER IDS",
    "CUSTOMER RISK DETERIORATES BUT EXPOSURE KEEPS GROWING",
    "PORTFOLIO SNAPSHOT DOES NOT RECONCILE TO CONTRACT-LEVEL DATA",
    "CUSTOMER_IN_DEFAULT_AT_APPROVAL",
}

CONTRACT_SCOPED_ISSUE_NAMES = {
    "ACTIVE_OVERDUE_AT_APPROVAL",
    "APPROVAL BY ROLE THAT DOESN'T EXIST",
    "APPROVAL IS ACTUALLY FOR ANOTHER CUSTOMER",
    "CONTRACT APPROVED AFTER START DATE",
    "CUSTOMER_IN_DEFAULT_AT_APPROVAL",
    "DOWN PAYMENT TOO LOW",
    "FINANCING ONLY NON TRATON BRANDS",
    "INFLATED PRICING",
    "INSUFFICIENT APPROVAL AUTHORITY",
    "INTEREST RATE EXTREMELY LOW",
    "LOW INTEREST RATE DESPITE SIGNIFICANT OVERDUES",
    "MISSING OR WEAK APPROVAL NARRATIVE",
    "MV CURVES DO NOT MATCH ASSET",
    "NO APPROVAL RECORDED",
    "NON COMMERCIAL VEHICLE RELATED ASSETS",
    "RECOVERED_OVERDUE_NOT_DISCLOSED",
    "VAGUE HARD COLLATERAL",
}


def shared_issue_batch_allowed(case_data: dict[str, Any], refs: list[str], issue_types: list[str]) -> bool:
    normalized_refs = normalize_ref_list(case_data, refs)
    normalized_issues = [resolve_issue_key(case_data, item) for item in issue_types]
    return (
        1 < len(normalized_refs) <= MAX_SHARED_ISSUE_BATCH_RECORDS
        and len(set(item for item in normalized_issues if item)) == 1
        and all(record_type_for_ref(case_data, ref) == 'contract' for ref in normalized_refs)
    )


def allowed_issue_key_set(case_data: dict[str, Any], issue_names: set[str]) -> set[str]:
    cache = case_data.setdefault("_issue_scope_key_cache", {})
    cache_key = tuple(sorted(issue_names))
    if cache_key in cache:
        return cache[cache_key]
    keys = set()
    for issue_name in issue_names:
        issue_key = resolve_issue_key(case_data, issue_name)
        if issue_key:
            keys.add(issue_key)
    cache[cache_key] = keys
    return keys


def record_type_for_ref(case_data: dict[str, Any], ref: str) -> str | None:
    value = str(ref or "").upper()
    if value in case_data["contracts"]:
        return "contract"
    if value in case_data["customers"]:
        return "customer"
    return None


def issue_compatible_with_target(
    case_data: dict[str, Any],
    ref: str,
    issue_type: str,
    strict_contract_scope: bool,
) -> bool:
    issue_key = resolve_issue_key(case_data, issue_type)
    record_type = record_type_for_ref(case_data, ref)
    if not issue_key or not record_type:
        return False

    customer_issue_keys = allowed_issue_key_set(case_data, CUSTOMER_SCOPED_ISSUE_NAMES)
    contract_issue_keys = allowed_issue_key_set(case_data, CONTRACT_SCOPED_ISSUE_NAMES)
    if record_type == "customer":
        return issue_key in customer_issue_keys
    if issue_key in contract_issue_keys:
        return True
    return not strict_contract_scope and issue_key in customer_issue_keys


def score_preflight_error(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    result = {
        "status": status,
        "reason": reason,
        "findings": [],
        "score_delta": 0,
    }
    result.update(extra)
    return result


def build_scope_aware_claims(
    case_data: dict[str, Any],
    record_refs: list[str],
    issue_types: list[str],
    rationale: str,
) -> dict[str, Any]:
    refs = normalize_ref_list(case_data, record_refs)
    resolved_issue_types: list[str] = []
    for issue_type in issue_types:
        issue_key = resolve_issue_key(case_data, issue_type)
        if issue_key:
            add_unique_ref(resolved_issue_types, case_data["issue_by_key"][issue_key])
    if not refs or not resolved_issue_types:
        return {"status": "no_claims", "claims": []}
    if len(refs) > MAX_RECORDS_PER_SCORE_TURN and not shared_issue_batch_allowed(case_data, refs, resolved_issue_types):
        return score_preflight_error(
            "needs_narrowing",
            "Too many records for one live-meeting score check.",
            target_count=len(refs),
        )

    has_customer_target = any(record_type_for_ref(case_data, ref) == "customer" for ref in refs)
    has_contract_target = any(record_type_for_ref(case_data, ref) == "contract" for ref in refs)
    strict_contract_scope = has_customer_target and has_contract_target
    claims: list[dict[str, Any]] = []
    incompatible: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    if shared_issue_batch_allowed(case_data, refs, resolved_issue_types):
        issue_type = resolved_issue_types[0]
        compatible_refs = []
        for ref in refs:
            if issue_compatible_with_target(case_data, ref, issue_type, strict_contract_scope):
                compatible_refs.append(ref)
            else:
                incompatible.append({"record_ref": ref, "issue_type": issue_type})
        if compatible_refs:
            claims.append({
                "record_refs": compatible_refs,
                "issue_type": issue_type,
                "rationale": rationale,
            })
    else:
        for ref in refs:
            for issue_type in resolved_issue_types:
                issue_key = resolve_issue_key(case_data, issue_type)
                if not issue_key:
                    continue
                if not issue_compatible_with_target(case_data, ref, issue_type, strict_contract_scope):
                    incompatible.append({"record_ref": ref, "issue_type": issue_type})
                    continue
                key = (ref, issue_key)
                if key in seen:
                    continue
                seen.add(key)
                claims.append({"record_refs": [ref], "issue_type": issue_type, "rationale": rationale})

    if len(claims) > MAX_CLAIMS_PER_SCORE_TURN:
        return score_preflight_error(
            "needs_narrowing",
            "Too many record-and-issue checks for one live-meeting turn.",
            target_count=len(refs),
            claim_count=len(claims),
        )
    if not claims and incompatible:
        return score_preflight_error(
            "needs_contract_examples",
            "That concern needs specific contract, asset, or VIN examples before it can be checked.",
            incompatible=incompatible,
        )
    return {"status": "ready", "claims": claims, "incompatible": incompatible}

def score_claims_need_explicit_pairing(record_refs: list[str], issue_types: list[str]) -> bool:
    return len(record_refs) > 1 and len(issue_types) > 1


def build_claims_from_model_claims(
    case_data: dict[str, Any],
    claims: list[dict[str, Any]],
    fallback_refs: list[str],
    fallback_issue_type: str | None,
    fallback_rationale: str,
) -> dict[str, Any]:
    incompatible: list[dict[str, str]] = []
    target_refs: list[str] = []
    has_customer_target = False
    has_contract_target = False
    normalized_claims: list[tuple[list[str], str, str]] = []

    for claim in claims:
        refs = normalize_ref_list(case_data, claim.get("record_refs")) or fallback_refs
        issue_type = str(claim.get("issue_type") or fallback_issue_type or "")
        issue_key = resolve_issue_key(case_data, issue_type)
        if not refs or not issue_key:
            continue
        canonical_issue_type = case_data["issue_by_key"][issue_key]
        rationale = str(claim.get("rationale") or fallback_rationale)
        normalized_claims.append((refs, canonical_issue_type, rationale))
        for ref in refs:
            add_unique_ref(target_refs, ref)
            has_customer_target = has_customer_target or record_type_for_ref(case_data, ref) == "customer"
            has_contract_target = has_contract_target or record_type_for_ref(case_data, ref) == "contract"

    issue_types = unique_ordered([item[1] for item in normalized_claims])
    if len(target_refs) > MAX_RECORDS_PER_SCORE_TURN and not shared_issue_batch_allowed(case_data, target_refs, issue_types):
        return score_preflight_error("needs_narrowing", "Too many records for one live-meeting score check.", target_count=len(target_refs))

    strict_contract_scope = has_customer_target and has_contract_target
    prepared_claims: list[dict[str, Any]] = []
    if shared_issue_batch_allowed(case_data, target_refs, issue_types):
        issue_type = issue_types[0]
        compatible_refs = [
            ref for ref in target_refs
            if issue_compatible_with_target(case_data, ref, issue_type, strict_contract_scope)
        ]
        for ref in target_refs:
            if ref not in compatible_refs:
                incompatible.append({"record_ref": ref, "issue_type": issue_type})
        if compatible_refs:
            rationale = normalized_claims[0][2]
            prepared_claims.append({
                "record_refs": compatible_refs,
                "issue_type": issue_type,
                "rationale": rationale,
            })
    else:
        seen: set[tuple[str, str]] = set()
        for refs, issue_type, rationale in normalized_claims:
            for ref in refs:
                if not issue_compatible_with_target(case_data, ref, issue_type, strict_contract_scope):
                    incompatible.append({"record_ref": ref, "issue_type": issue_type})
                    continue
                issue_key = resolve_issue_key(case_data, issue_type)
                key = (ref, issue_key or "")
                if key in seen:
                    continue
                seen.add(key)
                prepared_claims.append({"record_refs": [ref], "issue_type": issue_type, "rationale": rationale})

    if len(prepared_claims) > MAX_CLAIMS_PER_SCORE_TURN:
        return score_preflight_error("needs_narrowing", "Too many record-and-issue checks for one live-meeting turn.", claim_count=len(prepared_claims))
    if not prepared_claims and incompatible:
        return score_preflight_error("needs_contract_examples", "That concern needs specific contract, asset, or VIN examples before it can be checked.", incompatible=incompatible)
    if not prepared_claims:
        return {"status": "no_claims", "claims": []}
    return {"status": "ready", "claims": prepared_claims, "incompatible": incompatible}

def score_preflight_for_turn(
    case_data: dict[str, Any],
    chat_history: list[dict[str, Any]],
    active_refs: list[str] | None = None,
) -> dict[str, Any] | None:
    latest = latest_user_text(chat_history)
    latest_resolution = explicit_record_resolution_from_text(case_data, latest)
    if latest_resolution.get("ambiguous") and not latest_resolution.get("refs"):
        return score_preflight_error("ambiguous_refs", "Ambiguous record reference.", ambiguous=latest_resolution["ambiguous"])

    latest_refs = latest_resolution.get("refs", [])
    refs = latest_refs or normalize_ref_list(case_data, active_refs) or active_record_refs(case_data, chat_history, include_latest=False)
    issue_types = issue_intents_from_text(case_data, latest)
    if len(refs) > MAX_RECORDS_PER_SCORE_TURN and not shared_issue_batch_allowed(case_data, refs, issue_types):
        return score_preflight_error("needs_narrowing", "Too many records for one live-meeting score check.", target_count=len(refs))
    if BULK_REF_RE.search(latest or ""):
        return score_preflight_error("needs_narrowing", "Too many records for one live-meeting score check.", target_count=len(refs))
    if not refs or not issue_types:
        return None
    prepared = build_scope_aware_claims(case_data, refs, issue_types, latest)
    return prepared if prepared.get("status") != "ready" else None

def score_preflight_reply(preflight: dict[str, Any]) -> str:
    status = preflight.get("status")
    if status == "ambiguous_refs":
        return "[MOOD:Annoyed / Dismissive]\nWhich record do you mean? Give me the exact contract or customer ID."
    if status == "needs_contract_examples":
        return "[MOOD:Annoyed / Dismissive]\nGive me specific contract, asset, or VIN examples. I am not conceding a customer-level allegation that broad."
    if status == "needs_explicit_claims":
        return "[MOOD:Annoyed / Dismissive]\nTie each concern to its contract. I am not guessing which allegation belongs where."
    return "[MOOD:Annoyed / Dismissive]\nThat is too much for this meeting. Pick up to five records and keep it to the specific concern."


def singular_followup_needs_clarification(case_data: dict[str, Any], chat_history: list[dict[str, Any]], active_refs: list[str] | None = None) -> bool:
    latest = latest_user_text(chat_history)
    if explicit_record_refs_from_text(case_data, latest):
        return False
    refs = normalize_ref_list(case_data, active_refs) or active_record_refs(case_data, chat_history, include_latest=False)
    if len(refs) <= 1 or uses_plural_active_record_reference(latest):
        return False
    return bool(fact_detail_intent(latest) or issue_intents_from_text(case_data, latest) or SCORE_SIGNAL_RE.search(latest or ""))


def routing_direct_reply(case_data: dict[str, Any], chat_history: list[dict[str, Any]], active_refs: list[str] | None = None) -> str | None:
    bulk_reply = bulk_contract_reply(chat_history, case_data)
    if bulk_reply:
        return bulk_reply

    latest = latest_user_text(chat_history)
    latest_resolution = explicit_record_resolution_from_text(case_data, latest)
    if latest_resolution.get("ambiguous") and not latest_resolution.get("refs"):
        return "[MOOD:Annoyed / Dismissive]\nWhich record do you mean? Give me the exact contract or customer ID."

    if asks_for_unpinned_finding(case_data, latest):
        return "[MOOD:Annoyed / Dismissive]\nIf you think there is an issue, name it and tie it to the contract. I am not going to find one for you."
    if singular_followup_needs_clarification(case_data, chat_history, active_refs):
        return "[MOOD:Annoyed / Dismissive]\nWhich one are we talking about? Give me the contract ID."

    preflight = score_preflight_for_turn(case_data, chat_history, active_refs)
    if preflight:
        return score_preflight_reply(preflight)
    return None


def should_let_model_classify_issue(
    case_data: dict[str, Any],
    chat_history: list[dict[str, Any]],
    active_refs: list[str] | None = None,
) -> bool:
    latest = latest_user_text(chat_history)
    if general_policy_question(latest) or issue_intent_for_turn(case_data, chat_history):
        return False

    refs = (
        explicit_record_refs_from_text(case_data, latest)
        or normalize_ref_list(case_data, active_refs)
        or active_record_refs(case_data, chat_history, include_latest=False)
    )
    if not refs:
        return False

    explicit_refs = explicit_record_refs_from_text(case_data, latest)
    if explicit_refs and not SCORE_SIGNAL_RE.search(latest or ""):
        return False

    if fact_detail_intent_name(latest) == "record_violation_explanation" and not record_violation_explanation_question(latest):
        return False
    if record_policy_judgment_question(latest) or record_violation_explanation_question(latest):
        return True

    direct_fact = direct_public_fact_question(latest) and bool(
        re.search(r"^\s*(what|which|who|when|give\s+me|show\s+me|tell\s+me|list)\b", latest, re.IGNORECASE)
    )
    concern_language = bool(
        SCORE_SIGNAL_RE.search(latest or "")
        or re.search(
            r"\b(seems?|looks?|feels?|sounds?|too\s+(?:low|high|small)|tiny|abnormal|odd|unusual|questionable|zero|0(?:\.0+)?%?|why)\b|\?",
            latest,
            re.IGNORECASE,
        )
    )
    if direct_fact and not re.search(r"\b(seems?|looks?|feels?|sounds?|too\s+(?:low|high|small)|tiny|abnormal|odd|unusual|zero|0(?:\.0+)?%?|violat|against|should)\b", latest, re.IGNORECASE):
        return False
    return concern_language and fact_detail_intent_name(latest) not in {"general_policy_question"}


def fact_detail_intent(text: str) -> bool:
    return fact_detail_intent_name(text) in {
        "vin",
        "asset_id",
        "asset_mix",
        "brand",
        "interest_rate",
        "down_payment",
        "collateral",
        "exposure",
        "performance",
        "approval",
        "date",
    }


def count_explicit_record_targets(text: str, case_data: dict[str, Any] | None = None) -> int:
    if case_data is not None:
        return len(resolve_record_references_from_text(case_data, text)["refs"])
    terms = extract_reference_terms(text)
    return len(set(terms["contract_ids"] + terms["customer_ids"] + terms["asset_ids"] + terms["vins"]))


def bulk_contract_reply(chat_history: list[dict[str, Any]], case_data: dict[str, Any] | None = None) -> str | None:
    latest = latest_user_text(chat_history)
    if case_data:
        refs = resolve_record_references_from_text(case_data, latest).get("refs", [])
        issue_types = issue_intents_from_text(case_data, latest)
        too_many_targets = len(refs) > MAX_RECORDS_PER_SCORE_TURN and not shared_issue_batch_allowed(case_data, refs, issue_types)
    else:
        too_many_targets = count_explicit_record_targets(latest) > MAX_RECORDS_PER_SCORE_TURN
    if too_many_targets or BULK_REF_RE.search(latest or ""):
        return "[MOOD:Annoyed / Dismissive]\nThat is too much for this meeting. Narrow it to the contracts or issues you want checked."
    return None

def uses_plural_active_record_reference(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:these|those|them|their|both)\b|\ball(?:\s+of)?\s+(?:them|these|those|\d+|[a-z]+(?:\s+(?:records?|contracts?|customers?|assets?|cases?))?)\b",
            text or "",
            re.IGNORECASE,
        )
    )


def direct_public_fact_question(text: str) -> bool:
    return bool(DIRECT_PUBLIC_FACT_RE.search(text or ""))


def fact_detail_intent_name(text: str) -> str | None:
    latest = text or ""
    if re.search(r"\bvin\b", latest, re.IGNORECASE):
        return "vin"
    if re.search(r"\basset\s*id\b", latest, re.IGNORECASE):
        return "asset_id"
    if re.search(r"\b(what\s+(?:asset|assets|vehicle|vehicles|kind|type)|what\s+kind|what\s+type|which\s+(?:asset|assets|vehicle|vehicles)|asset\s+(?:attached|mix|details?)|model|body\s+type)\b", latest, re.IGNORECASE):
        return "asset_mix"
    if re.search(r"\b(which\s+brands?|brands?)\b", latest, re.IGNORECASE):
        return "brand"
    if re.search(r"\b(interest\s+rate|rate)\b", latest, re.IGNORECASE):
        return "interest_rate"
    if re.search(r"\b(down[-\s]?payment|deposit|upfront\s+payment|equity\s+contribution)\b", latest, re.IGNORECASE):
        return "down_payment"
    if re.search(r"\bcollateral\b", latest, re.IGNORECASE):
        return "collateral"
    if re.search(r"\bexposure\b", latest, re.IGNORECASE):
        return "exposure"
    if re.search(r"\b(status|performance|overdues?)\b", latest, re.IGNORECASE):
        return "performance"
    if re.search(r"\b(who\s+approved|approval\s+(?:date|level|timeline|picture|details?|record)|approved\s+on|approved\s+by)\b", latest, re.IGNORECASE):
        return "approval"
    if re.search(r"\b(start\s+date|end\s+date|contract\s+date|term)\b", latest, re.IGNORECASE):
        return "date"
    if general_policy_question(latest):
        return "general_policy_question"
    if record_violation_explanation_question(latest) or re.fullmatch(r"\s*why\s*\??\s*", latest, re.IGNORECASE):
        return "record_violation_explanation"
    return None


def build_session_context(case_data: dict[str, Any], chat_history: list[dict[str, Any]], active_refs: list[str] | None = None) -> str:
    refs = normalize_ref_list(case_data, active_refs) or active_record_refs(case_data, chat_history, include_latest=False)
    active_records: list[str] = []
    for ref in refs:
        record = case_data["contracts"].get(ref) or case_data["customers"].get(ref)
        if record:
            active_records.append(f"- {ref}: {build_brief_overview(record)}")

    if not active_records:
        return "Current session context: no active contract or customer yet. Ask for a concrete contract ID, customer ID, or customer name if needed."

    return "\n".join(
        [
            "Current session context for pronouns and follow-ups:",
            "If exactly one active record is listed, treat follow-up questions and findings as referring to that record unless the auditor names another record. Do not ask for the ID again.",
            "If multiple active records are listed and the latest auditor message is singular but ambiguous, ask a short clarification.",
            "Use brief_overview for general lookup replies; call get_case_material only for specific facts.",
            "For any concrete concern about a single active record, choose the closest issue type from the issue catalog and call update_score. Do not require exact issue wording.",
            "Issue catalog:",
            ", ".join(case_data["issue_columns"]),
            "Active records:",
            *active_records,
        ]
    )


def last_scored_issue_for_refs(ledger: dict[str, dict[str, Any]], refs: list[str]) -> str | None:
    issues = {
        finding["issue_type"]
        for finding in ledger.values()
        if finding.get("record_id") in refs or finding.get("contract_id") in refs or finding.get("customer_id") in refs
    }
    return next(iter(issues)) if len(issues) == 1 else None


def complete_tool_args(
    name: str,
    args: dict[str, Any],
    case_data: dict[str, Any],
    ledger: dict[str, dict[str, Any]],
    chat_history: list[dict[str, Any]],
    active_refs: list[str] | None = None,
) -> dict[str, Any]:
    completed = dict(args)
    latest = latest_user_text(chat_history)
    latest_message_refs = explicit_record_refs_from_text(case_data, latest)
    history_refs = active_record_refs(case_data, chat_history, include_latest=True)
    fallback_refs = normalize_ref_list(case_data, active_refs)
    refs = latest_message_refs or fallback_refs or history_refs
    issue_types = issue_intents_from_text(case_data, latest)
    issue_type = issue_types[0] if issue_types else None

    if name == "find_records" and not completed.get("query"):
        completed["query"] = latest
    if name in {"get_case_material", "check_score", "update_score"} and not completed.get("record_refs") and refs:
        completed["record_refs"] = refs
    if name in {"check_score", "update_score"} and not completed.get("issue_type") and issue_type:
        completed["issue_type"] = issue_type
    if name in {"check_score", "update_score"} and not completed.get("rationale"):
        completed["rationale"] = latest
    if name == "update_score":
        # Latest explicit targets must outrank stale model claim references.
        if latest_message_refs and completed.get("claims"):
            model_claims = list(completed.get("claims") or [])
            if len(latest_message_refs) == 1:
                for claim in model_claims:
                    claim["record_refs"] = list(latest_message_refs)
                completed["claims"] = model_claims
            elif all(
                not set(normalize_ref_list(case_data, claim.get("record_refs"))).intersection(latest_message_refs)
                for claim in model_claims
            ):
                if len(model_claims) == len(latest_message_refs):
                    for claim, ref in zip(model_claims, latest_message_refs):
                        claim["record_refs"] = [ref]
                    completed["claims"] = model_claims
                else:
                    completed["claims"] = []
        fallback_refs_for_claims = refs if uses_plural_active_record_reference(latest) or latest_message_refs else refs[:1]
        shared_issue_for_active_set = (
            len(refs) > 1
            and len(issue_types) == 1
            and uses_plural_active_record_reference(latest)
        )
        if shared_issue_for_active_set:
            prepared = build_scope_aware_claims(case_data, refs, issue_types, latest)
        elif completed.get("claims"):
            prepared = build_claims_from_model_claims(
                case_data,
                list(completed.get("claims") or []),
                fallback_refs_for_claims,
                issue_type,
                latest,
            )
        else:
            score_issues = issue_types or ([completed.get("issue_type")] if completed.get("issue_type") else [])
            if score_claims_need_explicit_pairing(refs, score_issues):
                prepared = score_preflight_error(
                    "needs_explicit_claims",
                    "Multiple records and multiple concerns need explicit record-to-issue pairs.",
                )
            else:
                prepared = build_scope_aware_claims(case_data, refs, score_issues, latest)
        if prepared.get("status") == "ready":
            completed["claims"] = prepared["claims"]
            first_claim = prepared["claims"][0]
            completed["record_refs"] = first_claim["record_refs"]
            completed["issue_type"] = first_claim["issue_type"]
            completed["rationale"] = first_claim["rationale"]
        elif prepared.get("status") not in {None, "no_claims"}:
            completed["_score_preflight_error"] = prepared
    return completed

def requests_record_lookup(text: str) -> bool:
    return bool(
        re.match(
            r"\s*(?:can\s+we\s+)?(?:look\s+at|open|review|check|inspect|pull\s+up|show\s+me|use)\b",
            text or "",
            re.IGNORECASE,
        )
    )

def choose_tool_choice_for_turn(
    case_data: dict[str, Any],
    chat_history: list[dict[str, Any]],
    active_refs: list[str] | None = None,
    force_final: bool = False,
) -> str | dict[str, str]:
    if force_final:
        return "none"
    if routing_direct_reply(case_data, chat_history, active_refs):
        return "none"

    latest = latest_user_text(chat_history)
    latest_message_refs = explicit_record_refs_from_text(case_data, latest)
    history_refs_before_latest = active_record_refs(case_data, chat_history, include_latest=False)
    fallback_refs = normalize_ref_list(case_data, active_refs)
    refs_with_latest = latest_message_refs or fallback_refs or history_refs_before_latest
    issue_type = issue_intent_for_turn(case_data, chat_history)

    if latest_message_refs and not issue_type and requests_record_lookup(latest):
        return {"type": "function", "name": "find_records"}
    if refs_with_latest and issue_type:
        return {"type": "function", "name": "update_score"}
    if refs_with_latest and should_let_model_classify_issue(case_data, chat_history, active_refs):
        return {"type": "function", "name": "update_score"}
    if refs_with_latest and fact_detail_intent(latest):
        return {"type": "function", "name": "get_case_material"}
    if latest_message_refs:
        return {"type": "function", "name": "find_records"}
    return "auto"


def should_require_tool(
    case_data: dict[str, Any],
    chat_history: list[dict[str, Any]],
    active_refs: list[str] | None = None,
) -> bool:
    choice = choose_tool_choice_for_turn(case_data, chat_history, active_refs)
    return isinstance(choice, dict) or choice not in {"auto", "none"}


def small_talk_reply(chat_history: list[dict[str, Any]], case_data: dict[str, Any] | None = None) -> str | None:
    latest = latest_user_text(chat_history).strip().lower()
    if not latest:
        return None
    if latest in {"hi", "hello", "hey", "hi there", "good morning", "morning"}:
        return "[MOOD:Professional / Controlled]\nMorning. We can discuss the book, but start me with a contract or customer."
    if re.fullmatch(r"how\s+(are|r)\s+(you|u)\??", latest):
        return "[MOOD:Annoyed / Dismissive]\nBusy, as usual. If we're doing this, start with a contract or customer."
    return None


def latest_message_extends_active_context(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:add|also|include|along\s+with|as\s+well)\b",
            text or "",
            re.IGNORECASE,
        )
    )

def active_refs_after_turn(
    case_data: dict[str, Any],
    chat_history: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    previous_refs: list[str] | None = None,
) -> list[str]:
    refs: list[str] = []
    for event in tool_events:
        output = event.get("output", {})
        for ref in output.get("primary_refs", []):
            if ref not in refs:
                refs.append(ref)
        for finding in output.get("findings", []):
            ref = finding.get("contract_id") or finding.get("customer_id") or finding.get("record_id")
            if ref and ref not in refs:
                refs.append(ref)
    if refs:
        if latest_message_extends_active_context(latest_user_text(chat_history)):
            refs = unique_ordered([*normalize_ref_list(case_data, previous_refs), *refs])
        return normalize_ref_list(case_data, refs)
    latest_refs = active_record_refs(case_data, chat_history, include_latest=True)
    return normalize_ref_list(case_data, previous_refs) or latest_refs




TOOL_DEFINITIONS = [
    {
        "type": "function",
        "name": "find_records",
        "description": "Resolve contract IDs, customer IDs, customer names, asset IDs, and VINs from auditor text or screenshot-extracted references. Returns identity and brief overview only, not the full public narrative.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "contract_ids": {"type": "array", "items": {"type": "string"}},
                "customer_ids": {"type": "array", "items": {"type": "string"}},
                "customer_names": {"type": "array", "items": {"type": "string"}},
                "asset_ids": {"type": "array", "items": {"type": "string"}},
                "vins": {"type": "array", "items": {"type": "string"}},
                "screenshot_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query", "contract_ids", "customer_ids", "customer_names", "asset_ids", "vins", "screenshot_refs"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_case_material",
        "description": "Get non-scoring public record details, or why/policy material after a finding is already verified. Do not use this for policy-sensitive yes/no probes; use update_score.",
        "parameters": {
            "type": "object",
            "properties": {
                "record_refs": {"type": "array", "items": {"type": "string"}},
                "issue_type": {"type": ["string", "null"]},
            },
            "required": ["record_refs", "issue_type"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "check_score",
        "description": "Verify whether the auditor identified a real issue for the right record. Does not update the score.",
        "parameters": {
            "type": "object",
            "properties": {
                "record_refs": {"type": "array", "items": {"type": "string"}},
                "issue_type": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["record_refs", "issue_type", "rationale"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "update_score",
        "description": "Verify and record concrete auditor concerns, accusations, anomalies, and policy-sensitive yes/no probes against the truth table. Duplicate or unsupported findings get zero points. Use claims for multiple record/issue pairs in one auditor turn.",
        "parameters": {
            "type": "object",
            "properties": {
                "record_refs": {"type": "array", "items": {"type": "string"}},
                "issue_type": {"type": "string"},
                "rationale": {"type": "string"},
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "record_refs": {"type": "array", "items": {"type": "string"}},
                            "issue_type": {"type": "string"},
                            "rationale": {"type": "string"}
                        },
                        "required": ["record_refs", "issue_type", "rationale"],
                        "additionalProperties": False
                    }
                }
            },
            "required": ["record_refs", "issue_type", "rationale", "claims"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_scorecard",
        "description": "Return current score by issue type and record.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def build_tool_definitions(case_data: dict[str, Any]) -> list[dict[str, Any]]:
    tools = copy.deepcopy(TOOL_DEFINITIONS)
    issue_columns = [str(issue) for issue in case_data.get("issue_columns", [])]
    if not issue_columns:
        return tools

    for tool in tools:
        if tool.get("name") not in {"check_score", "update_score"}:
            continue
        properties = tool.get("parameters", {}).get("properties", {})
        if "issue_type" in properties:
            properties["issue_type"]["enum"] = issue_columns
        claim_issue = (
            properties.get("claims", {})
            .get("items", {})
            .get("properties", {})
            .get("issue_type")
        )
        if claim_issue is not None:
            claim_issue["enum"] = issue_columns
    return tools


def load_prompt_text(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8").strip()


MIKAEL_INSTRUCTIONS = ""
MIKAEL_RESPONSE_STYLE = ""
MIKAEL_FINAL_RESPONSE = ""
ISSUE_LANGUAGE_GUIDE = ""


def build_runtime_issue_catalog(case_data: dict[str, Any]) -> str:
    issue_columns = [str(issue) for issue in case_data.get("issue_columns", [])]
    issue_list = case_data.get("issue_catalog_text") or "\n".join(f"- {issue}" for issue in issue_columns)
    return "\n".join(
        [
            "Runtime issue catalog from parquet. These are the only allowed issue_type values for update_score and check_score.",
            issue_list,
            "If the auditor raises a concern, anomaly, accusation, or policy-sensitive yes/no question about concrete or active records, interpret the meaning semantically, tolerate typos and informal wording, choose closest issue_type values from this runtime catalog, and call update_score. Do not require exact wording. Do not invent issue types. Do not answer issue yes/no probes from get_case_material.",
            "If one auditor turn contains multiple record/issue pairs, call update_score once with claims: one claim per compatible record and issue. Do not put extra issue phrases only in rationale.",
            "If customer IDs or customer names are paired with contract-level issues such as pricing, rates, down payment, approval, collateral, MV curves, or asset eligibility, ask for specific contract, asset, or VIN examples instead of applying the issue to every customer contract.",
        ]
    )

def build_current_turn_claim_instruction(
    case_data: dict[str, Any],
    chat_history: list[dict[str, Any]],
) -> str:
    latest_refs = explicit_record_refs_from_text(case_data, latest_user_text(chat_history))
    if len(latest_refs) < 2:
        return ""
    return (
        "Current auditor turn names these resolved records: "
        + ", ".join(latest_refs)
        + ". If you call update_score, include every explicit record-and-issue pair from this turn in claims. "
        "Do not score only the first pair, merge different concerns, or apply one concern to every record. "
        "Keep each claim tied to the record named with that concern."
    )


def build_agent_instructions(
    case_data: dict[str, Any],
    chat_history: list[dict[str, Any]],
    active_refs: list[str] | None = None,
) -> str:
    sections = [
        MIKAEL_INSTRUCTIONS,
        MIKAEL_RESPONSE_STYLE,
        ISSUE_LANGUAGE_GUIDE,
        build_runtime_issue_catalog(case_data),
        build_session_context(case_data, chat_history, active_refs),
    ]
    current_turn_instruction = build_current_turn_claim_instruction(case_data, chat_history)
    if current_turn_instruction:
        sections.append(current_turn_instruction)
    return "\n\n".join(sections)

def unaddressed_customer_refs_for_score_call(
    case_data: dict[str, Any],
    latest_user_message: str,
    claims: list[dict[str, Any]],
) -> list[str]:
    resolution = resolve_record_references_from_text(case_data, latest_user_message)
    claimed_refs: list[str] = []
    for claim in claims:
        for ref in normalize_ref_list(case_data, claim.get("record_refs")):
            add_unique_ref(claimed_refs, ref)
    return [ref for ref in resolution["customers"] if ref not in claimed_refs]

def call_tool(
    name: str,
    args: dict[str, Any],
    case_data: dict[str, Any],
    ledger: dict[str, dict[str, Any]],
    chat_history: list[dict[str, Any]] | None = None,
    active_refs: list[str] | None = None,
) -> dict[str, Any]:
    completed_args = complete_tool_args(name, args, case_data, ledger, chat_history or [], active_refs)
    preflight_error = completed_args.pop("_score_preflight_error", None)
    if preflight_error:
        return preflight_error
    try:
        if name == "find_records":
            return find_records(case_data, **completed_args)
        if name == "get_case_material":
            return get_case_material(case_data, **completed_args)
        if name == "check_score":
            return check_score(case_data, ledger, **completed_args)
        if name == "update_score":
            output = update_score(case_data, ledger, **completed_args)
            unaddressed_customers = unaddressed_customer_refs_for_score_call(
                case_data,
                latest_user_text(chat_history or []),
                list(completed_args.get("claims") or []),
            )
            if unaddressed_customers:
                output["unaddressed_customer_refs"] = unaddressed_customers
            return output
        if name == "get_scorecard":
            return get_scorecard(ledger)
        return {"error": f"Unknown tool: {name}"}
    except TypeError as exc:
        return {"error": f"Invalid tool arguments for {name}: {exc}", "arguments": completed_args}


def get_tool_calls(response: Any) -> list[Any]:
    calls = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) == "function_call":
            calls.append(item)
    return calls


def response_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return text
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) == "message":
            for content in getattr(item, "content", []) or []:
                if getattr(content, "type", None) in {"output_text", "text"}:
                    parts.append(getattr(content, "text", ""))
    return "\n".join(part for part in parts if part).strip()


def response_stopped_by_max_tokens(response: Any) -> bool:
    details = getattr(response, "incomplete_details", None)
    reason = getattr(details, "reason", None) if details is not None else None
    if isinstance(details, dict):
        reason = details.get("reason")
    status = getattr(response, "status", None)
    return status == "incomplete" and reason in {"max_tokens", "max_output_tokens"}


def portfolio_asset_mix_summary(items: list[dict[str, Any]]) -> str:
    seen_types: list[str] = []
    brands: list[str] = []
    for item in items:
        summary = str(item.get("asset_mix_summary") or item.get("brief_overview") or "").lower()
        for singular, plural in (("truck", "trucks"), ("trailer", "trailers"), ("bus", "buses"), ("passenger vehicle", "passenger vehicles")):
            if singular in summary and plural not in seen_types:
                seen_types.append(plural)
        brands.extend(part.strip() for part in str(item.get("brand_summary", "")).split(",") if part.strip())

    if len(seen_types) > 1:
        asset_summary = ", ".join(seen_types[:-1]) + " and " + seen_types[-1]
    else:
        asset_summary = seen_types[0] if seen_types else ""
    brand_summary = short_brand_summary(", ".join(unique_ordered(brands)))
    if asset_summary and brand_summary:
        return f"mostly {asset_summary}, {brand_summary}"
    if asset_summary:
        return f"mostly {asset_summary}"
    return ""


def compact_find_records_for_model(output: dict[str, Any]) -> dict[str, Any]:
    customer_contracts_sample = output.get("customer_contracts_sample", [])
    portfolio_mix = portfolio_asset_mix_summary(customer_contracts_sample)

    return {
        "primary_refs": output.get("primary_refs", []),
        "unmatched": output.get("unmatched", []),
        "contracts": [
            {
                "record_id": item.get("record_id"),
                "contract_id": item.get("contract_id"),
                "customer_id": item.get("customer_id"),
                "customer_name": item.get("customer_name"),
                "spoken_identification": item.get("spoken_identification"),
                "asset_mix_summary": item.get("asset_mix_summary"),
                "brand_summary": short_brand_summary(item.get("brand_summary")),
            }
            for item in output.get("contracts", [])
        ],
        "customers": [
            {
                "record_id": item.get("record_id"),
                "customer_id": item.get("customer_id"),
                "customer_name": item.get("customer_name"),
                "spoken_identification": item.get("spoken_identification"),
                "asset_mix_summary": item.get("asset_mix_summary") or portfolio_mix,
                "portfolio_asset_mix_summary": portfolio_mix,
                "brand_summary": short_brand_summary(item.get("brand_summary")),
            }
            for item in output.get("customers", [])
        ],
    }


def compact_tool_output_for_model(name: str, output: dict[str, Any]) -> dict[str, Any]:
    if name == "find_records" and not output.get("error"):
        return compact_find_records_for_model(output)
    if name != "update_score" or output.get("error"):
        return output

    findings = []
    for finding in output.get("findings", [])[:8]:
        findings.append(
            {
                "contract_id": finding.get("contract_id"),
                "customer_id": finding.get("customer_id"),
                "customer_name": finding.get("customer_name"),
                "issue_type": finding.get("issue_type"),
                "status": finding.get("status"),
                "brand_summary": finding.get("issue_brand_summary") or finding.get("brand_summary"),
                "issue_material": finding.get("issue_material"),
            }
        )

    result = {
        "status": output.get("status"),
        "issue_type": output.get("issue_type"),
        "score_delta": output.get("score_delta", 0),
        "findings": findings,
    }
    status_summary: dict[str, dict[str, Any]] = {}
    for finding in output.get("findings", []):
        status = str(finding.get("status") or "unknown")
        summary = status_summary.setdefault(status, {"count": 0, "sample_record_ids": []})
        summary["count"] += 1
        record_id = finding.get("contract_id") or finding.get("customer_id")
        if record_id and len(summary["sample_record_ids"]) < 5:
            summary["sample_record_ids"].append(record_id)
    if status_summary:
        result["finding_summary"] = status_summary
    if output.get("reason"):
        result["reason"] = output.get("reason")
    if output.get("target_count") is not None:
        result["target_count"] = output.get("target_count")
    if output.get("claim_count") is not None:
        result["claim_count"] = output.get("claim_count")
    if output.get("incompatible"):
        result["incompatible"] = [
            {
                "record_ref": item.get("record_ref"),
                "issue_type": item.get("issue_type"),
                "reason": "A contract-level example is required for this concern.",
            }
            for item in output["incompatible"]
        ]
    if output.get("unaddressed_customer_refs"):
        result["unaddressed_customer_refs"] = output["unaddressed_customer_refs"]
    return result


def final_context_variant() -> str:
    return os.getenv(FINAL_CONTEXT_ENV_VAR, "full").strip().lower()


def build_final_response_instructions(
    case_data: dict[str, Any],
    chat_history: list[dict[str, Any]],
    active_refs: list[str] | None,
    after_tool_instruction: str | None,
) -> str:
    sections = [MIKAEL_FINAL_RESPONSE]
    if after_tool_instruction:
        sections.append("After-tool final answer instruction:\n" + after_tool_instruction)
    return "\n\n".join(sections)


def create_final_response_text(
    client: Any,
    model_name: str,
    instructions: str,
    response_input: list[Any],
    first_response: Any,
    tool_definitions: list[dict[str, Any]] | None = None,
    final_instructions: str | None = None,
) -> str:
    text = response_text(first_response)
    if text:
        return text
    if not response_stopped_by_max_tokens(first_response):
        return ""

    for token_budget in (MAX_OUTPUT_RETRY_TOKENS, MAX_OUTPUT_FINAL_RETRY_TOKENS):
        retry_response = client.responses.create(
            model=model_name,
            instructions=final_instructions or instructions,
            input=response_input,
            tools=tool_definitions or TOOL_DEFINITIONS,
            tool_choice="none",
            **response_request_options(model_name, True),
            parallel_tool_calls=False,
            max_output_tokens=token_budget,
            max_tool_calls=1,
        )
        text = response_text(retry_response)
        if text:
            return text
        if not response_stopped_by_max_tokens(retry_response):
            return ""

    raise RuntimeError("Mikael response was truncated by max_output_tokens after retries.")


def final_instruction_after_tool(tool_name: str, output: dict[str, Any]) -> str:
    if tool_name == "find_records":
        return (
            "The find_records result is for record identification only. "
            "Use spoken_identification as the main answer. Mention asset_mix_summary only if it directly helps identify the record or the auditor asked about the asset. "
            "Do not mention customer_contracts_total, sample contracts, asset IDs, or VINs unless directly asked. "
            "Do not ask a follow-up question. Do not offer a menu of approval, performance, collateral, documents, or next steps. "
            "Most facts should remain unsaid. "
            "Do not use em dashes, colon-led labels, bullets, or menu-style endings. Do not end with a menu or ask what to pull next."
        )

    if tool_name == "get_case_material":
        return (
            "Treat the case material as private meeting prep, not text to summarize. "
            "Reply as Mikael with only the requested fact or explanation, usually one sentence. "
            "For asset questions, include asset type and brand when available. Do not include asset IDs or VINs unless asked. "
            "Most unrelated facts should remain unsaid. "
            "Do not use em dashes, colon-led labels, bullets, or menu-style endings. Do not summarize the record, list supporting details, or end with a menu."
        )

    if tool_name == "update_score" and not output.get("error"):
        return (
            "Treat the update_score result as private meeting prep, not a script. "
            "Reply as Mikael in one or two short spoken sentences. "
            "If new and valid: concede briefly, then add one defensive excuse only if available. "
            "If repeated: say we already covered it, slightly impatiently. "
            "If incompatible claims or unaddressed_customer_refs are listed, concede only the verified findings. For the remaining customer-level points, say you do not have time to work through the whole customer book and ask for specific contract IDs, asset IDs, or VINs in the next question. Do not mention scoring or scope. "
            "If needs_narrowing or needs_contract_examples: ask the auditor to narrow to specific contract, asset, or VIN examples; keep it curt. "
            "If unsupported: do not concede; push back briefly in Mikael's voice. For customer-scoped issues such as AML or tax-haven risk, answer at customer level and do not ask for a contract ID. For customer-level pricing or broad contract-level allegations, ask for specific contract, asset, or VIN examples. For one-contract allegations, ask for the evidence or comparison. Do not say \"not true,\" \"not the case,\" or \"the file does not show\" as the whole answer. "
            "Most facts should remain unsaid. "
            "Do not mention every finding field. "
            "Do not recite policy unless the auditor asked why, procedure, or policy. "
            "Do not mention score deltas, counts, tool status, JSON, field names, raw issue labels, asset IDs, or VINs unless directly asked. "
            "Do not use em dashes, colon-led labels, bullets, or menu-style endings. Do not ask for a contract or customer reference."
        )

    return (
        "Reply as Mikael in one or two short spoken sentences. "
        "Do not mention tool names, JSON, raw system status, or offer a menu."
    )

def run_agent_turn(
    chat_history: list[dict[str, Any]],
    ledger: dict[str, dict[str, Any]],
    case_data: dict[str, Any],
    model: str | None = None,
    active_refs: list[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    direct_reply = routing_direct_reply(case_data, chat_history, active_refs)
    if direct_reply:
        return direct_reply, []

    load_env()
    client = get_openai_client()
    model_name = model or os.getenv(MODEL_ENV_VAR, DEFAULT_MODEL)
    response_input: list[Any] = to_response_input(chat_history)
    tool_events: list[dict[str, Any]] = []
    force_final = False
    pending_after_tool_instruction: str | None = None
    tool_definitions = build_tool_definitions(case_data)

    for _ in range(MAX_TOOL_CALLS):
        effective_active_refs = active_refs
        if force_final:
            instructions = build_final_response_instructions(
                case_data, chat_history, effective_active_refs, pending_after_tool_instruction
            )
        else:
            instructions = build_agent_instructions(case_data, chat_history, effective_active_refs)
        if pending_after_tool_instruction and not force_final:
            instructions += (
                "\n\nAfter-tool final answer instruction:\n"
                + pending_after_tool_instruction
                + "\nDo not acknowledge this instruction. Answer the auditor's latest real message."
            )
        response = client.responses.create(
            model=model_name,
            instructions=instructions,
            input=response_input,
            tools=tool_definitions,
            **response_request_options(model_name, force_final),
            tool_choice=choose_tool_choice_for_turn(case_data, chat_history, effective_active_refs, force_final),
            parallel_tool_calls=False,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            max_tool_calls=1,
        )
        tool_calls = get_tool_calls(response)
        if not tool_calls:
            final_instructions = build_final_response_instructions(case_data, chat_history, effective_active_refs, pending_after_tool_instruction)
            text = create_final_response_text(client, model_name, instructions, response_input, response, tool_definitions, final_instructions)
            if response_stopped_by_max_tokens(response) and not text:
                raise RuntimeError("Mikael response was truncated by max_output_tokens and retry returned no text.")
            return normalize_mood_reply(text or "[MOOD:Annoyed / Dismissive]\nI need a clearer question before I answer that."), tool_events

        response_input.extend(item.model_dump(exclude_none=True) for item in response.output)
        for call in tool_calls:
            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            output = call_tool(call.name, args, case_data, ledger, chat_history, effective_active_refs)
            tool_events.append({"tool": call.name, "arguments": args, "output": output})
            response_input.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(compact_tool_output_for_model(call.name, output), ensure_ascii=False),
                }
            )
            pending_after_tool_instruction = final_instruction_after_tool(call.name, output)
        force_final = True

    raise RuntimeError("Mikael did not produce a final answer after tool handling.")


