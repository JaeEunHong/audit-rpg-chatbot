import base64
import html
import io
import importlib
import json
import logging
import os
import hashlib
import hmac
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CORE_DIR = ROOT / "main" / "core"
core_path = str(CORE_DIR)
if core_path in sys.path:
    sys.path.remove(core_path)
sys.path.insert(0, core_path)
legacy_core_path = str(ROOT / "exp" / "core")
while legacy_core_path in sys.path:
    sys.path.remove(legacy_core_path)
# The deployed app uses main/core. Do not put the legacy exp/core modules
# ahead of it: both trees contain top-level module names such as audit_types.
legacy_audit_types = sys.modules.get("audit_types")
if legacy_audit_types is not None and str(ROOT / "exp" / "core") in str(
    getattr(legacy_audit_types, "__file__", "")
):
    del sys.modules["audit_types"]

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import altair as alt
from streamlit_cookies_controller import CookieController

from app_support import DEFAULT_MODEL, extract_mood, image_to_data_url, interview_state_for_mood, load_env, small_talk_reply
import chat_runtime
from conversation_state import ConversationState
from stage_08_audit_pipeline import _attitude_stage
from db.team import create_team, list_teams
from db.audit import TEAM_GROUPS, create_session, ensure_audit_tables, leaderboard_rows, load_team_score_ledger, participant_score_rows, seed_default_teams, save_turn_bundle
from streamlit_related.components.leaderboard import render_leaderboard
from streamlit_related.components.team_form import render_team_picker

chat_runtime = importlib.reload(chat_runtime)
run_chat_turn = chat_runtime.run_chat_turn



def run_agent_turn(
    messages, score_ledger, data, model=None, status_callback=None,
):
    latest_user = next((item for item in reversed(messages) if item.get("role") == "user"), {})
    state_before = st.session_state.conversation_state.to_dict()
    runtime_messages = [
        {
            "message_number": index + 1,
            "role": item.get("role", "user"),
            "content": str(item.get("content") or ""),
            "image_text": str(item.get("visual_extraction_text") or item.get("spreadsheet_text") or ""),
        }
        for index, item in enumerate(item for item in messages if not item.get("idle"))
    ]
    ledger_sync = {"status": "not_run", "team_id": st.session_state.get("team_id", ""), "loaded_count": 0}
    if st.session_state.get("team_id"):
        try:
            loaded_ledger = load_team_score_ledger(st.session_state.team_id)
            st.session_state.score_ledger.update(loaded_ledger)
            loaded_contracts = {
                str(item.get("contract_id") or item.get("record_id") or "")
                for item in loaded_ledger.values()
                if str(item.get("contract_id") or item.get("record_id") or "")
            }
            current_pressure = int(st.session_state.conversation_state.attitude.get("pressure", 0))
            restored_pressure = max(current_pressure, len(loaded_contracts))
            st.session_state.conversation_state.attitude.update({
                "pressure": restored_pressure,
                "stage": _attitude_stage(restored_pressure),
                "last_trigger": "team_ledger_sync",
                "last_delta": 0,
            })
            ledger_sync.update({"status": "loaded", "loaded_count": len(loaded_ledger)})
        except Exception as exc:
            ledger_sync.update({"status": "error", "error": str(exc)})
    result = run_chat_turn(
        str(latest_user.get("content") or ""),
        st.session_state.graph_data,
        st.session_state.conversation_state,
        runtime_messages,
        score_ledger,
        image_data_urls=list(latest_user.get("images") or []),
        status_callback=status_callback,
        team=st.session_state.get("team_id") or "default",
    )
    st.session_state.conversation_state = result.get("conversation_state", st.session_state.conversation_state)
    visual_text = str(result.get("visual_extraction_text") or "").strip()
    if visual_text and latest_user is not None:
        latest_user["visual_extraction_text"] = visual_text
    events = []
    if visual_text:
        events.append({"tool": "visual_parser", "output": {"markdown_table": visual_text}})
    events.append({"tool": "team_ledger_sync", "output": ledger_sync})
    if result.get("request"):
        events.append({"tool": "llm1_parser", "output": result["request"]})
    events.append({"tool": "python_flow", "output": {"status": result.get("status"), "scoring": result.get("scoring")}})
    score_result = result.get("scoring") or result.get("score_result") or {}
    if score_result.get("findings"):
        events.append({"tool": "update_score", "output": score_result})
    state_after = result.get("conversation_state")
    state_after = state_after.to_dict() if state_after else state_before
    events.append({"tool": "conversation_debug", "output": {
        "message": str(latest_user.get("content") or ""),
        "before": state_before,
        "after": state_after,
        "action": result.get("action"),
        "status": result.get("status"),
        "state": result.get("state"),
        "evidence": result.get("evidence"),
        "mood": result.get("mood"),
        "attitude": result.get("attitude"),
        "attitude_trace": result.get("attitude_trace"),
        "scoring": score_result,
        "reply": result["reply"],
    }})
    events.append({"tool": "llm2_generator", "output": {"reply": result["reply"]}})
    if st.session_state.get("team_id") and st.session_state.get("session_id"):
        try:
            findings = [
                finding
                for event in events
                if event.get("tool") == "update_score"
                for finding in (event.get("output") or {}).get("findings", [])
            ]
            save_turn_bundle(
                team_id=st.session_state.team_id,
                session_id=st.session_state.session_id,
                participant_id=st.session_state.get("participant_id", ""),
                turn_no=len(messages),
                user_message=str(latest_user.get("content") or ""),
                assistant_message=result["reply"],
                activity={"events": events},
                state_before=state_before,
                state_after=st.session_state.conversation_state.to_dict(),
                findings=findings,
            )
        except Exception:
            pass
    return result["reply"], events, {
        "mood": result.get("mood"),
        "portrait": result.get("portrait"),
    }


def render_activity(events: list[dict[str, Any]]) -> None:
    debug = next((event.get("output") for event in events if event.get("tool") == "conversation_debug"), None)
    if debug:
        before = debug.get("before", {})
        after = debug.get("after", {})
        scoring = debug.get("scoring") or {}
        st.code("\n".join([
            "â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”",
            f'USER: "{debug.get("message", "")}"',
            "â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”",
            f"[entity]     {before.get('focus_entities')}  ->  {after.get('focus_entities')}",
            f"[topic]      {before.get('focus_topic')}  ->  {after.get('focus_topic')}",
            f"[confirm]    {before.get('pending_confirmation')}  ->  {after.get('pending_confirmation')}",
            f"[raw_data]   {'reused' if before.get('last_raw_data') else 'not reused'}",
            f"[score]      {scoring.get('status', 'not run')}  score={scoring.get('score', 0)}  delta={scoring.get('score_delta', 0)}  ledger_entries={len(st.session_state.score_ledger)}",
            "â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€",
            f"RESPONSE: {debug.get('reply', '')}",
            "â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”",
        ]), language="text")
st.set_page_config(page_title="Nordovia Audit RPG", page_icon=":material/search:", layout="wide", initial_sidebar_state="expanded")

load_env()

AUTH_COOKIE = "audit_rpg_auth"
AUTH_COOKIE_TTL = 2 * 60 * 60


def _auth_controller() -> CookieController:
    return CookieController(key="audit_rpg_auth_controller")


def _auth_secret() -> bytes:
    return os.getenv("APP_AUTH_SECRET") or os.getenv("APP_PASSWORD", "")


def _make_auth_token(values: dict[str, str]) -> str:
    payload = dict(values, expires_at=str(int(time.time()) + AUTH_COOKIE_TTL))
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(_auth_secret().encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _read_auth_token(token: str) -> dict[str, str] | None:
    try:
        encoded, signature = str(token).split(".", 1)
        expected = hmac.new(_auth_secret().encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        values = json.loads(raw.decode())
        if int(values.get("expires_at", 0)) <= int(time.time()):
            return None
        return values
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _restore_auth_cookie() -> None:
    if st.session_state.get("authenticated"):
        return
    values = _read_auth_token(_auth_controller().get(AUTH_COOKIE))
    if not values:
        return
    st.session_state.authenticated = True
    for key in ("team_id", "team_name", "participant_id", "participant_name", "session_id"):
        if values.get(key):
            st.session_state[key] = values[key]
    st.session_state.app_page = "demo" if values.get("demo_mode") == "1" else "chat"
    st.session_state.intro_complete = True


def _save_auth_cookie(*, demo_mode: bool = False) -> None:
    values = {
        key: str(st.session_state.get(key, ""))
        for key in ("team_id", "team_name", "participant_id", "participant_name", "session_id")
    }
    values["demo_mode"] = "1" if demo_mode else "0"
    _auth_controller().set(AUTH_COOKIE, _make_auth_token(values), max_age=AUTH_COOKIE_TTL)


def logout() -> None:
    _auth_controller().remove(AUTH_COOKIE)
    for key in ("authenticated", "team_id", "team_name", "participant_id", "participant_name", "session_id"):
        st.session_state.pop(key, None)
    st.session_state.app_page = "home"
    st.rerun()

def require_app_password() -> None:
    _restore_auth_cookie()
    expected = os.getenv("APP_PASSWORD", "")
    if not expected:
        st.error("APP_PASSWORD is not configured.")
        st.stop()
    if st.session_state.get("authenticated", False):
        return

    st.markdown(
        '<style>html,body,.stApp,[data-testid="stAppViewContainer"],[data-testid="stMain"],[data-testid="stMainBlockContainer"]{background:#FFFFFF !important;background-color:#FFFFFF !important;color:#20242A !important;}.auth-title{font-size:28px;font-weight:600;margin-bottom:18px;text-align:center;}[data-testid="stForm"]{width:100%;max-width:480px;margin:0 auto;box-sizing:border-box;border:1px solid #E1E4EA;border-radius:12px;padding:18px 18px 16px;background:#FFFFFF;}[data-testid="stForm"] label{color:#344054 !important;}[data-testid="stForm"] input,[data-testid="stTextInput"] input{background:#FFFFFF !important;border-color:#C9CED8 !important;color:#20242A !important;}[data-testid="stForm"] input:focus,[data-testid="stTextInput"] input:focus{border-color:#505AC9 !important;box-shadow:0 0 0 1px #505AC9 !important;}[data-testid="stFormSubmitButton"] button{background:#505AC9 !important;border-color:#505AC9 !important;color:#FFFFFF !important;}[data-testid="stFormSubmitButton"] button:hover{background:#464EB8 !important;border-color:#464EB8 !important;}</style>',

        unsafe_allow_html=True,
    )
    with st.container(width="stretch", horizontal_alignment="center"):
        auth_center = st.container(width=520)
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
MIKAEL_DEFAULT_IMAGE = ASSET_DIR / "looks_good.jpg"
MIKAEL_AVATAR = str(ASSET_DIR / "mikael_profile.png")
MIKAEL_INTRO_IMAGE = ASSET_DIR / "mikael_profile_2.png"
MIKAEL_MOOD_IMAGES = {
    "Embarrassed / Caught": (
        ASSET_DIR / "embarrased.png",
    ),
    "Professional / Controlled": (
        ASSET_DIR / "looks_good.jpg",
        ASSET_DIR / "amused.jpg",
        ASSET_DIR / "determined.jpg",
    ),
    "Guarded / Hesitant": (
        ASSET_DIR / "concerned.jpg",
        ASSET_DIR / "doubtful.jpg",
        ASSET_DIR / "skeptical.jpg",
    ),
    "Defensive / Cornered": (
        ASSET_DIR / "defensive.jpg",
        ASSET_DIR / "frustrated.jpg",
        ASSET_DIR / "what_is_this.jpg",
    ),
    "Reluctant / Defeated": (
        ASSET_DIR / "tired.jpg",
        ASSET_DIR / "thinking.jpg",
    ),
    "Annoyed / Dismissive": (
        ASSET_DIR / "frustrated.jpg",
        ASSET_DIR / "what_is_this.jpg",
    ),
    "Checking Records": (
        ASSET_DIR / "checking_details.jpg",
        ASSET_DIR / "examining_data.jpg",
        ASSET_DIR / "analysing.jpg",
    ),
}
IDLE_PORTRAITS = (
    ASSET_DIR / "Bored.png",
    ASSET_DIR / "coffee_break.jpg",
    ASSET_DIR / "playing_with_model.jpg",
    ASSET_DIR / "Nothing is happening.png",
)
PORTRAIT_IMAGES = {
    path.stem.lower(): path
    for path in ASSET_DIR.iterdir()
    if path.suffix.lower() in {".jpg", ".jpeg", ".png"}
}
PORTRAIT_LABELS = {
    "looks_good": "Looks good",
    "amused": "Amused",
    "determined": "Determined",
    "concerned": "Concerned",
    "doubtful": "Doubtful",
    "skeptical": "Skeptical",
    "defensive": "Defensive",
    "frustrated": "Frustrated",
    "what_is_this": "What is this?",
    "tired": "Tired",
    "thinking": "Thinking",
    "checking_details": "Checking details",
    "examining_data": "Examining data",
    "analysing": "Analysing",
}
STATUS_MESSAGES = [
    (0.0, "Mikael is checking something."),
    (3.5, "Mikael is clicking around the system."),
]
RECORD_STATUS_MESSAGES = STATUS_MESSAGES
MIKAEL_OPENING_MESSAGE = (
    "I'm fairly confident you won't find much to challenge here. "
    "There is a policy, people follow it closely, and this is a very tightly run ship. 😎"
)
st.session_state.setdefault("messages", [])
st.session_state.setdefault("score_ledger", {})
st.session_state.setdefault("conversation_state", ConversationState())
st.session_state.setdefault("tool_events", [])
st.session_state.setdefault("current_mood", "Professional / Controlled")
st.session_state.setdefault("pending_agent_turn", False)
st.session_state.setdefault("pending_record_work", False)
st.session_state.setdefault("pending_audit_toasts", [])
st.session_state.setdefault("show_deck_placeholder", False)
st.session_state.setdefault("app_page", "home")
st.session_state.setdefault("facilitator_ended", False)
st.session_state.setdefault("demo_mode", False)
st.session_state.setdefault("team_id", "")
st.session_state.setdefault("team_name", "")
st.session_state.setdefault("participant_id", "")
st.session_state.setdefault("session_id", "")
st.session_state.setdefault("intro_page", 0)
st.session_state.setdefault("intro_complete", False)
st.session_state.setdefault("show_activity", False)
st.session_state.setdefault("last_activity_at", time.time())
st.session_state.setdefault("idle_message_added", False)
st.session_state.setdefault("portrait_override", None)
st.session_state.setdefault("portrait_key", None)
st.session_state.setdefault("idle_mood_label", None)
st.session_state.setdefault("last_idle_message_at", 0.0)
st.session_state.setdefault("special_mood_label", None)
st.session_state.setdefault("pending_upload_review", False)

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
def image_data_url(path_text: str, image_mtime: float) -> str:
    path = Path(path_text)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


GRAPH_PATH = ROOT / "main" / "output" / "case_graph.json"
if not GRAPH_PATH.exists():
    raise FileNotFoundError("refactoring/output/case_graph.json is missing")


@st.cache_data(show_spinner=False)
def load_case_graph(path: str, modified_ns: int) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


st.session_state.graph_data = load_case_graph(
    str(GRAPH_PATH), GRAPH_PATH.stat().st_mtime_ns
)
case_data = st.session_state.graph_data



CUSTOMER_SCOPED_ISSUES = {
    "AML RISK",
    "CUSTOMER IN TAX HAVEN",
    "CONNECTED CUSTOMER EXPOSURE HIDDEN BY SEPARATE CUSTOMER IDS",
}


def select_page(page: str) -> None:
    st.session_state.app_page = page
    st.rerun()


DEFAULT_TEAMS = [
    ("TEAM_AI", "Team 1"),
    ("DATA_1", "Team 2"),
    ("DATA_2", "Team 3"),
    ("DATA_3", "Team 4"),
    ("DATA_4", "Team 5"),
    ("DATA_5", "Team 6"),
    ("PAPER_1", "Team 7"),
    ("PAPER_2", "Team 8"),
    ("DEMO_X", "Data X"),
    ("DEMO_Y", "Paper X"),
    ("DEMO_Z", "AI X"),
]


@st.cache_resource(show_spinner=False)
def initialize_audit_storage(cache_version: str = "teams-v3") -> bool:
    try:
        ensure_audit_tables()
        seed_default_teams()
        return True
    except Exception:
        return False


@st.cache_data(ttl=5, show_spinner=False)
def available_teams() -> list[dict[str, str]]:
    try:
        # Bump this version when the seeded team registry changes. The short
        # TTL also lets facilitator/demo views pick up Snowflake changes quickly.
        initialize_audit_storage("teams-v4")
        teams = list_teams()
        if teams:
            return [dict(team, group=team.get("group") or TEAM_GROUPS.get(team["team_id"], "DATA")) for team in teams]
    except Exception:
        pass
    return [
        {"team_id": team_id, "team_name": name, "group": TEAM_GROUPS.get(team_id, "DATA")}
        for team_id, name in DEFAULT_TEAMS
    ]


@st.dialog("Before you begin", dismissible=False)
def render_intro_wizard() -> None:
    pages = [
        ("Who is Mikael?", "You are meeting Mikael von Geld, a senior credit manager with years of experience defending the portfolio and the decisions behind it. He knows the business, the relationships, and exactly how to make an uncomfortable question sound complicated."),
        ("Build your case", "Find something that looks off.\n\nBring Mikael the contracts or customers behind it, but keep each message focused on one issue. Throw too many unrelated suspicions at him at once, and he’ll only get more confident… and more annoying."),
        ("File upload tips", "Upload a screenshot, Excel file, or CSV with only the relevant Contract IDs, Customer IDs, Asset IDs, or VINs. Keep groups around 150 records or fewer — if the file is too large, Mikael may ask you to narrow it down."),
    ]
    page = st.session_state.intro_page
    title, body = pages[page]
    if page == 0 and MIKAEL_INTRO_IMAGE.exists():
        st.image(str(MIKAEL_INTRO_IMAGE), width=260)
    st.markdown(f"### {title}")
    st.write(body)
    if page == 0:
        st.info("Character note: confident, experienced, and not especially eager to admit that the process failed.")
    st.progress((page + 1) / len(pages))
    left, right = st.columns(2)
    with left:
        if page > 0 and st.button("Back", key="intro_back"):
            st.session_state.intro_page -= 1
            st.rerun()
    with right:
        label = "Start chat" if page == len(pages) - 1 else "Next"
        if st.button(label, key="intro_next", type="primary"):
            if page == len(pages) - 1:
                st.session_state.intro_complete = True
            else:
                st.session_state.intro_page += 1
            st.rerun()


def render_home_page() -> None:
    teams = available_teams()
    st.markdown(
        "<div class='home-stage'><div class='home-kicker'>AUDIT ROOM</div>"
        "<div class='home-title'>Choose your team</div>"
        "<div class='home-copy'>Work together, find the inconsistencies, and see your team's score move.</div></div>",
        unsafe_allow_html=True,
    )
    with st.container(width="stretch", horizontal_alignment="center"):
        center = st.container(width=520)
    with center:
        demo_version = st.checkbox("Demo version", key="home_demo_version")
        visible_teams = (
            [team for team in teams if team["team_id"] in {"DEMO_X", "DEMO_Y", "DEMO_Z"}]
            if demo_version
            else [team for team in teams if team["team_id"] not in {"DEMO_X", "DEMO_Y", "DEMO_Z"}]
        )
        selected = st.selectbox(
            "Team",
            visible_teams,
            format_func=lambda team: team["team_name"],
            key="home_team_select",
            label_visibility="collapsed",
        )
        if demo_version:
            participant_name = "Demo participant"
        else:
            participant_name = st.text_input(
                "Your name",
                placeholder="Enter your name",
                key="participant_name_input",
                label_visibility="collapsed",
            )
        if st.button("Enter review room", key="join_team", type="primary", width="stretch"):
            if not participant_name.strip():
                st.warning("Enter your name before joining.")
                st.stop()
            st.session_state.team_id = selected["team_id"]
            st.session_state.team_name = selected["team_name"]
            st.session_state.participant_name = participant_name.strip()
            st.session_state.participant_id = st.session_state.get("participant_id") or str(__import__("uuid").uuid4())
            st.session_state.session_id = str(__import__("uuid").uuid4())
            st.session_state.score_ledger = {}
            try:
                create_session(
                    session_id=st.session_state.session_id,
                    team_id=st.session_state.team_id,
                    participant_id=st.session_state.participant_id,
                    participant_name=st.session_state.participant_name,
                )
            except Exception:
                pass
            st.session_state.intro_page = 0
            st.session_state.intro_complete = demo_version
            st.session_state.authenticated = True
            _save_auth_cookie(demo_mode=demo_version)
            select_page("demo" if demo_version else "chat")


@st.fragment(run_every=60)
def render_facilitator_page(case_data: dict[str, Any]) -> None:
    try:
        teams = available_teams()
        rows = leaderboard_rows()
    except Exception as exc:
        teams = available_teams()
        rows = []
        st.caption(f"Snowflake scoreboard unavailable; showing an empty preview. ({exc})")
    st.markdown("# Facilitator dashboard")
    if not st.session_state.facilitator_ended:
        st.caption("Team scores refresh automatically while the session is running.")
        back_col, home_col = st.columns(2)
        with back_col:
            if st.button("Back to chat", key="facilitator_back"):
                select_page("chat")
        with home_col:
            if st.button("Change team", key="facilitator_home"):
                st.session_state.team_id = ""
                st.session_state.team_name = ""
                select_page("home")
        render_leaderboard(rows, teams, case_data)
        if st.button("End session", key="facilitator_end_session", type="primary"):
            st.session_state.facilitator_ended = True
            st.rerun()
        return

    st.caption("Final results grouped by the preset DATA, PAPER, and AI team mapping.")
    try:
        participant_rows = participant_score_rows()
    except Exception as exc:
        participant_rows = []
        st.warning(f"Final KPI data is unavailable. ({exc})")
    final_rows = []
    for row in participant_rows:
        final_rows.append({
            "Group": TEAM_GROUPS.get(row["team_id"], "DATA"),
            "Participant": row["participant_name"],
            "Total score": row["score"],
        })
    if final_rows:
        final_df = pd.DataFrame(final_rows)
        final_df = (
            final_df.groupby(["Group", "Participant"], as_index=False)["Total score"]
            .sum()
        )
        with st.container(border=True):
            st.dataframe(final_df, hide_index=True, width="stretch")
            chart_data = (
                final_df.groupby("Group", as_index=False)["Total score"]
                .mean()
                .rename(columns={"Total score": "Average score per person"})
            )
            chart = (
                alt.Chart(chart_data)
                .mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5, size=44)
                .encode(
                    x=alt.X("Group:N", sort=["DATA", "PAPER", "AI"], title=None),
                    y=alt.Y("Average score per person:Q", title=None, scale=alt.Scale(zero=True)),
                    color=alt.Color(
                        "Group:N",
                        title="Team group",
                        scale=alt.Scale(
                            domain=["DATA", "PAPER", "AI"],
                            range=["#6F8FA3", "#B28AC2", "#4F46B5"],
                        ),
                        legend=alt.Legend(orient="top", title=None),
                    ),
                    tooltip=["Group:N", alt.Tooltip("Average score per person:Q", format=".2f")],
                )
                .properties(height=220)
                .configure_view(stroke=None)
                .configure_axis(
                    domainColor="#D9DDE7",
                    gridColor="#EEF0F5",
                    labelColor="#667085",
                    labelFontSize=14,
                    title=None,
                )
            )
            st.altair_chart(chart, width="stretch")
    else:
        st.info("No scored participant data is available yet.")


def render_participant_page() -> None:
    if not st.session_state.get("team_id"):
        render_home_page()
        return
    if not st.session_state.get("intro_complete"):
        render_intro_wizard()
        return
    render_audit_page()


def render_demo_page(case_data: dict[str, Any]) -> None:
    st.markdown("# Demo workspace")
    st.caption("Watch the team scoreboard while testing a participant chat in the same workspace.")
    teams = available_teams()
    selected_team_id = st.selectbox(
        "Demo team",
        [team["team_id"] for team in teams],
        index=next(
            (index for index, team in enumerate(teams) if team["team_id"] == st.session_state.get("team_id")),
            0,
        ),
        format_func=lambda team_id: next(team["team_name"] for team in teams if team["team_id"] == team_id),
        key="demo_team_select",
    )
    selected_team = next(team for team in teams if team["team_id"] == selected_team_id)
    if selected_team_id != st.session_state.get("team_id"):
        st.session_state.team_id = selected_team_id
        st.session_state.team_name = selected_team["team_name"]
        st.session_state.session_id = str(__import__("uuid").uuid4())
        st.session_state.participant_id = st.session_state.get("participant_id") or str(__import__("uuid").uuid4())
        st.session_state.participant_name = st.session_state.get("participant_name") or "Demo participant"
        st.session_state.messages = []
        st.session_state.score_ledger = {}
        st.session_state.conversation_state = ConversationState()
        st.session_state.intro_complete = True
        try:
            create_session(
                session_id=st.session_state.session_id,
                team_id=st.session_state.team_id,
                participant_id=st.session_state.participant_id,
                participant_name=st.session_state.participant_name,
            )
        except Exception:
            pass
        st.rerun()
    rows = leaderboard_rows()
    render_leaderboard(rows, teams, case_data)
    st.markdown("### Average score per person")
    try:
        participant_rows = participant_score_rows()
    except Exception:
        participant_rows = []
    if participant_rows:
        preview_df = pd.DataFrame([
            {
                "Group": TEAM_GROUPS.get(row["team_id"], "DATA"),
                "Participant": row["participant_name"],
                "Total score": row["score"],
            }
            for row in participant_rows
        ]).groupby(["Group", "Participant"], as_index=False)["Total score"].sum()
        chart_data = (
            preview_df.groupby("Group", as_index=False)["Total score"]
            .mean()
            .rename(columns={"Total score": "Average score per person"})
        )
        chart = (
            alt.Chart(chart_data)
            .mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5, size=48)
            .encode(
                x=alt.X("Group:N", sort=["DATA", "PAPER", "AI"], title=None),
                y=alt.Y("Average score per person:Q", title=None),
                color=alt.Color(
                    "Group:N", title=None,
                    scale=alt.Scale(domain=["DATA", "PAPER", "AI"], range=["#6F8FA3", "#B28AC2", "#4F46B5"]),
                    legend=None,
                ),
                tooltip=["Group:N", alt.Tooltip("Average score per person:Q", format=".2f")],
            )
            .properties(height=220)
            .configure_view(stroke=None)
            .configure_axis(
                gridColor="#EEF0F5",
                domainColor="#D9DDE7",
                labelFontSize=14,
                title=None,
            )
        )
        with st.container(border=True):
            st.altair_chart(chart, width="stretch")
    else:
        st.info("The KPI chart will appear after the first scored finding.")
    st.divider()
    render_audit_page()


model_name = os.getenv("AUDIT_GENERATOR_MODEL", DEFAULT_MODEL)
show_tools = False
st.markdown(
    """
    <style>
:root {
    --audit-bg: #FFFFFF;
    --audit-panel: #F5F6FA;
    --audit-text: #20242A;
    --audit-muted: #667085;
    --audit-line: #E1E4EA;
    --audit-accent: #505AC9;
    --audit-warm: #505AC9;
    --audit-input: #EEF0FF;
    --audit-font: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.stApp,
[data-testid="stAppViewContainer"],
[data-testid="stMain"],
[data-testid="stMain"] > div,
[data-testid="stHeader"] {
    background: var(--audit-bg) !important;
    background-color: #FFFFFF !important;
    color: var(--audit-text);
}
html,
body,
[data-testid="stAppViewContainer"] > .main,
[data-testid="stAppViewContainer"] > .main > div,
[data-testid="stMainBlockContainer"] {
    background-color: #FFFFFF !important;
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
    font-family: "Segoe UI", Inter, -apple-system, BlinkMacSystemFont, sans-serif !important;
    font-size: clamp(30px, 2.15vw, 34px) !important;
    font-weight: 600;
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
    max-width: 360px;
    aspect-ratio: 16 / 9;
    border-radius: 8px;
    overflow: hidden;
    background: transparent;
    line-height: 0;
    margin: 0 0 12px !important;
    transform: translateY(-42px);
}
.home-stage {
    max-width: 520px;
    margin: 96px auto 22px;
    text-align: center;
}
.home-kicker {
    color: var(--audit-accent);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .12em;
    margin-bottom: 12px;
}
.home-title {
    color: var(--audit-text);
    font-size: 30px;
    font-weight: 600;
    line-height: 1.2;
}
.home-copy {
    color: var(--audit-muted);
    font-size: 14px;
    line-height: 1.5;
    margin-top: 10px;
}
.mikael-portrait {
    display: block !important;
    width: 100% !important;
    max-width: none !important;
    height: 100% !important;
    object-fit: cover;
    object-position: center center;
    cursor: zoom-in;
}
.mikael-portrait.portrait-enlarged {
    position: fixed;
    z-index: 100000;
    inset: 8vh 10vw;
    width: 80vw !important;
    height: 84vh !important;
    object-fit: contain;
    background: #FFFFFF;
    border: 1px solid #E1E4EA;
    border-radius: 12px;
    box-shadow: 0 20px 60px rgba(16, 24, 40, 0.28);
    cursor: zoom-out;
}
.interview-status {
    display: flex;
    align-items: center;
    width: 100%;
    max-width: 360px;
    margin: 0 0 18px;
    color: var(--audit-muted);
    font-size: 11.5px;
    line-height: 1;
    white-space: nowrap;
    transform: translateY(-42px);
}
.status-item {
    display: inline-flex;
    align-items: center;
    height: 24px;
    gap: 5px;
    padding: 0 9px;
    border: 1px solid rgba(225, 228, 234, 0.95);
    border-radius: 999px;
    background: rgba(248, 249, 252, 0.9);
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
    border-top: 1px solid rgba(225, 228, 234, 0.8);
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
    max-width: none;
    padding-top: 0;
    margin-bottom: 8px;
}
.chat-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 8px;
}
.connection-badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    color: #667085;
    font-size: 11px;
    line-height: 1;
    white-space: nowrap;
}
.connection-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #22A06B;
    box-shadow: 0 0 0 2px rgba(34, 160, 107, 0.12);
}
.clear-chat-control,
.st-key-clear_chat_control {
    max-width: none;
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
    width: 100%;
    max-width: none;
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
    display: flex;
    flex-direction: row;
    gap: 8px;
    align-items: start;
    max-width: 92%;
    margin: 0 0 12px;
}
.chat-row.user {
    flex-direction: row-reverse;
    margin-left: auto;
}
.chat-row.grouped {
    margin-top: -2px;
    margin-bottom: 10px;
}
.chat-row.user .chat-avatar,
.chat-row.user .chat-speaker-name {
    display: none;
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
    font-weight: 400;
    line-height: 1.2;
    margin: 0 0 3px;
}
.chat-time {
    color: rgba(124, 132, 143, 0.86);
    font-size: 10.5px;
    font-weight: 400;
    margin-left: 5px;
}
.chat-row.grouped .chat-speaker {
    opacity: 1;
}
.chat-message-body {
    display: inline-block;
    max-width: 610px;
    border: 1px solid rgba(225, 228, 234, 0.72);
    border-radius: 8px;
    padding: 7px 10px;
    font-size: 14.5px;
    font-weight: 400;
    line-height: 1.42;
    color: var(--audit-text);
}
.chat-row.user .chat-message-body {
    background: rgba(238, 240, 255, 0.96);
    border-color: rgba(190, 196, 238, 0.92);
}
.chat-row.user .chat-content {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
}
.chat-row.user .chat-speaker {
    order: 0;
    text-align: right;
}
.chat-row.assistant .chat-message-body {
    background: rgba(255, 255, 255, 0.98);
    border-color: rgba(220, 223, 232, 0.9);
}
.chat-shot {
    display: block;
    width: min(160px, 100%) !important;
    max-width: 160px !important;
    height: auto !important;
    max-height: 110px;
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
    background: rgba(80, 90, 201, 0.62);
    animation: auditPulse 1.2s ease-in-out infinite;
}
@keyframes auditPulse {
    0%, 100% { opacity: 0.35; }
    50% { opacity: 0.95; }
}
[data-testid="stChatInput"] {
    max-width: none;
    position: sticky;
    bottom: 0;
    z-index: 4;
    background: linear-gradient(180deg, rgba(255, 255, 255, 0), var(--audit-bg) 30%);
    padding-top: 6px;
}
[data-testid="stChatInput"] > div {
    min-height: 46px !important;
    border-radius: 10px !important;
    border: 1px solid rgba(225, 228, 234, 0.86) !important;
    background: rgba(255, 255, 255, 0.92) !important;
    box-shadow: none !important;
}
[data-testid="stChatInput"] textarea {
    font-size: 14px !important;
    font-weight: 400 !important;
    min-height: 40px !important;
    line-height: 1.35 !important;
    color: var(--audit-text) !important;
    background: rgba(255, 255, 255, 0.92) !important;
}
[data-testid="stChatInput"] textarea::placeholder {
    color: rgba(63, 95, 111, 0.72) !important;
}
[data-testid="stChatInput"] button {
    width: 30px !important;
    height: 30px !important;
    min-width: 30px !important;
    border-radius: 8px !important;
    background: rgba(245, 246, 250, 0.9) !important;
    color: rgba(63, 95, 111, 0.62) !important;
    box-shadow: none !important;
}
[data-testid="stChatInput"] button:first-of-type {
    opacity: 0.38;
}
[data-testid="stSidebar"] {
    background: var(--audit-panel);
    border-right: 1px solid rgba(225, 228, 234, 0.95);
}
[data-testid="stExpander"] pre {
    font-size: 11px !important;
    line-height: 1.35 !important;
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
    background: rgba(245, 246, 250, 0.7) !important;
    color: rgba(32, 36, 42, 0.74) !important;
}
[data-testid="stSidebar"] input {
    font-size: 13px !important;
    font-weight: 400 !important;
    background: rgba(245, 246, 250, 0.8) !important;
}
[data-testid="stSidebar"] [data-testid="stToggle"] label {
    color: rgba(32, 36, 42, 0.62);
}
[data-testid="stSidebar"] [data-baseweb="tab-list"] {
    gap: 0.8rem;
    border-bottom: 1px solid rgba(225, 228, 234, 0.95);
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
    background: rgba(225, 228, 234, 0.95);
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
    border-bottom: 1px solid rgba(225, 228, 234, 0.9);
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
    border-bottom: 1px solid rgba(225, 228, 234, 0.58);
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
    border: 1px solid rgba(225, 228, 234, 0.9);
    border-radius: 7px;
    background: rgba(245, 246, 250, 0.75);
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
    return MIKAEL_AVATAR


ENTITY_COLUMNS = {
    "customerid": "CustomerID",
    "customername": "CustomerName",
    "contractid": "ContractID",
    "assetid": "AssetID",
    "vin": "VIN",
}


def spreadsheet_entity_text(file_name: str, content: bytes) -> str:
    if file_name.lower().endswith(".csv"):
        frame = pd.read_csv(io.BytesIO(content))
    else:
        frame = pd.read_excel(io.BytesIO(content), sheet_name=0)
    columns = {
        str(column).strip().lower().replace(" ", ""): column
        for column in frame.columns
    }
    selected = []
    for normalized, output_name in ENTITY_COLUMNS.items():
        source = columns.get(normalized)
        if source is not None:
            selected.append((source, output_name))
    if not selected:
        raise ValueError("The spreadsheet has none of the supported entity columns.")
    compact = frame[[source for source, _ in selected]].copy()
    compact.columns = [output_name for _, output_name in selected]
    compact = compact.fillna("").astype(str)
    return compact.to_markdown(index=False)


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
    override = st.session_state.get("portrait_override")
    if override:
        image_path = Path(override)
    elif interview_started:
        candidates = MIKAEL_MOOD_IMAGES.get(mood, MIKAEL_MOOD_IMAGES["Professional / Controlled"])
        image_path = candidates[len(st.session_state.messages) % len(candidates)]
    elif st.session_state.get("portrait_key") in PORTRAIT_IMAGES:
        image_path = PORTRAIT_IMAGES[st.session_state.portrait_key]
    else:
        image_path = MIKAEL_DEFAULT_IMAGE

    image_url = html.escape(image_data_url(str(image_path), image_path.stat().st_mtime if image_path.exists() else 0.0), quote=True)
    st.markdown(
        f'<div class="mikael-card">'
        f'<img class="mikael-portrait" src="{image_url}" alt="Mikael von Geld portrait" '
        'onclick="this.classList.toggle(\'portrait-enlarged\')"></div>',
        unsafe_allow_html=True,
    )

def render_interview_status(mood: str, initial: bool = False, display_label: str | None = None) -> None:
    status = {"mood": "Confident"} if initial else interview_state_for_mood(mood)
    if display_label:
        status = {"mood": display_label}
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


def message_time(message: dict) -> str:
    return html.escape(str(message.get("timestamp") or time.strftime("%H:%M")))


def render_chat_row(message: dict, previous_role: str | None = None) -> str:
    role = "user" if message.get("role") == "user" else "assistant"
    speaker = "Auditor" if role == "user" else "Mikael von Geld"
    grouped = " grouped" if previous_role == role else ""
    avatar_html = "" if role == "user" else (
        f'<img class="chat-avatar" src="{html.escape(avatar_data_url(role), quote=True)}" alt="{speaker} avatar">'
    )
    body = html_chat_text(message.get("content", ""))
    images = "".join(
        f'<img class="chat-shot" src="{html.escape(src, quote=True)}" alt="Uploaded screenshot">'
        for src in message.get("images", [])
    )
    if not body and not images:
        return ""
    return (
        f'<div class="chat-row {role}{grouped}">'
        + avatar_html
        + '<div class="chat-content">'
        f'<div class="chat-speaker"><span class="chat-speaker-name">{speaker}</span>'
        f'<span class="chat-time">{message_time(message)}</span></div>'
        f'<div class="chat-message-body">{body}{images}</div>'
        '</div>'
        '</div>'
    )


def render_chat_thread(messages: list[dict], pending: bool = False, record_work: bool = False):
    status_slot = None
    if not messages:
        with st.container(height=300):
            opening = {
                "role": "assistant",
                "content": MIKAEL_OPENING_MESSAGE,
                "timestamp": time.strftime("%H:%M"),
            }
            st.markdown(
                f'<div class="chat-shell">{render_chat_row(opening, None)}</div>',
                unsafe_allow_html=True,
            )
            if pending:
                status_slot = st.empty()
        return status_slot

    previous_role = None
    with st.container(height=300, key="chat_thread"):
        for message in messages:
            row = render_chat_row(message, previous_role)
            if not row:
                continue
            st.markdown(row, unsafe_allow_html=True)
            if message.get("role") == "assistant" and message.get("tool_events"):
                with st.expander("Activity", expanded=st.session_state.show_activity and st.session_state.app_page == "demo"):
                    render_activity(message["tool_events"])
            previous_role = "user" if message.get("role") == "user" else "assistant"
        if pending:
            status_slot = st.empty()
    return status_slot


def render_chat_autoscroll(message_count: int) -> None:
    # Include the count in the component payload so Streamlit creates a fresh
    # iframe after each new message instead of reusing an observer that expired.
    component_html = """
        <!-- chat message count: __MESSAGE_COUNT__ -->
        <script>
        (() => {
          const messageCount = __MESSAGE_COUNT__;
          const parent = window.parent.document;
          const scrollToBottom = () => {
            const root = parent.querySelector('[class*="st-key-chat_thread"]');
            if (!root) return false;
            const nodes = [root, ...root.querySelectorAll('*')];
            const scrollable = nodes.find((node) =>
              node.scrollHeight > node.clientHeight &&
              ['auto', 'scroll'].includes(getComputedStyle(node).overflowY)
            );
            if (!scrollable) return false;
            scrollable.scrollTop = scrollable.scrollHeight;
            return true;
          };
          let attempts = 0;
          const retry = setInterval(() => {
            attempts += 1;
            scrollToBottom();
            if (attempts >= 20) clearInterval(retry);
          }, 100);
          const observer = new MutationObserver(() => scrollToBottom());
          observer.observe(parent.body, {childList: true, subtree: true});
          setTimeout(() => observer.disconnect(), 2500);
        })();
        </script>
        """.replace("__MESSAGE_COUNT__", str(message_count))
    components.html(
        component_html,
        height=0,
        scrolling=False,
    )


@st.fragment(run_every="30s")
def idle_message_watch() -> None:
    if not st.session_state.messages or st.session_state.pending_agent_turn:
        return
    now = time.time()
    last_idle_at = float(st.session_state.get("last_idle_message_at", 0.0))
    if now - max(st.session_state.last_activity_at, last_idle_at) < 300:
        return
    idle_turn = sum(1 for item in st.session_state.messages if item.get("idle"))
    idle_labels = ("Bored", "Coffee break", "Playing with model", "Nothing is happening")
    comments = [
        "Hello...? I’m still here, if we’re not done with me yet.",
        "I took a coffee break. The findings, regrettably, did not.",
        "I was just testing the model while you were away. Very productive, obviously.",
        "No rush... I can keep staring at the case file if that helps.",
    ]
    st.session_state.portrait_override = str(IDLE_PORTRAITS[idle_turn % len(IDLE_PORTRAITS)])
    st.session_state.idle_mood_label = idle_labels[idle_turn % len(idle_labels)]
    st.session_state.messages.append({
        "role": "assistant",
        "content": comments[len(st.session_state.messages) % len(comments)],
        "mood": "Annoyed / Dismissive",
        "idle": True,
        "score_events": [],
        "tool_events": [],
    })
    st.session_state.idle_message_added = True
    st.session_state.last_idle_message_at = now
    st.rerun()
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


def run_agent_turn_with_loading(*, status_slot, messages, score_ledger, data, model: str, record_work: bool):
    def update_status(message: str) -> None:
        status_slot.markdown(render_chat_status(message), unsafe_allow_html=True)

    update_status("Mikael is checking the system.")
    return run_agent_turn(
        messages,
        score_ledger,
        data,
        model=model,
        status_callback=update_status,
    )
def render_activity(events: list[dict[str, Any]]) -> None:
    runtime_error = next((event.get("output") for event in events if event.get("tool") == "runtime_error"), None)
    if runtime_error:
        st.error(
            f"Runtime error: {runtime_error.get('error_type', 'UnknownError')} — "
            f"{runtime_error.get('message', 'No error message recorded.')}"
        )
        return
    debug = next((event.get("output") for event in events if event.get("tool") == "conversation_debug"), None)
    if not debug:
        return
    before = debug.get("before", {})
    after = debug.get("after", {})
    scoring = debug.get("scoring") or {}
    evidence = debug.get("evidence")

    def compact_items(items: list[Any], limit: int = 5) -> str:
        visible = items[:limit]
        suffix = f" +{len(items) - limit} more" if len(items) > limit else ""
        return f"{visible}{suffix}"

    def compact_state(value: Any) -> str:
        if not isinstance(value, dict):
            return str(value)
        result = dict(value)
        for key in ("focus_entities", "entity_ids"):
            if isinstance(result.get(key), list):
                result[key] = compact_items(result[key])
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    if isinstance(evidence, dict):
        compact_evidence = dict(evidence.get("evidence_package") or {})
        for key in ("entity_samples", "public_narrative_samples", "secret_narrative_samples"):
            if isinstance(compact_evidence.get(key), list):
                compact_evidence[key] = compact_items(compact_evidence[key], 3)
        evidence_label = json.dumps(compact_evidence, ensure_ascii=False, separators=(",", ":"))
    else:
        evidence_label = "none"
    requested_issue = (evidence or {}).get("requested_issue", []) if isinstance(evidence, dict) else []
    topic_before = (before.get("focus_topic") or {}).get("issue") or "none"
    topic_after = (after.get("focus_topic") or {}).get("issue") or requested_issue or "none"
    entities_before = before.get("focus_entities") or []
    entities_after = after.get("focus_entities") or []
    scoring_status = scoring.get("status", "not run")
    package = (evidence or {}).get("evidence_package", {}) if isinstance(evidence, dict) else {}
    presentation = (evidence or {}).get("easter_egg_presentation") if isinstance(evidence, dict) else None
    issue_label = ", ".join((evidence or {}).get("requested_issue", [])) if isinstance(evidence, dict) else "none"
    lines = [
        "======================================",
        f'USER: "{debug.get("message", "")}"',
        "======================================",
        f"[status]     {debug.get('status', scoring_status)}  state={debug.get('state', (evidence or {}).get('context_routing', {}).get('state', 'n/a') if isinstance(evidence, dict) else 'n/a')}",
        f"[action]     {debug.get('action') or 'not run'}",
        f"[issue]      {issue_label}",
        f"[topic]      {topic_before}  ->  {topic_after}",
        f"[entities]   {compact_items(entities_before)}  ->  {compact_items(entities_after)}",
        f"[findings]   confirmed={package.get('confirmed_count', 0)}  unsupported={package.get('unsupported_count', 0)}",
        f"[presentation] {presentation or 'default'}",
        f"[pending]    {compact_state(before.get('pending_confirmation'))}  ->  {compact_state(after.get('pending_confirmation'))}",
        f"[attitude]   pressure={(debug.get('attitude') or {}).get('pressure', 'n/a')}  stage={(debug.get('attitude') or {}).get('stage', 'n/a')}  delta={(debug.get('attitude_trace') or {}).get('pressure_delta', 'n/a')}",
        f"[tone]       {before.get('response_tone', 'confident')}  ->  {after.get('response_tone', package.get('tone', 'n/a'))}",
        f"[score]      {scoring_status}  score={scoring.get('score', 0)}  delta={scoring.get('score_delta', 0)}",
        f"[response]   {debug.get('reply', '')}",
        f"[details]    {evidence_label}",
        f"[raw_data]   {'reused' if before.get('last_raw_data') else 'not reused'}",
        f"[ledger]     entries={len(st.session_state.score_ledger)}",
        "--------------------------------------",
        f"RESPONSE: {debug.get('reply', '')}",
        "======================================",
    ]
    st.code("\n".join(lines), language="text")


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
            for contract_id in case_data.get("customers", {}).get(customer_id, {}).get("contract_ids", []):
                group["contracts"].add(str(contract_id))
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
        st.toast(f"Findings added +{delta}: {detail}", icon="🔍", duration="long")


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
    st.session_state.conversation_state = ConversationState()

def clear_chat_history_only() -> None:
    st.session_state.messages = []
    st.session_state.tool_events = []
    st.session_state.current_mood = "Professional / Controlled"
    st.session_state.pending_agent_turn = False
    st.session_state.pending_record_work = False
    st.session_state.pending_audit_toasts = []
    st.session_state.conversation_state = ConversationState()
    st.session_state.last_activity_at = time.time()
    st.session_state.idle_message_added = False
    st.session_state.idle_mood_label = None


def render_audit_page() -> None:
    idle_message_watch()
    model_name = os.getenv("AUDIT_GENERATOR_MODEL", DEFAULT_MODEL)
    st.markdown(
        """
        <div class='page-head'>
          <div class='app-title'>Meeting with Mikael von Geld</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    interview_started = any(message.get("role") == "user" for message in st.session_state.messages)
    processing_turn = bool(st.session_state.pending_agent_turn)
    record_work = bool(st.session_state.pending_record_work)
    portrait_mood = "Checking Records" if processing_turn and record_work else st.session_state.current_mood

    left_col, chat_col = st.columns([0.28, 0.72], gap="large", vertical_alignment="center")
    with left_col:
        render_mikael_panel(portrait_mood, interview_started or processing_turn)
        mood_status_slot = st.empty()
        if not processing_turn:
            with mood_status_slot.container():
                render_interview_status(
                    st.session_state.current_mood,
                    initial=not interview_started,
                    display_label=(st.session_state.idle_mood_label or st.session_state.special_mood_label),
                )

    with chat_col:
        st.markdown(
            "<div class='chat-heading'><span class='play-area-label'>Interview</span>"
            "<span class='connection-badge'><span class='connection-dot'></span>Snowflake connected</span></div>",
            unsafe_allow_html=True,
        )
        if not os.getenv("OPENAI_API_KEY"):
            st.warning("OPENAI_API_KEY is missing. Add it to environment or .env before sending a message.")

        chat_status_slot = render_chat_thread(st.session_state.messages, pending=processing_turn, record_work=record_work)
        render_chat_autoscroll(len(st.session_state.messages))

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
                    record_work=record_work,
                )
                status_slot.empty()
            except Exception as exc:
                logging.exception("Agent turn failed")
                reply = "[MOOD:Guarded / Hesitant]\nSorry, I didn’t catch that. Could you say it again?"
                events = [{
                    "tool": "runtime_error",
                    "output": {
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:500],
                    },
                }]
                updated_scope = {}
                status_slot.markdown(render_chat_status("Mikael cannot reach the case file."), unsafe_allow_html=True)
            previous_role = st.session_state.messages[-1].get("role") if st.session_state.messages else None
            reply_message = {"role": "assistant", "content": reply, "tool_events": events, "timestamp": time.strftime("%H:%M")}
            with reply_slot.container():
                st.markdown(
                    f'<div class="chat-shell">{render_chat_row(reply_message, previous_role)}</div>',
                    unsafe_allow_html=True,
                )
                if events:
                    with st.expander("Activity", expanded=st.session_state.show_activity and st.session_state.app_page == "demo"):
                        render_activity(events)
            score_events = audit_notes_from_events(events)
            queue_audit_note_toasts(score_events)

            # Portrait selection is decided once by chat_runtime from the
            # confirmed exact scope. Do not infer special portraits again
            # from individual score events here.
            st.session_state.portrait_override = None
            st.session_state.special_mood_label = None

            mood = updated_scope.get("mood") or extract_mood(reply)
            st.session_state.current_mood = mood
            st.session_state.portrait_key = (
                updated_scope.get("portrait")
                or ("checking_details" if st.session_state.pending_upload_review else None)
            )
            st.session_state.pending_upload_review = False
            st.session_state.messages.append({
                "role": "assistant",
                "content": reply,
                "timestamp": time.strftime("%H:%M"),
                "mood": mood,
                "score_events": score_events,
                "tool_events": events,
            })
            st.session_state.tool_events.extend(events)
            st.session_state.pending_agent_turn = False
            st.session_state.pending_record_work = False
            st.rerun()
        else:
            prompt = st.chat_input(
                "Found something unusual? Point me to the record and tell me what caught your eye.",
                accept_file=True,
                file_type=["png", "jpg", "jpeg", "xlsx", "xls", "csv"],
            )

            if prompt:
                mood_status_slot.empty()
                text = prompt.text or ""
                st.session_state.last_activity_at = time.time()
                st.session_state.idle_message_added = False
                st.session_state.portrait_override = None
                st.session_state.idle_mood_label = None
                st.session_state.special_mood_label = None
                st.session_state.pending_upload_review = bool(prompt.files)
                st.session_state.portrait_key = "examining_data" if prompt.files else None
                image_urls = []
                display_images = []
                spreadsheet_text = ""
                for uploaded in prompt.files or []:
                    content = uploaded.getvalue()
                    if Path(uploaded.name).suffix.lower() in {".xlsx", ".xls", ".csv"}:
                        spreadsheet_text = spreadsheet_entity_text(uploaded.name, content)
                    else:
                        image_urls.append(image_to_data_url(uploaded.name, content))
                        display_images.append(content)

                st.session_state.messages.append(
                    {
                        "role": "user",
                        "content": text,
                        "timestamp": time.strftime("%H:%M"),
                        "images": image_urls,
                        "display_images": display_images,
                        "spreadsheet_text": spreadsheet_text,
                    }
                )
                st.session_state.pending_record_work = True
                st.session_state.pending_agent_turn = True
                st.rerun()




with st.sidebar:
    with st.expander("Settings", expanded=False):
        if st.button("Log out", key="settings_logout", type="secondary"):
            logout()
        if st.button("Open Facilitator View", key="settings_facilitator_view", type="secondary"):
            select_page("facilitator")
        if st.session_state.app_page == "demo":
            st.session_state.show_activity = st.checkbox(
                "Show activity details",
                value=st.session_state.show_activity,
                help="Show the internal parser, graph, scoring, and conversation trace under each Mikael reply.",
            )

if st.session_state.app_page == "home":
    render_home_page()
elif st.session_state.app_page == "facilitator":
    render_facilitator_page(case_data)
elif st.session_state.app_page == "demo":
    render_demo_page(case_data)
else:
    render_participant_page()
