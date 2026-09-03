from __future__ import annotations

import run_experiment
from shared.audit_types import AuditRequest


def make_request(action, mentions):
    return AuditRequest(mentions, None, None, [], False, False, action)


def base_case_data():
    return {
        "contracts": {"SE105792": {}},
        "customers": {"CUST0014": {}},
        "asset_to_contract": {"AST510028": "SE105792"},
        "vin_to_contract": {"VIN001": "SE105792"},
    }


def test_follow_keeps_scope_unchanged():
    scope = {"contracts": ["SE105792"], "customers": [], "assets": [], "vins": []}
    result = run_experiment.apply_context_action_to_scope(
        make_request("follow", [{"mention_id": "e1", "kind": "contract", "text": "SE100016"}]),
        base_case_data(),
        scope,
    )
    assert result == scope


def test_merge_adds_contract_without_duplicates(monkeypatch):
    monkeypatch.setattr(
        run_experiment,
        "resolve_record_references_from_text",
        lambda case_data, text: {"contracts": ["SE105792"], "customers": [], "resolved_from_assets": [], "resolved_from_vins": [], "resolved_from_names": []},
    )
    scope = {"contracts": ["SE105792"], "customers": [], "assets": [], "vins": []}
    result = run_experiment.apply_context_action_to_scope(
        make_request("merge", [{"mention_id": "e1", "kind": "contract", "text": "SE105792"}]),
        base_case_data(),
        scope,
    )
    assert result == scope


def test_replace_replaces_scope_with_new_contract(monkeypatch):
    monkeypatch.setattr(
        run_experiment,
        "resolve_record_references_from_text",
        lambda case_data, text: {"contracts": ["SE105792"], "customers": [], "resolved_from_assets": [], "resolved_from_vins": [], "resolved_from_names": []},
    )
    result = run_experiment.apply_context_action_to_scope(
        make_request("replace", [{"mention_id": "e1", "kind": "contract", "text": "SE105792"}]),
        base_case_data(),
        {"contracts": ["SE100016"], "customers": ["CUST0014"], "assets": [], "vins": []},
    )
    assert result == {"contracts": ["SE105792"], "customers": [], "assets": [], "vins": []}


def test_merge_keeps_asset_reference_without_parent_expansion(monkeypatch):
    monkeypatch.setattr(
        run_experiment,
        "resolve_record_references_from_text",
        lambda case_data, text: {"contracts": [], "customers": [], "resolved_from_assets": [{"asset_id": "AST510028"}], "resolved_from_vins": [], "resolved_from_names": []},
    )
    result = run_experiment.apply_context_action_to_scope(
        make_request("merge", [{"mention_id": "e1", "kind": "asset", "text": "AST510028"}]),
        base_case_data(),
        {"contracts": [], "customers": [], "assets": [], "vins": []},
    )
    assert result == {"contracts": [], "customers": [], "assets": ["AST510028"], "vins": []}


def test_replace_without_entities_clears_scope():
    result = run_experiment.apply_context_action_to_scope(
        make_request("replace", []),
        base_case_data(),
        {"contracts": ["SE105792"], "customers": ["CUST0014"], "assets": [], "vins": []},
    )
    assert result == {"contracts": [], "customers": [], "assets": [], "vins": []}