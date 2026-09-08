"""Small Streamlit app for testing the refactored auditor chat flow."""

from __future__ import annotations

import base64
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import streamlit as st
from openai import OpenAI

ROOT = Path(__file__).resolve().parent
CORE = ROOT / "main" / "core"
VISUAL_MODEL = "gpt-5.6"
PARSER_MODEL = "gpt-4.1"
GENERATOR_MODEL = "gpt-4.1-mini"
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT))

from stage_02_visual_extraction import extract_visible_entities  # noqa: E402
import stage_08_audit_pipeline  # noqa: E402
from conversation_state import ConversationState  # noqa: E402
from run_chat_flow import (  # noqa: E402
    GENERATOR_PROMPT,
    PARSER_SCHEMA,
    load_local_env,
)

stage_08_audit_pipeline = importlib.reload(stage_08_audit_pipeline)
run_conversation_turn = stage_08_audit_pipeline.run_conversation_turn


load_local_env()
st.set_page_config(page_title="Auditor Chat Test", page_icon="🔎", layout="wide")


@st.cache_data
def load_graph(graph_mtime_ns: int) -> dict[str, Any]:
    graph_path = ROOT / "output" / "case_graph.json"
    if not graph_path.exists():
        raise FileNotFoundError("case_graph.json is missing")
    return json.loads(graph_path.read_text(encoding="utf-8"))


@st.cache_resource
def get_client() -> OpenAI:
    return OpenAI()


def parser_call(**payload: Any) -> str:
    retry_instruction = payload.pop("retry_instruction", "")
    if retry_instruction:
        payload["retry_instruction"] = retry_instruction
    response = get_client().responses.create(
        model=os.getenv("AUDIT_PARSER_MODEL", PARSER_MODEL),
        instructions=(CORE / "prompts" / "stage_03_request_parser_prompt.md").read_text(encoding="utf-8"),
        input=json.dumps(payload, ensure_ascii=False, indent=2),
        text={"format": PARSER_SCHEMA},
        max_output_tokens=6000,
    )
    raw = response.output_text or ""
    st.session_state.last_llm1_raw = raw
    return raw


def visual_call(image: Any) -> str:
    encoded = base64.b64encode(image.getvalue()).decode("ascii")
    response = get_client().responses.create(
        model=os.getenv("AUDIT_VISUAL_MODEL", VISUAL_MODEL),
        instructions=(CORE / "prompts" / "stage_02_visual_extraction_prompt.md").read_text(encoding="utf-8"),
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Extract the complete visible table."},
                {"type": "input_image", "image_url": f"data:{image.type};base64,{encoded}"},
            ],
        }],
        max_output_tokens=6000,
    )
    return response.output_text or ""


def generate_reply(message: str, result: dict[str, Any], case_data: dict[str, Any]) -> str:
    reply_context = compact_reply_data(result, case_data)
    st.session_state.last_debug["llm2_input"] = reply_context
    response = get_client().responses.create(
        model=os.getenv("AUDIT_GENERATOR_MODEL", GENERATOR_MODEL),
        instructions=GENERATOR_PROMPT.read_text(encoding="utf-8"),
        input=json.dumps({
            "latest_auditor_message": message,
            "python_result": reply_context,
        }, ensure_ascii=False, indent=2),
        text={
            "format": {
                "type": "json_schema",
                "name": "mikael_response",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"speech": {"type": "string"}},
                    "required": ["speech"],
                },
            }
        },
        max_output_tokens=1200,
    )
    raw = (response.output_text or "").strip()
    st.session_state.last_debug["llm2_raw"] = raw
    st.session_state.last_debug["llm2_status"] = getattr(response, "status", None)
    st.session_state.last_debug["llm2_incomplete_details"] = str(
        getattr(response, "incomplete_details", None)
    )
    st.session_state.last_debug["llm2_output_types"] = [
        getattr(item, "type", type(item).__name__)
        for item in (getattr(response, "output", None) or [])
    ]
    if not raw:
        raise RuntimeError("LLM2 returned an empty response.")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("LLM2 returned invalid JSON.") from exc
    speech = value.get("speech") if isinstance(value, dict) else None
    if not isinstance(speech, str) or not speech.strip():
        raise RuntimeError("LLM2 response did not contain speech.")
    return speech.strip()


def compact_reply_data(result: dict[str, Any], case_data: dict[str, Any]) -> dict[str, Any]:
    """Keep only the entity nodes and issue narratives needed for the reply."""
    filtered = result.get("filtered_data", {})
    issue_names = {
        str(item.get("name", "")).upper()
        for item in filtered.get("customer_concerns", [])
        + filtered.get("contract_concerns", [])
        if item.get("name")
    }
    scoring = result.get("scoring") or {}
    compact: dict[str, Any] = {"status": scoring.get("status") or result.get("status")}
    requested_issues = (result.get("request") or {}).get("requested_concerns", [])
    if requested_issues:
        compact["requested_issue"] = requested_issues[0]
    if result.get("clarification_type"):
        compact["clarification_type"] = result["clarification_type"]
    if result.get("options") and result.get("clarification_type") != "choose_concern":
        compact["options"] = result["options"]
    if result.get("missing"):
        compact["missing"] = result["missing"]
    if issue_names:
        compact["issues"] = [
            *[
                item for item in filtered.get("customer_concerns", [])
                if str(item.get("name", "")).upper() in issue_names
            ],
            *[
                item for item in filtered.get("contract_concerns", [])
                if str(item.get("name", "")).upper() in issue_names
            ],
        ]
    starts = filtered.get("starting_points", [])
    start_types = {item.get("type") for item in starts}
    if "contract" in start_types or not start_types:
        compact["contracts"] = [
            {"id": contract_id}
            for contract_id in filtered.get("contracts", [])
        ]
    elif "customer" in start_types:
        compact["customers"] = [
            {
                "id": customer_id,
                "name": case_data.get("customers", {}).get(customer_id, {}).get("customer_name", ""),
                "public_narrative": case_data.get("customers", {}).get(customer_id, {}).get("public_description", ""),
            }
            for customer_id in filtered.get("customers", [])
        ]
    elif "asset" in start_types:
        compact["assets"] = [{"id": asset_id} for asset_id in filtered.get("assets", [])]
    elif "vin" in start_types:
        compact["vins"] = [{"id": vin} for vin in filtered.get("vins", [])]
    entity_count = sum(
        len(compact.get(kind, []))
        for kind in ("customers", "contracts", "assets", "vins")
    )
    if entity_count > 1:
        compact["entity_scope"] = "group"
    return compact


def init_state() -> None:
    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("last_debug", {})
    st.session_state.setdefault("ledger", {})
    if "conversation_state" not in st.session_state:
        st.session_state.conversation_state = ConversationState()


def render_score_ledger(ledger: dict[str, Any], target: Any = st) -> None:
    with target.container():
        st.divider()
        st.subheader("Score ledger")
        if not ledger:
            st.caption("No scored issues yet.")
            return
        rows = []
        for entry in ledger.values():
            rows.append({
                "Team": entry.get("team", "default"),
                "Issue type": entry.get("issue_type", ""),
                "Contract ID": entry.get("contract_id", ""),
                "Customer ID": entry.get("customer_id", ""),
                "Score count": 1,
            })
        issue_count = len({row["Issue type"] for row in rows if row["Issue type"]})
        contract_count = len({row["Contract ID"] for row in rows if row["Contract ID"]})
        st.metric("Total score", issue_count * contract_count)
        st.dataframe(rows, hide_index=True, width="stretch")

        summary: dict[tuple[str, str], int] = {}
        for row in rows:
            key = (row["Team"], row["Issue type"])
            summary[key] = summary.get(key, 0) + row["Score count"]
        st.markdown("**Issue totals**")
        st.dataframe([
            {
                "Team": team,
                "Issue type": issue,
                "Contracts counted": count,
                "Score total": count,
            }
            for (team, issue), count in summary.items()
        ], hide_index=True, width="stretch")


def render_turn_debug(before: dict[str, Any], result: dict[str, Any]) -> None:
    after = result.get("conversation_state")
    after = after.to_dict() if after else before
    before_confirmation = before.get("pending_confirmation")
    after_confirmation = after.get("pending_confirmation")
    if isinstance(before_confirmation, dict) and "request_type" in before_confirmation:
        before_confirmation = {
            "kind": result.get("clarification_type"),
            "candidates": before_confirmation.get("requested_concerns", []),
        }
    if isinstance(after_confirmation, dict) and "request_type" in after_confirmation:
        after_confirmation = {
            "kind": result.get("clarification_type"),
            "candidates": result.get("options", []),
        }
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f'USER: "{result.get("request", {}).get("message", "")}"',
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"[entity]     {before.get('focus_entities')}  →  {after.get('focus_entities')}",
        f"[topic]      {before.get('focus_topic')}  →  {after.get('focus_topic')}",
        f"[confirm]    {before_confirmation}  →  {after_confirmation}",
        f"[raw_data]   {'reused' if result.get('request', {}).get('starting_points') and before.get('last_raw_data') else 'not reused'}",
        f"[score]      {((result.get('scoring') or {}).get('status') or 'not run')}  "
        f"score={((result.get('scoring') or {}).get('score', 0))}  "
        f"delta={((result.get('scoring') or {}).get('score_delta', 0))}  "
        f"ledger_entries={len(st.session_state.ledger)}",
        "──────────────────────────────────────",
        f"RESPONSE: {result.get('reply', '')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    st.code("\n".join(lines), language="text")


def main() -> None:
    init_state()
    st.title("Auditor chat flow")
    st.caption("Visual parser → LLM1 → Python graph filter → Mikael")
    if st.sidebar.button("Clear conversation"):
        st.session_state.messages = []
        st.session_state.last_debug = {}
        st.session_state.ledger = {}
        st.session_state.conversation_state = ConversationState()
        st.rerun()
    debug = st.sidebar.checkbox("Show internal flow", value=False)

    try:
        graph_path = ROOT / "output" / "case_graph.json"
        case_data = load_graph(graph_path.stat().st_mtime_ns)
    except FileNotFoundError:
        st.error("case_graph.json is missing. Run stage_00_build_case_graph.py first.")
        st.stop()

    for item in st.session_state.messages:
        with st.chat_message(item["speaker"]):
            st.write(item["content"])

    ledger_area = st.empty()
    render_score_ledger(st.session_state.ledger, ledger_area)

    prompt = st.chat_input(
        "Ask the auditor question or attach a screenshot",
        accept_file=True,
        file_type=["png", "jpg", "jpeg", "webp"],
    )
    if not prompt:
        return

    message = prompt.text.strip()
    uploaded = prompt.files[0] if prompt.files else None
    image_text = None
    if uploaded:
        with st.spinner("Reading screenshot..."):
            image_text = extract_visible_entities(uploaded, visual_call)
        st.session_state.last_debug["visual_text"] = image_text

    if not message and not image_text:
        st.warning("Please write a question or attach an image.")
        return

    visible_message = message or "Please inspect the attached screenshot."
    st.session_state.messages.append({
        "message_number": len(st.session_state.messages) + 1,
        "speaker": "auditor",
        "content": visible_message,
        "image_text": image_text or "",
    })
    with st.chat_message("auditor"):
        st.write(visible_message)
        if uploaded:
            st.image(uploaded, width="stretch")

    with st.spinner("Checking the request..."):
        state_before = st.session_state.conversation_state.to_dict()
        ledger_for_turn = dict(st.session_state.ledger)
        result = run_conversation_turn(
            visible_message,
            case_data,
            parser_call=parser_call,
            latest_messages=[
                {
                    "message_number": item["message_number"],
                    "role": item["speaker"],
                    "content": item["content"],
                    "image_text": item.get("image_text", ""),
                }
                for item in st.session_state.messages[:-1]
            ],
            conversation_state=st.session_state.conversation_state,
            known_concern_names=[
                {"name": name, **definition}
                for name, definition in case_data.get("concern_catalog", {}).items()
            ],
            image_text=image_text,
            ledger=ledger_for_turn,
        )
    st.session_state.ledger = dict(ledger_for_turn)
    result.setdefault("request", {})["message"] = visible_message
    st.session_state.last_debug["python_result"] = result

    with st.spinner("Preparing Mikael's reply..."):
        try:
            reply = generate_reply(visible_message, result, case_data)
        except RuntimeError as exc:
            st.error(str(exc))
            if debug:
                with st.expander("Internal flow", expanded=True):
                    render_turn_debug(state_before, {**result, "reply": "ERROR: " + str(exc)})
            return
    st.session_state.messages.append({
        "message_number": len(st.session_state.messages) + 1,
        "speaker": "mikael",
        "content": reply,
    })
    st.session_state.conversation_state = result.get("conversation_state", st.session_state.conversation_state)
    st.session_state.conversation_state.last_raw_data = result.get("filtered_data", {})
    st.session_state.conversation_state.add_turn(visible_message, reply)
    with st.chat_message("mikael"):
        st.write(reply)

    if debug:
        with st.expander("Internal flow", expanded=True):
            render_turn_debug(state_before, {**result, "reply": reply})

    ledger_area.empty()
    render_score_ledger(ledger_for_turn, ledger_area)


if __name__ == "__main__":
    main()
