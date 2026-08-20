# Project Development Instructions

## Specification-Driven Development

This project uses Specification-Driven Development (SDD) with an OpenSpec-style workflow.

Before implementing a feature or changing behavior:

1. Describe the goal, scope, module boundaries, data contracts, lifecycle, and error behavior.
2. Record the approved design in the relevant `docs/superpowers/specs/` document or an OpenSpec document when one exists.
3. Implement by module or vertical slice, keeping the public contracts explicit.
4. Verify the completed module with focused tests and manual/API checks appropriate to its risk.

## Testing Policy

Do not use test-driven development as the default workflow. Do not create a separate test-first cycle for every small class, validator, or helper.

Prefer:

- One focused test module for each meaningful module or vertical slice.
- Tests for especially important functions, state transitions, persistence boundaries, and user-facing API contracts.
- End-to-end or integration checks for flows that cross the API, task runner, event stream, and persistence layers.
- Manual verification for simple wiring and configuration changes when automated coverage would add little value.

Tests should validate the approved specification and public behavior, not dictate implementation details.

## Collaboration Style

When the user asks to be taught step by step, do not implement the requested code autonomously. First explain the overall design of the current part, then explain the purpose and structure of the next small step, show only the code or command needed for that step, and wait for the user to complete or confirm it before continuing. Guide the user through the implementation instead of taking ownership of the whole edit.

When teaching or implementing a change without an explicit step-by-step teaching request, explain the reason for the module or function before showing or editing its code. Keep implementation steps small enough to review, but do not split one coherent module into excessive micro-steps.

## Current API Context

The first API milestone is a local-only FastAPI research workbench without JWT authentication. The initial implementation may use in-memory task state and event delivery, with dependency boundaries that can later be replaced by PostgreSQL, Redis, and authentication.
