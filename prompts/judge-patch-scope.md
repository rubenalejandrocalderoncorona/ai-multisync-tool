PATCH SCOPE (this overrides the steps above where they differ). The page was PATCHED: only some sections were written for this change, the rest is an earlier, already approved page that is carried over word for word. You receive:
- CHANGED_SECTIONS: the sections written for this change. This is the text you judge, in place of DRAFT.
- UNCHANGED_PAGE: the carried-over sections. Read-only context, never the text under evaluation.
- CHANGE_SCOPE: the changed files, the changed public symbols and the source diff.

Rules for a patched page:
1. CLAIMS: split only CHANGED_SECTIONS into claims. Do not list claims from UNCHANGED_PAGE in "claims".
2. FACTS: list only the facts the change is responsible for: facts whose evidence lies in the CHANGED_FILES or the changed symbols, facts the source diff adds, changes or removes, and facts that CHANGED_SECTIONS mention. Do NOT list a fact merely because CODE shows it and the page does not state it; the unchanged sections were never required to cover every current fact. A fact that CHANGED_SECTIONS state correctly is covered. A fact the diff changes that neither CHANGED_SECTIONS nor UNCHANGED_PAGE states correctly is NOT covered.
3. CARRIED OVER: list in "carriedOver" up to 10 claims of UNCHANGED_PAGE that CODE contradicts or does not support, as {"text":"","supported":false}. They are only reported to a reviewer and never fail the page. Leave it empty when there are none.
4. STYLE and QUALITY: judge the page as a whole, but never let UNCHANGED_PAGE wording lower the factual checks.

The JSON object has one more optional key: "carriedOver":[{"text":"","supported":false}].
