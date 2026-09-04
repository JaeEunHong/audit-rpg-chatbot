import base64
import html
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CORE_DIR = ROOT / "main" / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import streamlit as st

from audit_rpg import DATA_PATH, DEFAULT_MODEL, ENTITY_MASTER_PATH, active_refs_after_turn, extract_mood, get_scorecard, image_to_data_url, interview_state_for_mood, load_case_data, load_env, should_require_tool, small_talk_reply
from run_experiment import contracts_for_customer, run_investigation



def run_agent_turn(
    messages, score_ledger, data, model=None, active_refs=None,
    active_investigation_scope=None,
):
    latest_user = next((item for item in reversed(messages) if item.get("role") == "user"), {})
    result = run_investigation(
        str(latest_user.get("content") or ""),
        data,
        score_ledger,
        model or os.getenv("AUDIT_RPG_MODEL", "gpt-4.1"),
        image_data_urls=list(latest_user.get("images") or []),
        chat_history=messages,
        active_investigation_scope=active_investigation_scope,
    )
    visual_text = str(result.get("visual_extraction_text") or "").strip()
    if visual_text and latest_user is not None:
        latest_user["visual_extraction_text"] = visual_text
    events = []
    if visual_text:
        events.append({"tool": "visual_parser", "output": {"markdown_table": visual_text}})
    if result.get("request"):
        events.append({"tool": "llm1_parser", "output": result["request"]})
    if result.get("status") == "lookup":
        events.append({"tool": "find_records", "output": {"status": "lookup", "records": result.get("records", [])}})
    score_result = result.get("score_result") or {}
    if score_result.get("findings"):
        events.append({"tool": "update_score", "output": score_result})
    events.append({"tool": "llm2_generator", "output": {"reply": result["reply"]}})
    return result["reply"], events, result.get("active_investigation_scope", active_investigation_scope)
st.set_page_config(page_title="Nordovia Audit RPG", page_icon=":material/search:", layout="wide", initial_sidebar_state="expanded")

load_env()

def require_app_password() -> None:
    expected = os.getenv("APP_PASSWORD", "")
    if not expected:
        st.error("APP_PASSWORD is not configured.")
        st.stop()
    if st.session_state.get("authenticated", False):
        return

    st.markdown(
        '<style>.auth-title{font-size:28px;font-weight:600;margin-bottom:18px;text-align:center;}[data-testid="stForm"]{width:100%;max-width:480px;margin:0 auto;box-sizing:border-box;border:1px solid #D9D6CE;border-radius:12px;padding:18px 18px 16px;background:#F7F5F0;}[data-testid="stFormSubmitButton"] button{background:#3F5F6F !important;border-color:#3F5F6F !important;color:#FFFFFF !important;}[data-testid="stFormSubmitButton"] button:hover{background:#304B58 !important;border-color:#304B58 !important;}</style>',

        unsafe_allow_html=True,
    )
    _auth_left, auth_center, _auth_right = st.columns([1, 1, 1])
    with auth_center:
        st.markdown("<div class='auth-title'>Audit interview</div>", unsafe_allow_html=True)
        with st.form("app_password_form"):
            entered = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Enter", type="primary", use_container_width=True)
    if submitted:
        if entered == expected:
            st.session_state.authenticated = True
            st.rerun()
        st.error("Incorrect password.")
    st.stop()


require_app_password()

ASSET_DIR = ROOT / "assets"
MIKAEL_DEFAULT_IMAGE = ASSET_DIR / "Confident.png"
AUDITOR_AVATAR = str(ASSET_DIR / "auditor_profile.png")
MIKAEL_AVATAR = str(ASSET_DIR / "mikael_profile.png")
MIKAEL_MOOD_IMAGES = {
    "Professional / Controlled": (ASSET_DIR / "Confident.png", "Confident"),
    "Guarded / Hesitant": (ASSET_DIR / "Concerned.png", "Concerned"),
    "Defensive / Cornered": (ASSET_DIR / "Defensive.png", "Defensive"),
    "Reluctant / Defeated": (ASSET_DIR / "Conceding.png", "Conceding"),
    "Annoyed / Dismissive": (ASSET_DIR / "Arrogant.png", "Arrogant"),
    "Checking Records": (ASSET_DIR / "Checking_records.png", "Checking records"),
}
STATUS_MESSAGES = [
    (0.0, "Mikael is checking something."),
    (3.5, "Mikael is clicking around the system."),
]
RECORD_STATUS_MESSAGES = STATUS_MESSAGES
st.session_state.setdefault("messages", [])
st.session_state.setdefault("score_ledger", {})
st.session_state.setdefault("tool_events", [])
st.session_state.setdefault("current_mood", "Professional / Controlled")
st.session_state.setdefault("active_refs", [])
st.session_state.setdefault(
    "active_investigation_scope",
    {"contracts": [], "customers": [], "assets": [], "vins": []},
)
st.session_state.setdefault("pending_agent_turn", False)
st.session_state.setdefault("pending_record_work", False)
st.session_state.setdefault("pending_audit_toasts", [])
st.session_state.setdefault("show_deck_placeholder", False)
st.session_state.setdefault("app_page", "chat")
st.session_state.setdefault("team_id", "")
st.session_state.setdefault("team_name", "")

def restore_score_ledger_from_messages() -> None:
    """Recover verified findings if Streamlit retained chat but lost the ledger."""
    ledger = st.session_state.get("score_ledger")
    if ledger:
        return

    recovered: dict[str, dict[str, Any]] = {}
    events = list(st.session_state.get("tool_events", []))
    for message in st.session_state.get("messages", []):
        events.extend(message.get("tool_events", []))

    for event in events:
        if event.get("tool") != "update_score":
            continue
        output = event.get("output") or {}
        for finding in output.get("findings", []):
            if finding.get("status") != "new_score":
                continue
            record_id = str(finding.get("record_id") or "").strip()
            issue_key = str(finding.get("issue_key") or "").strip()
            if record_id and issue_key:
                recovered[f"{record_id}::{issue_key}"] = finding

    if recovered:
        st.session_state.score_ledger = recovered


restore_score_ledger_from_messages()


@st.cache_data(show_spinner=False)
def cached_case_data(data_mtime: float, entity_mtime: float):
    return load_case_data(DATA_PATH, ENTITY_MASTER_PATH)

@st.cache_data(show_spinner=False)
def image_data_url(path_text: str, image_mtime: float) -> str:
    path = Path(path_text)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


entity_mtime = ENTITY_MASTER_PATH.stat().st_mtime if ENTITY_MASTER_PATH.exists() else 0.0
case_data = cached_case_data(DATA_PATH.stat().st_mtime, entity_mtime)
scorecard = get_scorecard(st.session_state.score_ledger)



CUSTOMER_SCOPED_ISSUES = {
    "AML RISK",
    "CUSTOMER IN TAX HAVEN",
    "CONNECTED CUSTOMER EXPOSURE HIDDEN BY SEPARATE CUSTOMER IDS",
}


def select_page(page: str) -> None:
    st.session_state.app_page = page
    st.rerun()


def render_home_page() -> None:
    st.markdown("## Nordovia Audit")
    st.caption("Team audit workspace")
    st.markdown("### Who is Mikael?")
    st.write("Mikael von Geld is the senior credit manager answering for the portfolio.")
    st.markdown("### How to play")
    st.write("Choose a team, then raise a concrete concern about a contract or customer. Verified findings count for your team.")
    try:
        teams = list_teams()
    except Exception as exc:
        st.error(str(exc))
        return
    selected = render_team_picker(teams, create_team)
    if selected:
        st.session_state.team_id = selected["team_id"]
        st.session_state.team_name = selected["team_name"]
        st.session_state.app_page = "chat"
        st.rerun()
    if st.session_state.get("team_id"):
        st.info(f"Current team: {st.session_state.team_name}")
        home_chat, home_facilitator = st.columns(2)
        with home_chat:
            if st.button("Open chat", use_container_width=True):
                select_page("chat")
        with home_facilitator:
            if st.button("Facilitator view", use_container_width=True):
                select_page("facilitator")


@st.fragment(run_every=10)
def render_facilitator_page(case_data: dict[str, Any]) -> None:
    try:
        teams = list_teams()
        rows = leaderboard_rows()
    except Exception as exc:
        st.error(str(exc))
        return
    st.markdown("## Facilitator")
    st.caption("Leaderboard refreshes every 10 seconds.")
    render_leaderboard(rows, teams, case_data)


model_name = os.getenv("AUDIT_RPG_MODEL", DEFAULT_MODEL)
show_tools = False
st.markdown(
    """
    <style>
:root {
    --audit-bg: #F7F5F0;
    --audit-panel: #EFEEE9;
    --audit-text: #20242A;
    --audit-muted: #667085;
    --audit-line: #DDD8CF;
    --audit-accent: #3F5F6F;
    --audit-warm: #B7794A;
    --audit-input: #F1F4F7;
    --audit-font: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.stApp,
[data-testid="stAppViewContainer"],
[data-testid="stMain"],
[data-testid="stMain"] > div,
[data-testid="stHeader"] {
    background: var(--audit-bg);
    color: var(--audit-text);
}
[data-testid="stHeader"] {
    background: transparent !important;
}
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
[data-testid="stMainMenu"],
[data-testid="stDeployButton"] {
    opacity: 0.06;
}
[data-testid="collapsedControl"],
[data-testid="stSidebarCollapseButton"] {
    opacity: 0.82 !important;
}
[data-testid="collapsedControl"] button,
[data-testid="stSidebarCollapseButton"] button {
    color: rgba(63, 95, 111, 0.82) !important;
}
[data-testid="collapsedControl"] svg,
[data-testid="stSidebarCollapseButton"] svg {
    opacity: 1 !important;
    stroke: rgba(63, 95, 111, 0.9) !important;
}
html,
body,
.stApp {
    overflow-x: hidden;
}
.block-container,
[data-testid="stMainBlockContainer"] {
    box-sizing: border-box;
    width: min(100%, 1240px) !important;
    max-width: 1240px !important;
    margin: 0 auto !important;
    padding: 62px 56px 56px !important;
    overflow: visible !important;
    text-wrap: balance;
}
html,
body,
.stApp,
[data-testid="stAppViewContainer"],
[data-testid="stMain"],
[data-testid="stSidebar"],
[data-testid="stMarkdownContainer"],
[data-testid="stWidgetLabel"],
[data-testid="stCaptionContainer"],
[data-testid="stTextInput"] input,
[data-testid="stChatInput"] textarea,
[data-testid="stButton"] button,
[data-baseweb="tab"],
[data-baseweb="select"] * {
    font-family: var(--audit-font) !important;
    letter-spacing: 0;
}
[data-testid="stHorizontalBlock"] {
    gap: 64px;
}
.page-head {
    display: block;
    width: 100%;
    overflow: visible !important;
    padding: 10px 0 0;
    margin: 0 0 38px;
}
.page-head .app-title {
    display: block;
    font-family: Constantia, Georgia, "Times New Roman", serif !important;
    font-size: clamp(32px, 2.25vw, 38px) !important;
    font-weight: 700;
    letter-spacing: 0;
    line-height: 1.12;
    color: var(--audit-text);
    margin: 0 0 10px;
    padding: 0;
    max-width: 100%;
    overflow: visible !important;
}
.page-subtitle-pill {
    color: var(--audit-muted);
    font-size: 13px;
    font-weight: 400;
    line-height: 1.3;
}
.mikael-card {
    width: 100%;
    max-width: 330px;
    aspect-ratio: 16 / 9;
    border-radius: 8px;
    overflow: hidden;
    background: transparent;
    line-height: 0;
    margin: 0 0 12px;
}
.mikael-portrait {
    display: block !important;
    width: 100% !important;
    height: 100% !important;
    object-fit: cover;
    object-position: center center;
}
.interview-status {
    display: flex;
    align-items: center;
    width: 100%;
    max-width: 330px;
    margin: 0 0 18px;
    color: var(--audit-muted);
    font-size: 11.5px;
    line-height: 1;
    white-space: nowrap;
}
.status-item {
    display: inline-flex;
    align-items: center;
    height: 24px;
    gap: 5px;
    padding: 0 9px;
    border: 1px solid rgba(221, 216, 207, 0.95);
    border-radius: 999px;
    background: rgba(251, 250, 248, 0.66);
}
.status-label {
    color: var(--audit-muted);
    font-weight: 500;
}
.status-value {
    color: var(--audit-text);
    font-weight: 600;
}
.briefing-panel {
    width: 100%;
    max-width: 330px;
    border-top: 1px solid rgba(221, 216, 207, 0.8);
    padding: 14px 0 0;
    margin: 0;
    background: transparent;
}
.brief-label,
.play-area-label {
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.045em;
    color: var(--audit-accent);
    text-transform: uppercase;
}
.brief-label {
    margin-bottom: 7px;
}
.brief-body {
    font-size: 13.5px;
    font-weight: 400;
    line-height: 1.38;
    color: var(--audit-muted);
}
.play-area-label {
    max-width: 660px;
    border-top: 1px solid rgba(221, 216, 207, 0.8);
    padding-top: 14px;
    margin-bottom: 8px;
}
.clear-chat-control,
.st-key-clear_chat_control {
    max-width: 660px;
    margin: -7px 0 2px;
    padding: 0 !important;
    text-align: right;
}
.st-key-clear_chat_control [data-testid="stVerticalBlock"],
.st-key-clear_chat_control [data-testid="stElementContainer"] {
    gap: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
}
.clear-chat-control [data-testid="stButton"] button,
.st-key-clear_chat_control [data-testid="stButton"] button {
    min-height: 18px !important;
    height: 18px !important;
    line-height: 18px !important;
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
    color: rgba(102, 112, 133, 0.48) !important;
    font-size: 10.5px !important;
    font-weight: 400 !important;
    box-shadow: none !important;
}
.clear-chat-control [data-testid="stButton"] button:hover,
.st-key-clear_chat_control [data-testid="stButton"] button:hover {
    background: transparent !important;
    color: rgba(63, 95, 111, 0.82) !important;
}
.chat-shell {
    max-width: 660px;
}
.chat-thread {
    height: min(300px, calc(100vh - 500px));
    min-height: 260px;
    overflow-y: auto;
    padding: 2px 8px 2px 0;
    margin: 0;
}
.chat-thread.empty-thread {
    height: 86px;
    min-height: 86px;
    overflow: visible;
}
.chat-thread::-webkit-scrollbar {
    width: 6px;
}
.chat-thread::-webkit-scrollbar-thumb {
    background: rgba(63, 95, 111, 0.22);
    border-radius: 999px;
}
.chat-empty {
    color: var(--audit-muted);
    font-size: 13.5px;
    line-height: 1.4;
    padding: 4px 0 8px;
}
.chat-row {
    display: grid;
    grid-template-columns: 30px minmax(0, 1fr);
    gap: 8px;
    align-items: start;
    max-width: 660px;
    margin: 0 0 12px;
}
.chat-row.grouped {
    margin-top: -2px;
    margin-bottom: 10px;
}
.chat-avatar {
    width: 30px;
    height: 30px;
    border-radius: 50%;
    object-fit: cover;
    opacity: 0.9;
}
.chat-speaker {
    color: rgba(102, 112, 133, 0.95);
    font-size: 12px;
    font-weight: 600;
    line-height: 1.2;
    margin: 0 0 3px;
}
.chat-row.grouped .chat-speaker {
    opacity: 0.72;
}
.chat-message-body {
    display: inline-block;
    max-width: 610px;
    border: 1px solid rgba(221, 216, 207, 0.72);
    border-radius: 8px;
    padding: 7px 10px;
    font-size: 14.5px;
    font-weight: 400;
    line-height: 1.42;
    color: var(--audit-text);
}
.chat-row.user .chat-message-body {
    background: rgba(241, 244, 247, 0.92);
    border-color: rgba(211, 217, 224, 0.88);
}
.chat-row.assistant .chat-message-body {
    background: rgba(250, 249, 246, 0.78);
    border-color: rgba(221, 216, 207, 0.68);
}
.chat-shot {
    display: block;
    width: min(280px, 100%) !important;
    max-width: 280px !important;
    height: auto !important;
    max-height: 190px;
    object-fit: contain;
    object-position: left top;
    border-radius: 7px;
    margin-top: 7px;
}
.chat-image-hint {
    color: var(--audit-muted);
    font-size: 11px;
    line-height: 1.3;
    margin-top: 4px;
}
.screenshot-guidance {
    color: var(--audit-muted);
    font-size: 11px;
    line-height: 1.35;
    margin-top: 8px;
    max-width: 280px;
}
.chat-status-row {
    display: grid;
    grid-template-columns: 30px minmax(0, 1fr);
    gap: 8px;
    align-items: center;
    max-width: 660px;
    margin: 0 0 8px;
}
.chat-status-body {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    color: var(--audit-muted);
    font-size: 13px;
    line-height: 1.35;
    padding: 4px 0;
}
.chat-status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: rgba(183, 121, 74, 0.62);
    animation: auditPulse 1.2s ease-in-out infinite;
}
@keyframes auditPulse {
    0%, 100% { opacity: 0.35; }
    50% { opacity: 0.95; }
}
[data-testid="stChatInput"] {
    max-width: 660px;
    position: sticky;
    bottom: 0;
    z-index: 4;
    background: linear-gradient(180deg, rgba(247, 245, 240, 0), var(--audit-bg) 30%);
    padding-top: 6px;
}
[data-testid="stChatInput"] > div {
    min-height: 46px !important;
    border-radius: 10px !important;
    border: 1px solid rgba(221, 216, 207, 0.86) !important;
    background: var(--audit-input) !important;
    box-shadow: none !important;
}
[data-testid="stChatInput"] textarea {
    font-size: 14px !important;
    font-weight: 400 !important;
    min-height: 40px !important;
    line-height: 1.35 !important;
    color: var(--audit-text) !important;
}
[data-testid="stChatInput"] button {
    width: 30px !important;
    height: 30px !important;
    min-width: 30px !important;
    border-radius: 8px !important;
    background: rgba(239, 238, 233, 0.45) !important;
    color: rgba(63, 95, 111, 0.62) !important;
    box-shadow: none !important;
}
[data-testid="stChatInput"] button:first-of-type {
    opacity: 0.38;
}
[data-testid="stSidebar"] {
    background: var(--audit-panel);
    border-right: 1px solid rgba(221, 216, 207, 0.95);
}
[data-testid="stSidebar"] > div {
    padding-top: 34px;
}
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3 {
    font-size: 16px !important;
    font-weight: 600 !important;
    line-height: 1.25 !important;
    color: var(--audit-text) !important;
}
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] input,
[data-testid="stSidebar"] button,
[data-testid="stSidebar"] [data-baseweb="select"] * {
    font-size: 13px !important;
    font-weight: 400;
    color: rgba(32, 36, 42, 0.78);
}
[data-testid="stSidebar"] button {
    font-size: 13px !important;
    font-weight: 500 !important;
    border-color: rgba(32, 36, 42, 0.14) !important;
    background: rgba(247, 245, 240, 0.48) !important;
    color: rgba(32, 36, 42, 0.74) !important;
}
[data-testid="stSidebar"] input {
    font-size: 13px !important;
    font-weight: 400 !important;
    background: rgba(247, 245, 240, 0.55) !important;
}
[data-testid="stSidebar"] [data-testid="stToggle"] label {
    color: rgba(32, 36, 42, 0.62);
}
[data-testid="stSidebar"] [data-baseweb="tab-list"] {
    gap: 0.8rem;
    border-bottom: 1px solid rgba(221, 216, 207, 0.95);
    margin: 0 0 1.1rem;
}
[data-testid="stSidebar"] [data-baseweb="tab"],
[data-testid="stSidebar"] [data-baseweb="tab"] p {
    height: 30px;
    padding: 0 0 7px;
    font-size: 13px !important;
    font-weight: 500 !important;
    color: rgba(32, 36, 42, 0.56) !important;
}
[data-testid="stSidebar"] [data-baseweb="tab"],
[data-testid="stSidebar"] [data-baseweb="tab"] > div {
    background: transparent !important;
    box-shadow: none !important;
    border: 0 !important;
}
[data-testid="stSidebar"] [aria-selected="true"],
[data-testid="stSidebar"] [aria-selected="true"] p {
    color: var(--audit-text) !important;
}
[data-testid="stSidebar"] [data-baseweb="tab"][aria-selected="true"] {
    border-bottom-color: var(--audit-accent) !important;
}
[data-baseweb="tab-highlight"] {
    height: 2px !important;
    background: var(--audit-accent) !important;
}
[data-baseweb="tab-highlight"] {
    background: var(--audit-accent) !important;
}
[data-baseweb="tab-border"] {
    background: var(--audit-line) !important;
}
.sidebar-summary {
    font-size: 12.5px;
    font-weight: 400;
    line-height: 1.35;
    color: var(--audit-muted);
    margin: -0.1rem 0 1rem;
}
.sidebar-rule {
    height: 1px;
    background: rgba(221, 216, 207, 0.95);
    margin: 1rem 0 0.8rem;
}
.audit-summary-table {
    width: 100%;
    border-collapse: collapse;
    margin: 0.2rem 0 0.9rem;
    font-size: 11.5px;
    color: var(--audit-muted);
}
.audit-summary-table th {
    padding: 0.26rem 0.12rem;
    border-bottom: 1px solid rgba(221, 216, 207, 0.9);
    color: var(--audit-muted);
    font-size: 9.5px;
    font-weight: 600;
    letter-spacing: 0.03em;
    text-align: right;
    white-space: nowrap;
}
.audit-summary-table th:first-child,
.audit-summary-table td:first-child {
    text-align: left;
}
.audit-summary-table td {
    padding: 0.32rem 0.12rem;
    border-bottom: 1px solid rgba(221, 216, 207, 0.58);
    line-height: 1.25;
    text-align: right;
    vertical-align: top;
}
.audit-summary-table td:first-child {
    width: 58%;
    color: var(--audit-text);
    font-weight: 500;
    overflow-wrap: anywhere;
}.audit-note {
    margin: 0.36rem 0 0.62rem;
}
.audit-finding-tag {
    display: inline-flex;
    align-items: center;
    height: 20px;
    max-width: 100%;
    padding: 0 7px;
    border: 1px solid rgba(63, 95, 111, 0.18);
    border-radius: 999px;
    background: rgba(63, 95, 111, 0.055);
    color: rgba(63, 95, 111, 0.92);
    font-size: 10.5px;
    font-weight: 600;
    letter-spacing: 0.01em;
    line-height: 20px;
    white-space: nowrap;
}
.audit-note-line {
    font-size: 12.5px;
    font-weight: 400;
    line-height: 1.3;
    color: var(--audit-text);
}
.audit-note-detail {
    color: var(--audit-muted);
    margin: 0.22rem 0 0 1px;
}
.deck-action {
    margin-top: 1.05rem;
}
.deck-placeholder {
    margin-top: 0.6rem;
    padding: 0.58rem 0.64rem;
    border: 1px solid rgba(221, 216, 207, 0.9);
    border-radius: 7px;
    background: rgba(247, 245, 240, 0.45);
}
.deck-placeholder-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--audit-text);
    margin-bottom: 0.15rem;
}
.deck-placeholder-body {
    font-size: 12.5px;
    line-height: 1.35;
    color: var(--audit-muted);
}
[data-testid="stToast"] {
    border: 1px solid rgba(63, 95, 111, 0.16);
    background: rgba(255, 255, 255, 0.96);
    box-shadow: none;
}
[data-testid="stToast"] [data-testid="stMarkdownContainer"] p {
    color: rgba(32, 36, 42, 0.82);
    font-size: 13px;
    line-height: 1.35;
}
@media (max-height: 820px) and (min-width: 901px) {
    .block-container,
    [data-testid="stMainBlockContainer"] {
        padding-top: 52px !important;
        padding-bottom: 48px !important;
    }
    .page-head {
        margin-bottom: 58px;
    }
    .page-head .app-title,
    h1.app-title {
        font-size: clamp(30px, 2.05vw, 34px) !important;
    }
    .mikael-card,
    .briefing-panel,
    .interview-status {
        max-width: 310px;
    }
    .chat-thread {
        height: min(390px, calc(100vh - 275px));
        min-height: 230px;
    }
}
@media (max-width: 900px) {
    .block-container,
    [data-testid="stMainBlockContainer"] {
        width: 100% !important;
        padding: 46px 20px 76px !important;
    }
    [data-testid="stHorizontalBlock"] {
        gap: 26px;
    }
    .page-head {
        margin-bottom: 22px;
    }
    .page-head .app-title,
    h1.app-title {
        font-size: clamp(30px, 7.5vw, 36px) !important;
        line-height: 1.08;
    }
    .mikael-card,
    .briefing-panel,
    .interview-status,
    .play-area-label,
    .chat-shell,
    .chat-thread,
    .chat-row,
    .chat-status-row,
    [data-testid="stChatInput"] {
        max-width: 100%;
    }
    .chat-thread {
        height: min(420px, calc(100vh - 330px));
    }
}
</style>
    """,
    unsafe_allow_html=True,
)




def chat_avatar(role: str) -> str:
    return AUDITOR_AVATAR if role == "user" else MIKAEL_AVATAR


def avatar_data_url(role: str) -> str:
    avatar = Path(chat_avatar(role))
    return image_data_url(str(avatar), avatar.stat().st_mtime if avatar.exists() else 0.0)
def display_chat_content(content: str) -> str:
    text = str(content or "").strip()
    if text.startswith("[MOOD:"):
        _mood_line, separator, rest = text.partition("\n")
        if separator:
            return rest.strip()
        closing = text.find("]")
        return text[closing + 1:].strip() if closing >= 0 else text
    return text

def render_mikael_panel(mood: str, interview_started: bool) -> None:
    if interview_started:
        image_path, _portrait_label = MIKAEL_MOOD_IMAGES.get(mood, MIKAEL_MOOD_IMAGES["Professional / Controlled"])
    else:
        image_path = MIKAEL_DEFAULT_IMAGE

    image_url = html.escape(image_data_url(str(image_path), image_path.stat().st_mtime if image_path.exists() else 0.0), quote=True)
    st.markdown(
        f'<div class="mikael-card"><img class="mikael-portrait" src="{image_url}" alt="Mikael von Geld portrait"></div>',
        unsafe_allow_html=True,
    )

def render_interview_status(mood: str, initial: bool = False) -> None:
    status = {"mood": "Confident"} if initial else interview_state_for_mood(mood)
    st.markdown(
        (
            "<div class='interview-status'>"
            f"<span class='status-item'><span class='status-label'>Mood</span><span class='status-value'>{html.escape(status['mood'])}</span></span>"
            "</div>"
        ),
        unsafe_allow_html=True,
    )

def html_chat_text(content: str) -> str:
    text = display_chat_content(content)
    if not text:
        return ""
    return html.escape(text).replace("\n", "<br>")


def render_chat_row(message: dict, previous_role: str | None = None) -> str:
    role = "user" if message.get("role") == "user" else "assistant"
    speaker = "Auditor" if role == "user" else "Mikael"
    grouped = " grouped" if previous_role == role else ""
    avatar_url = html.escape(avatar_data_url(role), quote=True)
    body = html_chat_text(message.get("content", ""))
    images = "".join(
        f'<img class="chat-shot" src="{html.escape(src, quote=True)}" alt="Uploaded screenshot">'
        for src in message.get("images", [])
    )
    if not body and not images:
        return ""
    return (
        f'<div class="chat-row {role}{grouped}">'
        f'<img class="chat-avatar" src="{avatar_url}" alt="{speaker} avatar">'
        '<div class="chat-content">'
        f'<div class="chat-speaker">{speaker}</div>'
        f'<div class="chat-message-body">{body}{images}</div>'
        '</div>'
        '</div>'
    )


def render_chat_thread(messages: list[dict], pending: bool = False, record_work: bool = False):
    status_slot = None
    if not messages:
        with st.container(height=300):
            st.markdown("<div class=chat-shell><div class=chat-thread empty-thread><div class=chat-empty>Start with a contract ID or customer that looks unusual.</div></div></div>", unsafe_allow_html=True)
            if pending:
                status_slot = st.empty()
        return status_slot

    previous_role = None
    with st.container(height=300):
        for message in messages:
            row = render_chat_row(message, previous_role)
            if not row:
                continue
            st.markdown(row, unsafe_allow_html=True)
            if message.get("role") == "assistant" and message.get("tool_events"):
                with st.expander("Activity", expanded=False):
                    render_compact_tool_trace(message["tool_events"])
            previous_role = "user" if message.get("role") == "user" else "assistant"
        if pending:
            status_slot = st.empty()
    return status_slot
def render_chat_status(label: str) -> str:
    avatar_url = html.escape(avatar_data_url("assistant"), quote=True)
    safe_label = html.escape(label)
    return (
        '<div class="chat-shell">'
        '<div class="chat-status-row assistant">'
        f'<img class="chat-avatar" src="{avatar_url}" alt="Mikael avatar">'
        '<div class="chat-status-body">'
        '<span class="chat-status-dot"></span>'
        f'<span>{safe_label}</span>'
        '</div>'
        '</div>'
        '</div>'
    )

def status_message_for_elapsed(elapsed_seconds: float, record_work: bool = False) -> str:
    messages = RECORD_STATUS_MESSAGES if record_work else STATUS_MESSAGES
    label = messages[0][1]
    for threshold, message in messages:
        if elapsed_seconds >= threshold:
            label = message
        else:
            break
    return label


def run_agent_turn_with_loading(*, status_slot, messages, score_ledger, data, model: str, active_refs: list[str], active_investigation_scope: dict[str, list[str]], record_work: bool):
    status_slot.markdown(render_chat_status(status_message_for_elapsed(0, record_work)), unsafe_allow_html=True)
    return run_agent_turn(
        messages,
        score_ledger,
        data,
        model=model,
        active_refs=active_refs,
        active_investigation_scope=active_investigation_scope,
    )
def title_case_issue(issue_type: str) -> str:
    return str(issue_type or "Finding").replace("_", " ").lower().title()


def unique_non_empty(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def render_compact_tool_trace(events: list[dict]) -> None:
    for event in reversed(events[-12:]):
        tool = str(event.get("tool") or "tool")
        output = event.get("output") or {}
        if tool == "visual_parser":
            st.caption("Visual parser output")
            st.code(str(output.get("markdown_table") or ""), language="markdown")
            continue
        if tool == "llm1_parser":
            st.caption("LLM1 parser output")
            st.json(output)
            continue
        if tool == "llm2_generator":
            st.caption("LLM2 generator output")
            st.code(str(output.get("reply") or ""), language="text")
            continue
        summary = output.get("score_summary") or {}
        findings = output.get("findings") or []
        status = str(output.get("status") or "completed")
        delta = summary.get("score_delta", output.get("score_delta", 0))
        issue_type = summary.get("issue_type") or output.get("issue_type")
        parts = [tool, status]
        if issue_type:
            parts.append(str(issue_type))
        parts.append(f"score {delta:+g}" if isinstance(delta, (int, float)) else f"score {delta}")
        if findings:
            counts: dict[str, int] = {}
            record_ids: list[str] = []
            for finding in findings:
                finding_status = str(finding.get("status") or "result")
                counts[finding_status] = counts.get(finding_status, 0) + 1
                record_id = str(finding.get("record_id") or "")
                if record_id and record_id not in record_ids and len(record_ids) < 5:
                    record_ids.append(record_id)
            parts.append("findings " + ", ".join(f"{name} {count}" for name, count in counts.items()))
            if record_ids:
                parts.append(", ".join(record_ids))
        st.caption(" | ".join(parts))


def audit_notes_from_events(events: list[dict]) -> list[dict]:
    notes: list[dict] = []
    for event in events:
        if event.get("tool") != "update_score":
            continue
        output = event.get("output", {})
        summary = output.get("score_summary") or {}
        findings = output.get("findings", [])
        scored = [item for item in findings if item.get("status") == "new_score"]
        repeats = [item for item in findings if item.get("status") == "repeat"]
        unsupported = [item for item in findings if item.get("status") == "unsupported"]
        delta = int(summary.get("score_delta") or output.get("score_delta") or 0)
        issue_type = title_case_issue(summary.get("issue_type") or output.get("issue_type") or "Finding")
        confirmed = scored or repeats
        notes.append(
            {
                "title": "Finding" if delta else "Already noted" if repeats else "Not supported",
                "issue_type": issue_type,
                "delta": delta,
                "contracts": unique_non_empty([item.get("contract_id") for item in confirmed]),
                "customers": unique_non_empty([f"{item.get('customer_id')} - {item.get('customer_name')}" for item in confirmed]),
                "unsupported": unique_non_empty([item.get("contract_id") or item.get("customer_id") for item in unsupported]),
                "assets": unique_non_empty([asset for item in confirmed for asset in (item.get("issue_asset_ids") or item.get("asset_ids", []))]),
                "brands": unique_non_empty([item.get("issue_brand_summary") or item.get("brand_summary") for item in confirmed]),
                "total": summary.get("total_score", 0),
            }
        )
    return notes

def audit_notes_from_scorecard(scorecard: dict) -> list[dict]:
    notes: list[dict] = []
    for issue_type, issue_score in scorecard.get("by_issue_type", {}).items():
        notes.append(
            {
                "issue_type": title_case_issue(issue_type),
                "score": int(issue_score.get("score") or 0),
                "contract_count": int(issue_score.get("contract_count") or 0),
                "customer_count": int(issue_score.get("customer_count") or 0),
                "findings": issue_score.get("findings", []),
            }
        )
    return sorted(notes, key=lambda note: (-note["score"], note["issue_type"]))


def local_scorecard_notes(case_data: dict[str, Any], ledger: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for finding in ledger.values():
        issue_type = str(finding.get("issue_type") or "Finding")
        group = grouped.setdefault(issue_type, {"customers": set(), "contracts": set(), "findings": 0})
        group["findings"] += 1
        customer_id = str(finding.get("customer_id") or "").strip()
        if customer_id:
            group["customers"].add(customer_id)
        if issue_type in CUSTOMER_SCOPED_ISSUES:
            for contract in contracts_for_customer(case_data, customer_id):
                group["contracts"].add(str(contract.get("record_id") or ""))
        else:
            contract_id = str(finding.get("contract_id") or "").strip()
            if contract_id:
                group["contracts"].add(contract_id)
    return sorted(
        [{"issue_type": issue, "contract_count": len(item["contracts"]), "customer_count": len(item["customers"]), "score": len(item["contracts"]), "finding_count": item["findings"]} for issue, item in grouped.items()],
        key=lambda item: (-item["score"], item["issue_type"]),
    )

def render_audit_summary_table(notes: list[dict]) -> None:
    rows = []
    for note in notes:
        rows.append(
            "<tr>"
            f"<td>{html.escape(short_issue_label(note.get('issue_type', 'finding')).title())}</td>"
            f"<td>{note.get('contract_count', 0)}</td>"
            f"<td>{note.get('customer_count', 0)}</td>"
            f"<td>{note.get('score', 0)}</td>"
            "</tr>"
        )
    if not rows:
        st.caption("No verified findings yet.")
        return
    st.markdown(
        "<table class='audit-summary-table'>"
        "<thead><tr><th>Issue Type</th><th>Contract</th><th>Customer</th><th>Score</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>",
        unsafe_allow_html=True,
    )
def short_issue_label(issue_type: str) -> str:
    labels = {
        "Active Overdue At Approval": "overdue at approval",
        "Aml Risk": "AML risk",
        "Approval By Role That Doesn't Exist": "invalid approval role",
        "Approval Is Actually For Another Customer": "wrong customer approval",
        "Connected Customer Exposure Hidden By Separate Customer Ids": "hidden connected exposure",
        "Contract Approved After Start Date": "late approval",
        "Customer In Tax Haven": "tax haven",
        "Customer Risk Deteriorates But Exposure Keeps Growing": "risk up, exposure up",
        "Customer In Default At Approval": "default at approval",
        "Down Payment Too Low": "low down payment",
        "Financing Only Non Traton Brands": "non-Traton brands",
        "Inflated Pricing": "inflated pricing",
        "Insufficient Approval Authority": "approval authority",
        "Interest Rate Extremely Low": "very low rate",
        "Low Interest Rate Despite Significant Overdues": "low rate with arrears",
        "Missing Or Weak Approval Narrative": "weak approval note",
        "Mv Curves Do Not Match Asset": "MV mismatch",
        "No Approval Recorded": "no approval recorded",
        "Non Commercial Vehicle Related Assets": "non-commercial asset",
        "Portfolio Snapshot Does Not Reconcile To Contract Level Data": "snapshot mismatch",
        "Recovered Overdue Not Disclosed": "recovered overdue",
        "Vague Hard Collateral": "vague collateral",
    }
    normalized = str(issue_type or "finding").replace("_", " ").strip().lower()
    for key, label in labels.items():
        if key.replace("_", " ").strip().lower() == normalized:
            return label
    return normalized

def count_label(count: int, singular: str) -> str:
    suffix = "" if count == 1 else "s"
    return f"{count} {singular}{suffix}"


def compact_items(values: list[str], limit: int = 3) -> str:
    items = unique_non_empty(values)
    shown = items[:limit]
    if len(items) > limit:
        shown.append(f"+{len(items) - limit}")
    return ", ".join(shown)


def sidebar_note_detail(note: dict) -> str:
    findings = note.get("findings", [])
    contracts = compact_items([item.get("contract_id") for item in findings if item.get("contract_id")])
    customers = compact_items([item.get("customer_id") for item in findings if item.get("customer_id")], limit=2)
    return " \u00b7 ".join(part for part in [contracts, customers] if part)


def render_audit_note(note: dict) -> None:
    issue_label = short_issue_label(note.get("issue_type", "finding")).capitalize()
    detail_html = html.escape(sidebar_note_detail(note))
    st.markdown(
        (
            "<div class='audit-note'>"
            f"<span class='audit-finding-tag'>{html.escape(issue_label)}</span>"
            f"<div class='audit-note-line audit-note-detail'>{detail_html}</div>"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def toast_audit_notes(notes: list[dict]) -> None:
    for note in notes:
        delta = int(note.get("delta", 0) or 0)
        if delta <= 0:
            continue
        refs = compact_items(note.get("contracts") or note.get("customers") or [], limit=3)
        detail = short_issue_label(note.get("issue_type", "finding")).capitalize()
        if refs:
            detail += f" \u00b7 {refs}"
        st.toast(f"Finding added: {detail}", icon=":material/check_circle:", duration="long")


def queue_audit_note_toasts(notes: list[dict]) -> None:
    queued = list(st.session_state.get("pending_audit_toasts", []))
    queued.extend(note for note in notes if int(note.get("delta", 0) or 0) > 0)
    st.session_state.pending_audit_toasts = queued


def flush_audit_note_toasts() -> None:
    notes = list(st.session_state.get("pending_audit_toasts", []))
    if not notes:
        return
    st.session_state.pending_audit_toasts = []
    toast_audit_notes(notes)


flush_audit_note_toasts()


def reset_interview() -> None:
    clear_chat_history_only()
    st.session_state.score_ledger = {}
    st.session_state.team_id = ""
    st.session_state.team_name = ""

def clear_chat_history_only() -> None:
    st.session_state.messages = []
    st.session_state.tool_events = []
    st.session_state.current_mood = "Professional / Controlled"
    st.session_state.active_refs = []
    st.session_state.active_investigation_scope = {"contracts": [], "customers": [], "assets": [], "vins": []}
    st.session_state.pending_agent_turn = False
    st.session_state.pending_record_work = False
    st.session_state.pending_audit_toasts = []


def render_audit_page() -> None:
    model_name = os.getenv("AUDIT_RPG_MODEL", DEFAULT_MODEL)
    st.markdown(
        """
        <div class='page-head'>
          <div class='app-title'>Interview: Mikael von Geld</div>

        </div>
        """,
        unsafe_allow_html=True,
    )

    interview_started = any(message.get("role") == "user" for message in st.session_state.messages)
    processing_turn = bool(st.session_state.pending_agent_turn)
    record_work = bool(st.session_state.pending_record_work)
    portrait_mood = "Checking Records" if processing_turn and record_work else st.session_state.current_mood

    left_col, chat_col = st.columns([0.30, 0.70], gap="large", vertical_alignment="top")
    with left_col:
        render_mikael_panel(portrait_mood, interview_started or processing_turn)
        render_interview_status(st.session_state.current_mood, initial=not interview_started and not processing_turn)
        st.markdown('<div class="screenshot-guidance">For screenshots, include only the relevant Contract IDs and Customer IDs.</div>', unsafe_allow_html=True)

    with chat_col:
        st.markdown("<div class='play-area-label'>Interview</div>", unsafe_allow_html=True)
        if not os.getenv("OPENAI_API_KEY"):
            st.warning("OPENAI_API_KEY is missing. Add it to environment or .env before sending a message.")

        chat_status_slot = render_chat_thread(st.session_state.messages, pending=processing_turn, record_work=record_work)

        with st.container(key="clear_chat_control"):
            if st.button(
                "Clear chat",
                key="clear_chat_only_button",
                type="secondary",
                disabled=not st.session_state.messages or st.session_state.pending_agent_turn,
            ):
                clear_chat_history_only()
                st.rerun()

        if st.session_state.pending_agent_turn:
            status_slot = chat_status_slot or st.empty()
            reply_slot = st.empty()
            try:
                reply, events, updated_scope = run_agent_turn_with_loading(
                    status_slot=status_slot,
                    messages=st.session_state.messages,
                    score_ledger=st.session_state.score_ledger,
                    data=case_data,
                    model=model_name,
                    active_refs=st.session_state.active_refs,
                    active_investigation_scope=st.session_state.active_investigation_scope,
                    record_work=record_work,
                )
                status_slot.empty()
            except Exception as exc:
                reply = f"[MOOD:Annoyed / Dismissive]\nI cannot answer while the model connection is failing: {exc}"
                events = []
                updated_scope = st.session_state.active_investigation_scope
                status_slot.markdown(render_chat_status("Mikael cannot reach the case file."), unsafe_allow_html=True)
            previous_role = st.session_state.messages[-1].get("role") if st.session_state.messages else None
            reply_message = {"role": "assistant", "content": reply, "tool_events": events}
            with reply_slot.container():
                st.markdown(
                    f'<div class="chat-shell">{render_chat_row(reply_message, previous_role)}</div>',
                    unsafe_allow_html=True,
                )
                if events:
                    with st.expander("Activity", expanded=False):
                        render_compact_tool_trace(events)
            score_events = audit_notes_from_events(events)
            queue_audit_note_toasts(score_events)

            mood = extract_mood(reply)
            st.session_state.current_mood = mood
            st.session_state.messages.append({
                "role": "assistant",
                "content": reply,
                "mood": mood,
                "score_events": score_events,
                "tool_events": events,
            })
            st.session_state.tool_events.extend(events)
            st.session_state.active_investigation_scope = updated_scope
            st.session_state.active_refs = (
                updated_scope["contracts"]
                + updated_scope["customers"]
                + updated_scope["assets"]
                + updated_scope["vins"]
            )
            st.session_state.pending_agent_turn = False
            st.session_state.pending_record_work = False
            st.rerun()
        else:
            prompt = st.chat_input(
                "Found something unusual? Name the record and tell me what you found.",
                accept_file=True,
                file_type=["png", "jpg", "jpeg"],
            )

            if prompt:
                text = prompt.text or ""
                image_urls = []
                display_images = []
                for uploaded in prompt.files or []:
                    content = uploaded.getvalue()
                    image_urls.append(image_to_data_url(uploaded.name, content))
                    display_images.append(content)

                st.session_state.messages.append(
                    {
                        "role": "user",
                        "content": text,
                        "images": image_urls,
                        "display_images": display_images,
                    }
                )
                quick_reply = small_talk_reply(st.session_state.messages, case_data)
                if quick_reply:
                    mood = extract_mood(quick_reply)
                    st.session_state.current_mood = mood
                    st.session_state.messages.append({"role": "assistant", "content": quick_reply, "mood": mood, "score_events": []})
                    st.rerun()

                st.session_state.pending_record_work = should_require_tool(case_data, st.session_state.messages, st.session_state.active_refs)
                st.session_state.pending_agent_turn = True
                st.rerun()




with st.sidebar:
    audit_nav, settings_nav = st.tabs(["Audit", "Settings"])
    with audit_nav:
        st.markdown("### Audit notes")
        display_notes = local_scorecard_notes(case_data, st.session_state.score_ledger)
        total_score = sum(int(item["score"]) for item in display_notes)
        st.caption(f"Score {total_score} · {len(display_notes)} issue type")
        render_audit_summary_table(display_notes)
    with settings_nav:
        if st.button("Reset interview", key="reset_interview_button", type="secondary", use_container_width=True):
            reset_interview()
            st.rerun()

render_audit_page()
