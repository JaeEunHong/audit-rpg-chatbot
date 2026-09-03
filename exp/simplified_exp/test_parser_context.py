import json

import run_experiment


class FakeResponse:
    output_text = json.dumps({
        "entity_mentions": [],
        "requested_access": None,
        "requested_content": None,
        "issue_claims": [],
        "follow_active_context": False,
        "small_talk": False,
    })


class FakeResponses:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return FakeResponse()


class FakeClient:
    def __init__(self):
        self.responses = FakeResponses()


def test_parser_receives_latest_message_history_and_identifier_scope(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(run_experiment, "get_openai_client", lambda: client)
    monkeypatch.setattr(run_experiment, "load_env", lambda: None)

    history = [
        {"role": "user", "content": "Open SE105843."},
        {"role": "assistant", "content": "Mikael overview."},
        {"role": "user", "content": "What was the approval date?"},
        {"role": "assistant", "content": "Mikael approval answer.", "tool_events": [{"secret": "hidden"}]},
        {"role": "user", "content": "What about the approval?"},
    ]
    scope = {
        "contracts": ["SE105843"],
        "customers": [],
        "assets": [],
        "vins": [],
    }

    run_experiment.parse_investigation_request(
        "What about the approval?",
        {"issue_columns": []},
        "gpt-4.1",
        chat_history=history,
        active_investigation_scope=scope,
    )

    items = client.responses.kwargs["input"][0]["content"]
    text = "\n".join(item["text"] for item in items if item["type"] == "input_text")
    assert text.count("What about the approval?") == 1
    assert "Auditor: Open SE105843." in text
    assert "Mikael (Auditee): Mikael overview." in text
    assert "Mikael (Auditee): Mikael approval answer." in text
    assert "tool_events" not in text
    assert "secret" not in text
    assert json.dumps(scope, ensure_ascii=False) in text


def test_parser_history_is_limited_to_previous_ten_visible_messages(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(run_experiment, "get_openai_client", lambda: client)
    monkeypatch.setattr(run_experiment, "load_env", lambda: None)

    history = [
        {"role": "user", "content": f"old-{index}"}
        for index in range(12)
    ] + [{"role": "user", "content": "latest"}]

    run_experiment.parse_investigation_request(
        "latest",
        {"issue_columns": []},
        "gpt-4.1",
        chat_history=history,
        active_investigation_scope={
            "contracts": [], "customers": [], "assets": [], "vins": []
        },
    )

    items = client.responses.kwargs["input"][0]["content"]
    history_text = next(item["text"] for item in items if item["text"].startswith("Recent visible dialogue:"))
    history_lines = history_text.splitlines()
    assert "Auditor: old-1" not in history_lines
    assert "Auditor: old-2" in history_lines
    assert "Auditor: old-11" in history_lines
    assert "latest" not in history_text


def test_parser_accepts_empty_history_and_scope(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(run_experiment, "get_openai_client", lambda: client)
    monkeypatch.setattr(run_experiment, "load_env", lambda: None)

    run_experiment.parse_investigation_request("Open SE105843.", {"issue_columns": []}, "gpt-4.1")

    items = client.responses.kwargs["input"][0]["content"]
    assert any(item["text"] == "Recent visible dialogue:\n(none)" for item in items)
    assert any('"contracts": []' in item["text"] for item in items)


def test_parser_history_uses_placeholder_for_previous_images(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(run_experiment, "get_openai_client", lambda: client)
    monkeypatch.setattr(run_experiment, "load_env", lambda: None)

    run_experiment.parse_investigation_request(
        "What next?",
        {"issue_columns": []},
        "gpt-4.1",
        chat_history=[
            {"role": "user", "content": "Review this screenshot.", "images": ["data:image/png;base64,secret"]},
            {"role": "assistant", "content": "I am looking at it."},
            {"role": "user", "content": "What next?"},
        ],
        active_investigation_scope={
            "contracts": [], "customers": [], "assets": [], "vins": []
        },
    )

    items = client.responses.kwargs["input"][0]["content"]
    history_text = next(item["text"] for item in items if item["text"].startswith("Recent visible dialogue:"))
    assert "Auditor: Review this screenshot.\n<Image attached>" in history_text
    assert "data:image" not in history_text
    assert "secret" not in history_text
