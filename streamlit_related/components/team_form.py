from __future__ import annotations

from typing import Any, Callable

import streamlit as st


def render_team_picker(
    teams: list[dict[str, str]],
    create_team: Callable[[str], dict[str, str]],
) -> dict[str, str] | None:
    st.subheader("Join your team")
    options = {team["team_name"]: team for team in teams}
    if options:
        selected = st.selectbox("Existing team", ["Select a team"] + list(options))
        if selected != "Select a team" and st.button("Continue with team", use_container_width=True):
            return options[selected]

    st.markdown("Or create a new team")
    with st.form("create_team_form", clear_on_submit=True):
        name = st.text_input("Team name", placeholder="Nordics review team")
        submitted = st.form_submit_button("Create team", use_container_width=True)
    if submitted:
        try:
            return create_team(name)
        except Exception as exc:
            st.error(str(exc))
    return None
