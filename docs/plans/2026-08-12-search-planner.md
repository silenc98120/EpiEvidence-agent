# Search Planner Implementation Plan

> Status: approved MVP implementation scope.

## Goal

Convert `IntentRecognitionResult` and `MeshNormalizationResult` into a provider-neutral `SearchPlan`, then deterministically compile that plan for PubMed and Europe PMC.

## Fixed behavior

- Each extracted research keyword becomes one concept group.
- Confirmed MeSH preferred labels are stored separately from free-text terms.
- LLM English candidates, MeSH preferred labels, and Entry Terms are retained as free-text alternatives.
- Terms inside one concept group use `OR`.
- Different concept groups use `AND`.
- PubMed uses `[Mesh]` and `[Title/Abstract]` field tags.
- Europe PMC uses `MESH:` and `TITLE_ABS:` field prefixes.
- Query generation is ordinary Python code; the LLM does not emit database grammar.
- Online retrieval, pagination, fan-out/fan-in, and search broadening are outside this change.

## Files

1. Implement models, builder, escaping, and both compilers in `app/core/search_strategy_generator.py`.
2. Add `search_plan` to `agent/state.py`.
3. Add focused behavior tests in `tests/test_search_strategy_generator.py` after the source implementation.
4. Update `README.md` to reflect the implemented boundary.

## Verification

Use the semaglutide/overweight/weight-loss example to verify that:

- MeSH headings receive controlled-vocabulary field tags;
- Entry Terms and English candidates receive title/abstract field tags;
- alternatives are deduplicated;
- groups are parenthesized and joined with `AND`;
- malformed empty plans and non-research inputs fail explicitly.

