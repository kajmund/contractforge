Generate one synthetic contract document for the ContractForge dataset.

Document title: [[DOCUMENT_TITLE]]
document_id: [[DOCUMENT_ID]]
Document kind: [[DOCUMENT_KIND]]
Contract type: [[CONTRACT_TYPE]]
Language: [[LANGUAGE_NAME]] ([[LANGUAGE_CODE]])
Jurisdiction: [[JURISDICTION]]
Difficulty: [[DIFFICULTY]] of 6
Complexity: [[COMPLEXITY]]
Style: [[STYLE]] | directness: [[DIRECTNESS]] | structure: [[STRUCTURE]]
Document quality: [[DOCUMENT_QUALITY]] | distractor count: [[DISTRACTOR_COUNT]]
Dependency modes: [[DEPENDENCY_MODES]] | reference modes: [[REFERENCE_MODES]]
Attempt: [[ATTEMPT]]
Planned clause count: [[CLAUSE_COUNT]]

## Parties

[[PARTIES_JSON]]

## Variation profile

[[VARIATION_JSON]]

## Clause plan (exactly these clauses, in this order)

[[CLAUSE_PLAN]]

## Facts (the only material truth)

[[FACTS_JSON]]

## Anomalies that must be reproduced

[[ANOMALIES_JSON]]

## Missing documents and references

Missing documents that must not be invented: [[MISSING_DOCUMENTS]]
Documents referenced by this document: [[DOCUMENT_REFERENCES]]

## Feedback from the previous attempt

[[FEEDBACK]]

## Output requirements

- Return exactly one JSON object. No markdown fences, no prose before or after.
- The object must have exactly this structure and these ids:

[[OUTPUT_SCHEMA]]

- `document_id` must be exactly `[[DOCUMENT_ID]]`.
- Every planned `clause_id` must appear exactly once, in the planned order.
- `heading` and `text` must be non-empty strings written in
  [[LANGUAGE_NAME]].
- Clause text must be continuous legal prose: no lists of ids, no metadata, no
  field names, no JSON, no invented material terms.
- Use the supplied `explicit_surfaces` verbatim in `explicit` clauses and do
  not translate figures, amounts, dates or defined terms.
