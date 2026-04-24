---
applyTo: '**'
---
# Documentation Generation Standards

## Critical Content Preservation Rules
- NEVER delete, modify, change, or rephrase any provided source information
- ONLY allowed operations: reorder content, add new context sections, add explanatory headings
- Preserve all technical details, commands, URLs, and specific terminology exactly as provided
- Maintain all original formatting for code blocks, commands, and configuration examples
- Keep all names, emails, project identifiers, and system references unchanged

## File Format Standards
- Use .md for standard documentation without React components
- Use .mdx for documents requiring React components (ZoomableImage, Tabs, TabItem)
- Always include proper frontmatter with id, title, and sidebar_position
- Use consistent heading hierarchy starting with ## (never use single #)
- Include required imports at top of MDX files

## Template Selection Logic
- SOP Template: For step-by-step procedures, operational guides, troubleshooting, how-to guides
- Technical Concept Template: For architectural explanations, system overviews, conceptual documentation
- Default Template: When content does not clearly fit any specialized template

## Markdown Formatting Standards
- Commands and code: Use ```bash, ```yaml, ```json code blocks with proper language tags
- File paths and inline code: Use `backticks`
- UI elements and important terms: Use **bold**
- Emphasis: Use *italics* sparingly
- Lists: Use consistent bullet points (-) or numbers (1.)
- Tables: Use proper markdown table formatting with headers

## Callout Standards
- Use Docusaurus callout format: :::info, :::warning, :::danger, :::tip
- Always include descriptive titles for callouts
- Preserve exact callout content from source material

## Content Organization Requirements
- Always start with Description section explaining the document's purpose
- Group related information under logical headings with consistent numbering
- Include Prerequisites section for procedural documents
- Add Safety Checks or Risk sections where applicable
- Include Troubleshooting sections for operational guides

## Required Documentation Sections
- Frontmatter: id, title, sidebar_position (and imports for MDX)
- Description: Brief overview of document content
- Main content: Organized with clear headings
- References: Links to related documentation and external resources
- Change History: Table with date, version, author, change description, and completeness status

## Handling Missing Content — CRITICAL RULE

The output document must contain ONLY content derived from the source file. Apply these rules without exception:

1. **Delete empty sections** — If the source file has no information for a template section (e.g. Security Model, References, Step-by-Step Guide), delete that section and its heading entirely. Do not write `<!-- No information available -->`, do not write placeholder text, do not leave the heading with an empty body.

2. **Delete template instructions** — Any line in the template that is an instruction to the author (e.g. "Break down the main parts...", "Describe the system...", "Insert diagram here", italicised guidance text) must be removed. These are authoring prompts, not content.

3. **Delete template title placeholders** — If the document title is a template placeholder like `# [Technical Concept Template]` or `# [Default Template]`, replace it with the actual document title derived from the source content.

4. **Never invent content** — Every fact, name, step, reference, risk, command, or example in the output must come directly from the source file. Do not assume, infer, or fabricate anything not explicitly stated in the source.

5. **A shorter accurate document beats a complete-looking fabricated one** — It is always correct to output a document with only 3 sections if that is all the source supports.

**What counts as "missing content"**: A section is missing if the source file contains no information that belongs in it. Paraphrasing source content into a different section is allowed. Inventing content to fill a section is not.

## Change History Table
Always include a Change History table at the end of the document, even if the source has no history. Use this format:

| Date | Version | Author | Change Description | Completeness |
|------|---------|--------|--------------------|--------------|
| YYYY-MM-DD | 1.0 | Author Name | Initial sync from source repository | Fully filled / Partially filled |

- Set **Completeness** to `Fully filled` if all standard template sections had source content to populate them.
- Set **Completeness** to `Partially filled` if one or more sections were deleted because the source lacked content for them. In that case, add a note listing which sections were omitted (e.g., `Partially filled — Security Model, References omitted`).
- Use the actual author name from the source document if available; otherwise write `Documentation Bot`.

## Folder Structure

Place each synced file into one of these subfolders within the service directory:

- **how-to-guides** — step-by-step task instructions
- **configuration-field-reference** — config fields, parameters, and options
- **features** — feature descriptions and capabilities
- **setup-guides** — installation, deployment, onboarding
- **reference** — API reference, CLI reference, data dictionaries
- **concepts** — conceptual explanations, architecture overviews
- **tutorials** — end-to-end learning walkthroughs
- **troubleshooting** — error resolution, FAQs, debugging guides

If the file is a top-level overview or introduction for the entire service, place it at ROOT (no subfolder).

## Naming Conventions
- File names: Use kebab-case with descriptive names
- Document IDs: Match filename without extension
- Titles: Clear, descriptive, include relevant service/product names
- Headings: Use sentence case, be descriptive and scannable

## Cross-Reference Standards
- Internal links: Use relative paths to other documentation
- External links: Include full URLs with descriptive link text
- API documentation: Link to official sources

## Quality Standards
- Ensure all commands are complete and executable
- Verify all URLs and references are accessible
- Maintain consistent terminology throughout documentation
- Include all necessary context for users to complete tasks
