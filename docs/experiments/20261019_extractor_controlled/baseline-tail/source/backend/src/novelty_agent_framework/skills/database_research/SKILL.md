---
name: database_research
description: Retrieve traceable scholarly evidence for a bounded research task using the database-first path, including source acquisition and Reader grounding.
---

# Database research

Use for scholarly research tasks in every language. Follow:

Database → source artifact → text → Reader → Evidence

- Use registered databases and the supplied SearchPlan. Reference search provides candidate leads, not proof of database coverage.
- Prefer publisher or repository source documents and text extracted from them. Preserve available URL, source record, artifact and extraction lineage; never claim an original file was saved when only extracted text is available. Full original-artifact storage is outside this phase.
- Read a returned readable artifact before further discovery. Evaluate existing unread candidates before expanding queries. Metadata and snippets are not Evidence; a source-authored abstract may support only what it explicitly states.
- Quote exact successful Reader spans. LLM summaries, translations and generated interpretations are derived information, not original Evidence. Do not quote them as source text.
- Emit semantic drafts only; the Builder assigns provenance and database_evidence for verified database sources. Subject references keep their own namespace and do not imply successful database retrieval.
- If no papers can be retrieved, record the limitation and optionally use web_supplement for search advice only. Existing paper candidates with insufficient support do not qualify as no papers. Distinguish provider failure from successful zero hits and from unread or unsupported candidates.
- Reserve time for finalization. A candidate need not produce a card; explain gaps rather than fabricate evidence or conclude absence of prior work.
