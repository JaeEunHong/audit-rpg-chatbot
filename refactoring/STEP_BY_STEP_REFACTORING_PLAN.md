# Step-by-step refactoring plan

This work stays inside refactoring/. Existing app.py, main/, and test/ must not
be changed until the isolated implementation passes its tests.

Work one stage at a time: inspect current behavior, add a focused test, make
the smallest change, run the test, and review the result.

## 0. Freeze the baseline

- Run the existing tests.
- Run the refactoring tests.
- Confirm production files are unchanged.

## 1. Centralize case data

File: main/core/stage_01_case_data.py

This is the only parquet reader. It loads entity_master.parquet and
llm_review_index.parquet and builds one case_data object containing contracts,
customers, asset_to_contract, vin_to_contract, relationship sets, the issue
catalog, and normalized issue values.

Check:

- AST510028 maps to SE105792.
- SE105792 maps to CUST0312.
- SE105792 has its complete asset and VIN sets.
- Streamlit caching remains at the application boundary, not in this loader.

## 2. Verify entity resolution

File: main/core/stage_04_entity_resolution.py

Check all directions:

- asset to contract, customer, and VIN set
- VIN to contract and customer
- contract to customer, assets, and VINs
- customer to contracts, assets, and VINs

Keep the original asset or VIN in the resolved target. Scoring may use the
owning contract, but the original input must not be lost.

Do not add issue or scoring logic here.

## 3. Make verification states explicit

File: main/core/stage_05_verification.py

Check these outcomes:

- no entity: missing_entity or missing_entity_and_issue
- known entity plus normal fact request: lookup
- known entity plus vague concern: missing_issue
- known entity plus catalog issue: ready_for_scoring
- ambiguous entity: ambiguous_entity
- unknown entity: not_found
- customer plus multiple contracts plus contract issue: needs_contract_examples

Examples:

- AST510028 alone: missing_issue when no content is requested.
- Tell me about AST510028: lookup.
- Is the pricing for AST510028 inflated?: ready_for_scoring.

## 4. Verify scoring

File: main/core/stage_06_scoring.py

Check:

- true issue and no ledger entry: new_score
- true issue and existing ledger entry: repeat
- false issue: unsupported
- unknown issue: unsupported

The acceptance case is:

AST510028 → SE105792 → INFLATED PRICING → new_score → score_delta 1

The same request with the same ledger must return repeat and score_delta 0.

## 5. Clean the request parser

Files: stage_03_request_parser.py and its numbered parser prompt.

The parser identifies entities, concerns, entity-to-concern pairing, lookup or
investigation intent, and context action.

The parser does not check issue truth, calculate scores, replace asset IDs with
contract IDs, or invent issues outside the runtime catalog.

Check these distinctions:

- Tell me about AST510028: overview and no issue claims.
- AST510028 looks strange: explanation and no claim if no catalog issue fits.
- Is the pricing for AST510028 inflated?: explanation and INFLATED PRICING claim.

## 6. Add parser review handling

Parser review is a second parser call only when the first result is incomplete
or unclear. The review must not revive an unrelated old entity or issue.

Flow:

Parser draft → review required? → parser review → Python verification

## 7. Move visual extraction into its own stage

Files: stage_02_visual_extraction.py and its numbered prompt.

The visual stage converts an image into a complete visible entity table. It
reads every visible row, preserves row order, omits unreadable values, and does
not classify or score issues.

## 8. Clean response generation

Files: stage_07_response.py and its numbered prompt.

The generator receives ResponseContext and returns JSON with mood and speech.

Allowed sources:

- lookup: public material
- new_score and repeat: verified result and approved material
- clarification: clarification state
- unsupported, not_found, needs_contract_examples, and small_talk: their supplied state

Do not expose internal scorecards, ledgers, raw issue labels, or unverified
findings.

## 9. Connect the pipeline

File: stage_08_audit_pipeline.py

The pipeline only controls this order:

optional visual extraction → parser → optional parser review → entity
resolution → verification → scoring → response context → generator

It must not duplicate resolver, verification, or scoring decisions.

## 10. Add replay coverage

Directory: test/replay/

Replay:

- lookup followed by a vague question
- issue followed by why
- repeated asset issue
- customer issue requiring a contract example
- multiple active records and a singular pronoun
- new screenshot with no stale issue reuse
- parser draft followed by parser review

Use fixed parser and visual outputs. Do not call live LLMs in tests.

## 11. Compare before migration

Run the same scenarios through the existing and refactored implementations.
Compare resolved record, verification state, issue result, score delta, repeat
behavior, and clarification type. Compare wording only after structured results
match.

## 12. Apply the migration last

Only after all unit and replay tests pass, use OVERWRITE_MAP.md to copy the new
implementation. Review every replacement manually. Do not automatically delete
or overwrite audit_rpg.py or run_experiment.py.
