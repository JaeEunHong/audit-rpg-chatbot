# Audit Chat Flow

This document explains what happens from the auditor's message to Mikael's
answer and the Activity panel.

The main rule is simple:

> Python decides what the data means. LLM2 decides how Mikael says it.

LLM2 must not change the issue, score, entities, or finding result.

## 1. The auditor sends a message

The message can come from:

- normal chat text;
- a screenshot;
- an Excel file;
- a group of contract, customer, asset, or VIN IDs.

The app keeps the current conversation state, including:

- the records currently being discussed;
- the last issue;
- the current pressure level;
- whether this conversation already has a confirmed finding.

## 2. The app reads the input

For normal text, the app sends the message to LLM1.

For a screenshot, the visual reader first extracts visible text and IDs.

For an Excel file, Python reads the rows and IDs directly.

The result is one text request plus any IDs found in the input.

## 3. LLM1 identifies the request

LLM1 reads:

- the auditor's latest message;
- the available issue list;
- a small amount of previous conversation context;
- any IDs already found by the app.

LLM1 returns a structured request containing:

- the requested action, such as assess, explain, or lookup;
- the possible issue;
- the mentioned entities;
- selections such as “the first 20 contracts”;
- whether the auditor changed the issue or continued the previous one.

LLM1 does not score anything and does not write Mikael's answer.

## 4. Python cleans and checks the request

Python then:

- normalizes issue names;
- keeps valid issue names from the issue list;
- keeps explicit IDs found in the message;
- carries the previous issue forward for short follow-up questions such as
  “but how?”;
- detects a new issue or a new group of entities;
- rejects a request when several concerns are mixed and cannot be separated;
- asks for a smaller group when the selection is too large.

This produces the resolved request for the current turn.

## 5. Python connects the entities

The app follows the data relationships.

Examples:

- asset → contract;
- VIN → every contract using that VIN;
- contract → customer;
- customer → related contracts;
- customer or contract → related assets.

This is why a message mentioning two asset IDs can become two contract IDs
before scoring.

Python also selects the relevant public and private case information.

## 6. Python checks whether the issue can be assessed

The app compares the selected records with the selected issue.

The result is one of these:

- `new_score`: the issue is confirmed for new records;
- `repeat`: the issue was already scored for these records;
- `unsupported`: the selected records do not support the issue;
- `mixed_issue`: some selected records support it and some do not;
- `clarification`: the request needs more information;
- `not_found`: the requested records could not be found.

Important distinction:

- all records unsupported = `unsupported`;
- some confirmed and some unsupported = `mixed_issue`.

Mixed findings are not changed into unsupported.

## 7. Python updates the score and pressure

For every new confirmed finding, the score increases.

For unsupported findings, pressure decreases according to the existing scoring
rule. Repeated findings do not add the same score again.

Pressure is cumulative for the conversation.

The current attitude stages are:

- pressure `0`: confident;
- pressure `1–50`: embarrassed;
- pressure `51–150`: guarded;
- pressure `151–400`: defensive;
- pressure `401–900`: nervous;
- pressure above `900`: defeated.

The first confirmed finding is therefore embarrassed even when the starting
pressure came from an earlier team or session update.

## 8. Python creates the decision result

The decision result is the single source for the audit outcome.

It contains:

- the result status;
- the finding list;
- confirmed count;
- unsupported count;
- repeat count;
- score change.

This result is used by both the Activity panel and the response preparation.

## 9. Python creates the evidence package

The evidence package contains the facts that Mikael may use:

- the issue;
- the selected entity samples;
- confirmed and unsupported counts;
- score change;
- public narrative samples when needed;
- private explanations for confirmed findings;
- the selected tone information.

Python does not invent a summary of the evidence. It passes the relevant data
and narrative samples through.

## 10. Python creates the response policy

The response policy controls only how Mikael speaks.

It contains:

- `tone`: embarrassed, guarded, defensive, nervous, defeated, or annoyed;
- `attitude_style`: the general attitude in plain words;
- `rhythm_level`: how smooth or hesitant the speech should be;
- allowed visible moods.

The rhythm levels are relative:

- `0`: fluent and direct;
- `1`: mostly fluent with a small awkward qualification;
- `2`: cautious with a mid-thought qualification;
- `3`: uncertain with corrections or unfinished thoughts;
- `4`: tired, reluctant, and less polished.

These values are not shown to the auditor.

## 11. LLM2 writes Mikael's answer

LLM2 receives:

- the latest auditor message;
- the evidence package;
- the response policy;
- a small recent dialogue window used only to avoid repeating the same wording.

LLM2 may change:

- sentence rhythm;
- pauses;
- self-corrections;
- level of defensiveness;
- visible mood;
- portrait choice.

LLM2 may not change:

- the issue;
- the entities;
- the finding status;
- the score;
- the evidence;
- the approval facts.

The recent dialogue window is only a style reference. It is never used for
scoring or as a replacement for the evidence package.

## 12. The app validates the LLM2 result

The app checks that the returned mood is allowed for the selected tone.

If the model returns a mood that does not match the policy, Python uses the
allowed mood instead.

The conversation state then stores the current response tone for the next
turn.

## 13. The Activity panel is built

Activity is built from the same Python result used by LLM2.

It shows the important facts in a compact order:

- status and state;
- action;
- issue;
- entities;
- confirmed and unsupported findings;
- pressure and attitude stage;
- tone change;
- score and score change;
- response;
- evidence details when Activity is opened.

The Activity panel does not calculate a separate score and does not use a
separate issue decision.

## 14. The next message continues from the saved state

After the answer, the app saves:

- the selected entities;
- the current issue;
- the pressure;
- the current response tone;
- whether a confirmed finding has already occurred.

Therefore:

- “but how?” keeps the previous issue;
- a new issue replaces the previous issue;
- a repeated check does not score the same finding twice;
- a new confirmed finding can increase pressure;
- an unsupported or mixed result changes the tone without changing the facts.

## One-line view

```text
Auditor message
  → input reader
  → LLM1 request
  → Python resolves IDs and issue
  → Python checks and scores evidence
  → DecisionResult
  → EvidencePackage + response policy
  → LLM2 writes Mikael's words
  → Activity and chat display the same result
```
