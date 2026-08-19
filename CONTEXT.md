# EpiEvidence Domain Glossary

## Publication Type

A category reported by a literature database for a publication, such as Journal Article,
Review, or Clinical Trial. It is source metadata and may be broad.

Not the same as: Study Design.

## Study Design

The methodological design used by a study, such as randomized controlled trial, cohort
study, or case-control study. It may need to be inferred during abstract or full-text
screening and must not be guessed from a broad Publication Type alone.

## Open Access

The legal access status of an article or full-text resource. Open Access does not by
itself guarantee that a direct downloadable PDF link is present.

Not the same as: Full-text Availability.

## Full-text Availability

Whether at least one usable full-text resource is known for an article. A DOI landing
page alone does not establish Full-text Availability.

## Full-text Resource

One concrete representation of an article's full text. Each resource has its own URL,
format, access status, and download status. One article can have multiple resources,
such as both HTML and PDF.

## Conversation

A front-end chat container that may contain multiple Research Tasks.

## Research Task

One independent research request created by the backend from a user query. Clarification
and supplementary searches can continue the same Research Task.

Not the same as: Conversation or Search Run.

## Search Run

One actual provider-specific search execution within a Research Task. Retrying or
supplementing a search creates another Search Run without overwriting the earlier run.

## Source Record

One raw record returned by an external literature provider during a Search Run. It keeps
the provider's identifier and provenance and may later resolve to a canonical Article.

## Article

The canonical representation of one deduplicated publication inside EpiEvidence. One
Article may be supported by multiple Source Records from different providers or runs.
