# Project 1: EpiEvidence Design Record

> Status: design baseline, not an implementation-complete claim.
> Updated: 2026-08-12
> Purpose: a shared design record for the epidemiology/public-health evidence briefing Agent. Other workspaces should read this file before changing the architecture.

> [!IMPORTANT]
> **2026-08-17 MVP scope override:** the first release has been narrowed to medical-literature search and recommendation. Intent recognition, MeSH normalization, a provider-neutral SearchPlan builder, deterministic PubMed/Europe PMC query compilers, PubMed/Europe PMC online searchers, the SQLAlchemy ORM, and the PostgreSQL baseline schema are implemented. PostgreSQL repositories, unified result normalization/deduplication, LangGraph retrieval fan-out/fan-in, and abstract relevance ranking are next. Full-text appraisal, evidence synthesis, academic report writing, paper reading, mind maps, and learned/reward-model ranking are deferred. Where older sections below describe a complete evidence briefing workflow, treat them as long-term design context rather than current MVP commitments.

## 0. Current MVP Baseline (2026-08-12)

### Product boundary

The first release helps a user move from a natural-language medical research question to a transparent, reproducible search and a ranked set of candidate papers:

```text
Natural-language query
-> intent and keyword extraction
-> exact local MeSH normalization
-> provider-neutral SearchPlan
-> source-specific query compilation
-> parallel retrieval
-> result normalization and deduplication
-> abstract relevance screening
-> relevance-tiered, recency-ordered recommendations
```

The retrieval pass does not download or parse full text. It records source-reported full-text resources and selects a preferred PDF-first candidate. After abstract screening, the user chooses which papers the PMC OA downloader should retrieve. Full-text parsing and formal methodological appraisal remain deferred.

### Retrieval policy

- PubMed and Europe PMC are default sources.
- ClinicalTrials.gov and medRxiv are enabled when the research question calls for them.
- Semantic Scholar is a supplementary discovery source.
- Google Scholar is deferred because it lacks a stable official API suitable for the initial automated workflow.
- The first retrieval pass stores metadata and abstracts and records OA availability as `available`, `unavailable`, or `unknown`.
- OA availability is not a quality signal and does not exclude a record from search results.

### Ranking policy

Abstract screening measures relevance to the user's research intent; it is not a formal quality assessment. Results are first grouped by relevance (`high`, `medium`, `low`) and then ordered by publication date within each group. When high-relevance results are insufficient, the system broadens the SearchPlan and reruns retrieval rather than silently lowering the inclusion standard.

### Implemented modules

#### Intent Recognizer

`app/core/intent_recognizer.py` currently provides:

- four action categories: `simple_chat`, `clarification_answer`, `new_research`, and `supplementary_search`;
- research-direction extraction;
- structured keyword groups with raw user text, up to three English candidates, and confidence;
- LangChain structured output with Pydantic validation;
- Loguru telemetry for latency, token usage, completion, call failure, and invalid structured output.

Its result is stored in `EvidenceState.intent_analysis`. Terms at this stage remain unverified LLM candidates.

#### MeSH Normalizer

`scripts/build_mesh_index.py` streams `data/MESH.csv.gz` directly into a generated SQLite index. The current local build contains 355,402 concepts and 1,015,169 preferred/entry terms. `app/core/mesh_normalizer.py`:

- uses English candidates only for matching while retaining Chinese raw text for provenance;
- applies NFKC, case-folding, and whitespace normalization;
- performs indexed exact matching on `mesh_terms.normalized_term`;
- returns MeSH ID, preferred label, matched term/type, and all Entry Terms;
- deduplicates matches by MeSH ID;
- records aggregate completion/failure telemetry without logging raw medical terms.

Its result is stored separately in `EvidenceState.mesh_normalization`, preserving the distinction between unverified LLM extraction and locally verified terminology.

### Immediate next stage

The provider-neutral `SearchPlan`, deterministic PubMed/Europe PMC compilers, and both
online searchers are implemented. PubMed retrieval uses ESearch + EFetch and returns
validated title/abstract metadata. Europe PMC retrieval returns validated normalized
records and full-text resource discovery metadata. The immediate next step is to define
one provider-neutral `EvidenceRecord`, then write repositories that atomically create
`search_runs`, retain `source_records`, upsert canonical `articles`, and attach
`full_text_resources`. Cross-source deduplication and LangGraph retrieval fan-out/fan-in
follow after that boundary.

### PostgreSQL persistence baseline (2026-08-14)

`database/schema.sql` now defines backend research tasks, provider-specific search runs,
raw source records, canonical articles, and discovered full-text resources. Raw provider
payloads remain JSONB for provenance; normalized plain-text abstracts and screening
metadata live on canonical articles. Graph State should retain IDs and load article
context just in time instead of copying raw records into global messages.

The DDL was executed against an isolated PostgreSQL 18.4 instance and produced five
tables, five foreign keys, and twenty-five indexes. This is a schema baseline only:
repositories, persistence nodes, Alembic migrations, and production database
configuration are not yet implemented.

## 1. Career Goal And Product Positioning

### Primary goal

Build one finished, demonstrable Agent project for `AI application developer` / `Agent engineer` roles. The project must be usable, measurably faster than a manual first-pass literature review, and able to explain its engineering choices in an interview.

### Product name and positioning

Working name: **EpiEvidence: Public-health Rapid Evidence Briefing Agent**.

It is a research-assistance system that turns a natural-language epidemiology or clinical-research question into a source-linked evidence briefing. It helps users complete a first pass of question clarification, PubMed search, screening, structured extraction, evidence comparison, and report drafting.

It is **not**:

- a diagnostic system;
- an automated clinical-decision or treatment recommendation system;
- a formal systematic review, meta-analysis, GRADE conclusion, or full risk-of-bias assessment engine;
- a replacement for researcher or clinician review.

### Target users

- Public-health researchers and students.
- Clinical research assistants and medical students doing a first literature pass.
- Research teams that need a short, auditable evidence brief before a deeper review.

### User value

The system should reduce repetitive work, not automate final professional judgement:

```text
Natural-language question
-> reproducible search plan
-> candidate studies and evidence table
-> source-linked first draft
-> conflicts, limitations, and evidence-insufficient states
```

The final report must make it easy for a person to inspect sources, revise scope, and decide whether a formal review is needed.

## 2. Scope Decisions

### In scope for the MVP

- New research question, follow-up, supplementary search, report revision, and export intent routing.
- Clinical-research question clarification using PICO.
- Epidemiology question clarification using disease + indicator as required fields, plus at least one of population, time, or place. Multiple indicators are permitted.
- PubMed metadata and abstract retrieval through NCBI E-utilities.
- MeSH/synonym-based query planning, deduplication, relevance screening, and structured evidence cards.
- Source-linked rapid evidence report with transparent limitations.
- Citation verification, bounded semantic repair loops, task-level retries, SSE progress, monitoring, and offline evaluation.

### Explicitly out of scope for the MVP

- Medical diagnosis, individual risk prediction, drug prescription, dosage, or treatment plan.
- Automated clinical conclusions such as a formal "intervention works" claim.
- Full-text-only formal RoB 2, ROBINS-I, NOS, or GRADE assessments without appropriate source access and human confirmation.
- Quantitative meta-analysis and pooled effect estimation.
- A broad general medical knowledge graph or generic chat agent.
- General triage, doctor/hospital ranking, appointment scheduling, or medical report image interpretation.
- Storage of sensitive health profiles or an inferred medical user profile.

### Deferred product directions

- Structured basic lab-report education and pre-visit department navigation can be a separate, tightly bounded project. It must not be branded as diagnosis or full triage.
- Evidence-grounded SFT QA generation can be an export module after evidence cards and verification exist. It must include data quality checks and cannot claim to improve a model unless fine-tuning and baseline-vs-after evaluation are completed.
- A generic administrative/chat Agent is not the flagship project because the scenario is common and its value is harder to measure.

## 3. Why This Is A Controlled Multi-Agent Workflow

`Workflow` and `multi-agent` are not competing architectures.

- LangGraph owns task state, node ordering, pauses, retries, human approval, and terminal states.
- Specialist Agents own bounded reasoning jobs with different prompts, schemas, and quality criteria.
- Deterministic operations remain tools or ordinary nodes, not agents.

The system should be described as a **state-machine-constrained multi-agent evidence workflow**, not as agents freely chatting with each other.

### Appropriate specialist Agents

| Agent | Responsibility |
|---|---|
| Coordinator Agent | Decide the next graph route from task state and intent. |
| Clarification Agent | Ask the minimum high-information questions and form PICO/epidemiology fields. |
| Search Planning Agent | Normalize terminology and build staged PubMed search plans. |
| Screening and Extraction Agent | Judge relevance and extract source-bound evidence fields. |
| Appraisal Agent | Identify methodology signals and state limits of available evidence. |
| Synthesis and Report Agent | Build an evidence-aligned report from evidence cards and claim ledger. |
| Citation Verification Agent | Reject unsupported, mismatched, exaggerated, or out-of-scope claims. |

### Not Agents

- PubMed/PMC/MeSH API calls.
- Rate limiting, caching, deduplication, persistence, and SSE delivery.
- Schema, PMID, DOI, reference, and numeric-format validation.
- Celery retry scheduling.
- Telemetry and evaluation collection.

## 4. System Flow

```text
User request
  -> FastAPI creates or resumes task
  -> TaskContextManager loads task assets
  -> IntentRecognizer identifies task mode
  -> LangGraph Coordinator selects next node
  -> Clarification Agent (if necessary)
  -> Entity normalization and search planning
  -> MCP Tool Manager calls PubMed / PMC / MeSH
  -> Screening and deduplication
  -> Structured evidence extraction
  -> Appraisal and evidence-boundary analysis
  -> Synthesis and report drafting
  -> Citation verification and bounded repair loop
  -> Human confirmation if automation cannot resolve an issue
  -> Final report delivery through SSE/export
  -> Persist artifacts, collect telemetry, run evaluation
```

### Flow contract by step

| Step | Required context | Validation | Processing | Output |
|---|---|---|---|---|
| Task intake | `task_id`, user query, session, output preference | Non-empty query, length, duplicate-request check | Create idempotent task and initial audit event | `TaskInput` |
| Intent routing | Query, short conversation state, existing task state | Valid intent enum; no conflict with current state | Identify new task, clarification answer, follow-up, supplementary search, revision, or export | `IntentDecision` |
| Clarification | Query, confirmed fields, clinical/epidemiology template | PICO for clinical questions; disease + indicator + at least one time/place/population factor for epidemiology | Ask only 1-3 highest-information questions or finalize structured question | `ResearchQuestion` or `ClarificationRequest` |
| Normalization | Structured question, MeSH/synonym data, source policy | Mapping confidence; retain unresolved user term instead of inventing a mapping | Generate concepts, synonyms, eligibility criteria | `NormalizedEntities`, `EligibilityCriteria` |
| Search planning | Entities, criteria, previous queries, source catalog | Valid query grammar, permitted source, date range | Generate broad, precise, and review-priority query variants | `SearchPlan` |
| Retrieval | Search plan, cache, rate-limit state | PubMed response schema, PMID uniqueness, retry classification | ESearch/EFetch, metadata acquisition, response provenance capture | `CandidateStudy[]` |
| Screening | Candidates, eligibility criteria, prior evidence IDs | Stable PMID/DOI deduplication; screening schema and reason required | Rules + ranker + bounded model judgement classify include/exclude/uncertain | `ScreeningResult` |
| Evidence extraction | Included study, abstract/full-text fragments, extraction schema | Each non-empty field needs cited source span; unknown is `not_reported`; numeric/unit formats | Extract design, population, exposure/intervention, outcome, estimates, limitations | `EvidenceCard[]` |
| Appraisal | Evidence cards, source availability, study-type rubric | Rubric fits study type; no formal quality score from insufficient abstract data | Identify bias/limitations signals and need for human/full-text review | `AppraisalResult[]` |
| Synthesis | Evidence cards, appraisal, research question | No invalid pooling; avoid causal claims from observational association; no duplicate cohort counting | Group comparable studies, identify agreement, conflict, and gaps | `ClaimLedger`, `SynthesisPlan` |
| Report draft | Claim ledger, sources, report template | Every critical claim maps to evidence ID; prohibited clinical claims rejected | Produce cited briefing and limitations | `ReportDraft` |
| Verification | Draft, evidence cards, original metadata | Citation identity, effect direction, numbers, support coverage, wording boundary | Deterministic checks + verifier Agent; bounded repair or human escalation | `VerificationResult`, revised draft |
| Delivery and learning | Verified report, preferences, explicit feedback | Version freeze, source integrity, feedback de-identification | SSE/output export; record measurable quality signals | `FinalReport`, `FeedbackRecord` |

## 5. Context, Memory, And RAG Decisions

### The core is evidence retrieval, not broad medical RAG

PubMed retrieval and source-linked evidence extraction are the primary retrieval system. This is retrieval-augmented generation in a broad sense, but it is not a need to ingest the medical web into ChromaDB.

### Storage responsibilities

| Store | Content | Why |
|---|---|---|
| PostgreSQL | Tasks, structured questions, search runs, sources, evidence cards, report versions, validation results, audit log | Durable, queryable, relational evidence assets |
| Redis | Graph state, task locks, rate limits, SSE progress, short-lived cache | Fast recovery and worker coordination |
| ChromaDB or pgvector, optional | User-authorized uploads, allowed open full-text fragments, previous report/evidence semantic lookup | Task-scoped long-document retrieval only |

### Allowed RAG corpora

- Documents uploaded by the user with permission to process.
- Open-access full texts that policy permits the system to retrieve and retain.
- Current-task source fragments, each with PMID/DOI, page/section, chunk ID, and text hash.
- Historical verified report/evidence assets for follow-up questions.
- Small operational corpora: MeSH mappings, report templates, study-design definitions, and evaluation rules.

### Prohibited RAG use

- A heterogeneous web crawl used as untraceable medical truth.
- Using user-profile embeddings to infer medical facts or sensitive traits.
- Allowing a retrieved background paragraph to support a clinical conclusion without a traceable evidence source.

### Research memory

Research memory may store confirmed question fields, successful and failed queries, screening decisions, evidence cards, report versions, and classified failure cases. It must not store free-form medical "facts" as truth or dump raw bad cases into a permanent system prompt.

Bad cases should be recorded as structured classes, for example:

```text
unsupported_claim
citation_mismatch
ambiguous_question
duplicate_sample
unsafe_guidance
schema_error
```

Only a small set of relevant, verified improvement rules should be retrieved in a later task.

## 6. Mapping From EchoMind To EpiEvidence

| EchoMind concept | EpiEvidence adaptation |
|---|---|
| `api/main.py` | FastAPI task creation/resume, SSE progress stream, report/export endpoint |
| `MemoryManager` | `TaskContextManager`, centered on research task assets rather than medical persona |
| Redis working memory | Graph state, cache, locks, job status, rate limit |
| Chroma episodic memory | Optional source-bound task/document index |
| Chroma user profile | Omit for MVP; keep only explicit non-sensitive format/source preferences if needed |
| `IntentRecognizer` | New task, clarification, follow-up, supplementary search, revision, export intents |
| `MCPToolManager` | PubMed, PMC, MeSH, metadata validation, cache, circuit breaker, fallback |
| `knowledge_base` | MeSH/synonym rules, reporting templates, study-design rules, authorized documents, not broad medical truth |
| `AgentOrchestrator` | LangGraph Coordinator and specialist-agent subgraphs |
| `SkillManager` | Fixed, testable domain capabilities selected by state, not generic keyword matching |
| `PerformanceMonitor` | Node latency, tool errors, retries, retrieval quality, token/cost traces |
| `evaluator` | Retrieval relevance, citation consistency, extraction completeness, report review score |

## 7. Reliability, Retries, And Human Review

### Failure categories

| Failure | Handling |
|---|---|
| Network timeout, PubMed rate limit, transient API error | Celery exponential-backoff retry with a finite attempt count |
| Zero retrieval results | Search planner creates at most 2-3 documented query variants |
| Duplicate state on retry | Use stable `evidence_id` keyed merge and node idempotency; do not append blindly |
| Missing field in source | Use `not_reported`; never ask the model to fill it from general knowledge |
| Unsupported report claim | Remove/soften claim or return to supplementation; do not leave unsupported wording |
| Abstract insufficient for appraisal | Mark formal appraisal unavailable and request full text/human review |
| Repeated semantic verification failure | Pause graph and request human confirmation |

### Important current-code issue

`agent/state.py` currently defines `search_results` and `evaluated_evidence` with the `operator.add` reducer. If a node is retried, list entries can be duplicated. The target design must replace this with keyed/idempotent merge semantics based on PMID/DOI and `evidence_id`.

### Retry ownership

- LangGraph conditional edges: semantic/business loop, for example report verification fails and report must be revised.
- LangGraph retry policy: transient node exceptions where appropriate.
- Celery: asynchronous job execution, task-level retry/recovery, and long-running workload management.
- SSE: progress transport only. It does not drive state transitions.

## 8. Report Contract

Every final report should include:

1. Structured research question and scope.
2. Search date, data source, query strategy, and inclusion/exclusion criteria.
3. Candidate, deduplicated, included, excluded, and uncertain study counts.
4. A structured evidence table.
5. Main findings, with each material claim linked to evidence IDs.
6. Conflicting findings and possible sources of heterogeneity.
7. Evidence gaps and methodological limitations.
8. Reference list containing PMID, title, journal, year, and source link.
9. Boundary statement: rapid evidence briefing only, not a systematic review or clinical decision.

## 9. Evaluation And Demonstration Metrics

The project should make its value measurable instead of merely claiming that it is "better than manual".

| Metric | Example MVP target |
|---|---|
| First draft latency | A pre-defined test question produces a first report within 3 minutes, excluding human review |
| Citation consistency | Manual sample audit of material claims finds >= 90% correct source linkage |
| Structured extraction completeness | Required fields either have a cited source span or are explicitly `not_reported` |
| Retrieval relevance | Label a small query set and measure relevance/recall at selected top-K values |
| Safety and boundary behavior | Evidence-insufficient, conflict, and out-of-scope cases produce restrained output |
| Operational reliability | Track tool failure rate, retry success rate, duplicate rate, and per-node duration |

An evaluation set should include normal successful cases, no-result cases, contradictory literature, missing PICO/epidemiology fields, metadata failures, and citation-mismatch adversarial cases.

## 10. Technology Direction

| Concern | Proposed direction |
|---|---|
| API | FastAPI |
| Graph orchestration | LangGraph |
| LLM provider | Provider abstraction / model gateway supporting OpenAI-compatible APIs and multiple vendors |
| Background execution | Celery + Redis |
| Durable storage | PostgreSQL |
| Cache/state/locks | Redis |
| Evidence retrieval | PubMed/NCBI E-utilities, optional permitted PMC full text |
| Validation | Pydantic v2, deterministic source/identifier checks |
| Streaming | SSE |
| Observability | Langfuse is preferred for a self-hostable/free-oriented path; LangSmith is compatible with LangGraph but may create cost constraints |
| Vector retrieval | Defer until task-scoped document retrieval demonstrates a real need |

## 11. Current Repository Reality

The repository currently contains an early skeleton, not a finished Agent:

- `main.py` only prints a greeting.
- `agent/graph.py` has no graph nodes or edges.
- `agent/state.py` contains an initial partial state model.
- Intent, evidence-search, and quality-evaluator prompt/schema folders exist but are not yet a completed end-to-end workflow.
- `README.md` describes aspirational architecture such as SQLite, ChromaDB, Langfuse, and evaluation, but the current `pyproject.toml` only declares LangChain, LangGraph, LangChain OpenAI, and Pydantic.

Future work should not represent design-only modules as implemented in a resume or README.

## 12. Recommended Build Order

1. Define Pydantic contracts and a typed, idempotent `EvidenceState`.
2. Implement task API, basic SSE status, and PostgreSQL task persistence.
3. Implement question clarification and MeSH/PubMed search planning.
4. Implement PubMed retrieval, deduplication, and a source provenance model.
5. Implement screening and evidence-card extraction with span-level citations.
6. Implement report generation from evidence cards, then citation verification.
7. Add controlled retry loops, Celery/Redis asynchronous execution, and human-review pause/resume.
8. Build a compact, labeled evaluation suite and Langfuse instrumentation.
9. Add task-scoped RAG only for uploads or permitted full-text documents.
10. Polish the UI/demo, record metrics, and prepare interview examples for failure/retry/citation cases.

## 13. Interview Narrative

The strongest narrative is not "I built many agents." It is:

> I built a constrained multi-agent evidence workflow for public-health literature briefing. The graph uses specialist agents only for semantic tasks, while deterministic tools own retrieval, validation, persistence, and retries. Each final claim is linked to a source artifact, and the system can represent uncertainty, conflict, and insufficient evidence rather than fabricating a clinical conclusion.

Topics to prepare with concrete examples:

- Why LangGraph is needed instead of a single LLM call.
- How transient technical retries differ from semantic repair loops.
- How retries avoid duplicate studies and repeated state updates.
- Why authoritative live retrieval is primary and broad medical RAG is not.
- How citation verification and evidence IDs reduce unsupported claims.
- How the system is evaluated and where it deliberately stops.

## 14. External Reference Principles

- NHS 111 is useful as a product-boundary reference because it explicitly directs users to appropriate help rather than claiming diagnosis: <https://111.nhs.uk/>.
- Infermedica and Ada are useful as proprietary product-design references, not as reusable open-source data/code.
- HPO, MONDO, MeSH, LOINC, and MedlinePlus cover terminology or educational layers, not a plug-in replacement for a full clinical-triage engine.
- Public accessibility does not automatically grant bulk ingestion, re-publication, or model-training rights. Verify licences and terms before adding any external corpus.
