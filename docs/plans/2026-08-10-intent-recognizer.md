# Intent Recognizer Implementation Plan

## Goal

Implement the first EpiEvidence workflow capability: classify a research-task request
without performing terminology resolution or literature retrieval.

## Scope

- Define typed request and decision contracts for research-task intents.
- Implement deterministic rules for the supported intents.
- Return a safe `clarification_required` decision for uncertain requests.
- Reserve an LLM adapter boundary without calling an external provider.
- Test representative Chinese and English requests.

## Tasks

1. Add Pydantic models and intent enums in `skills/intent_router/schemas.py`.
2. Add an intent prompt contract in `skills/intent_router/prompts.py`.
3. Implement `skills/intent_router/recognizer.py`, with input normalization, rule
   matching, confidence, next-stage decisions, and an unimplemented LLM adapter.
4. Add pytest coverage for new research, follow-up, supplementary search, revision,
   export, and ambiguous input.

## Verification

Run `uv run pytest tests/test_intent_recognizer.py` and confirm all cases pass.

## Explicit Non-Goals

- FastAPI routes.
- MeSH matching and bilingual medical-term expansion.
- LangGraph orchestration.
- External LLM calls.
