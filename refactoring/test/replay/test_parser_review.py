from audit_types import AuditRequest
from stage_03_request_parser import parse_request_with_review


def test_parser_review_replaces_incomplete_draft():
    calls = []

    def draft(**kwargs):
        calls.append("draft")
        return '{"entity_mentions": [{"mention_id": "a1", "kind": "asset", "text": "AST510028"}], "requested_access": "secret", "requested_content": "explanation", "issue_claims": [], "follow_active_context": false, "small_talk": false, "context_action": "follow"}'

    def review(**kwargs):
        calls.append("review")
        return '{"entity_mentions": [{"mention_id": "a1", "kind": "asset", "text": "AST510028"}], "requested_access": "secret", "requested_content": "explanation", "issue_claims": [{"mention_id": "a1", "candidate_issue": "INFLATED PRICING", "rationale": "The pricing appears inflated."}], "follow_active_context": false, "small_talk": false, "context_action": "follow"}'

    request = parse_request_with_review("Is AST510028 overpriced?", draft, review)
    assert calls == ["draft", "review"]
    assert request.issue_claims[0]["candidate_issue"] == "INFLATED PRICING"
