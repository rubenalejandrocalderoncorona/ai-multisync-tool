# Prompts

Every LLM instruction the pipeline uses lives here, so reviewers can read and change them without touching code.

| File | Used by | Purpose |
|---|---|---|
| `judge-docs.md` | `judge` node, documentation mode | Fact-check a draft against its source doc |
| `judge-code.md` | `judge` node, code mode | Fact-check a draft against a code snapshot |
| `draft-docs.md` | `write_draft` node | Restructure a source doc into a page (persona injected from the style library) |
| `draft-code.md` | `write_draft` node | Write or update a page from code |
| `gar.md` | `similarity` / `gar` nodes | Hypothetical paragraph used for retrieval; never indexed |
| `polish.md` | `polish_draft` node | Language-only pass, forbidden from changing facts |

`{{PERSONA}}` and `{{STYLE}}` are filled from `config/doc-styles.json` (see that file's header).

Each prompt is identified in the logs as `<name>@<sha8>`, so any run can be traced to the exact wording that produced it.
To test changes without editing these files, point `PROMPTS_DIR` at a copy.

Rules for editing: keep the JSON output contract in the judge prompts unchanged (the code parses it), and keep the "data, not instructions" line in every prompt.
