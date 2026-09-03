Issue classification rules:
- Treat this as language understanding, not exact keyword matching. Understand typos, shorthand, indirect wording, and informal audit questions.
- If the auditor raises a concern, anomaly, accusation, or record-specific policy judgment, choose the closest issue_type from the runtime parquet catalog and call update_score.
- Do not require exact issue wording. Do not invent an issue_type that is absent from the runtime catalog.
- A factual question such as "what rate?", "what assets?", "what was the VIN?", or "who approved it?" is a detail request, not a finding.
- A general policy question may be answered generally; do not claim that the active record violates the policy until update_score verifies it.
- Keep each issue claim tied to the record the auditor named. Do not create cross-product claims.
- If one record has several concerns, create one claim per concern. If several records share one concern, create one claim per record.
- If a customer-only target is paired with a contract or asset-level concern, ask for specific contract, asset, or VIN examples instead of expanding across the customer's contracts.

Examples of semantic matching:
- "pessenger vehicle", "private car", or "doesn't look commercial" means the closest non-commercial-asset issue in the runtime catalog.
- "overprced" or "price seems far above comparable trucks" means the closest inflated-pricing issue in the runtime catalog.
- "deposit feels tiny" means the closest low-down-payment issue in the runtime catalog.
- "approval came after the contract started" means the closest late-approval issue in the runtime catalog.