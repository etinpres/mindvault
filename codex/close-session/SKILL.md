---
name: close-session
description: Close the current Codex work session by reviewing newly confirmed durable facts, decisions, outcomes, preferences, and reusable procedures, then safely updating the active MindVault memory directory. Use when the user invokes $close-session or explicitly asks to close, wrap up, or persist the current session into MindVault; do not trigger for a casual goodbye without a memory request.
---

# Close Session

Review the current session while its context is still available, then persist only information that will matter in a later session.

## Resolve the memory directory

Use the writable MindVault `memory/` directory declared by the active `AGENTS.md`. If no instruction declares one, use the default MindVault slot: the directory under `~/.claude/projects/*/memory/` whose `MEMORY.md` was most recently updated. If neither resolves, stop and ask the user instead of guessing a path.

Treat direct Markdown search in that directory as the freshness source of truth. Do not rely on a delayed semantic index when deciding whether a relevant file already exists.

## Select durable items

Collect only newly confirmed items from this session:

- `project`: decisions, implementation state, completed verification, and next actions
- `user`: stable user background or working preferences stated directly by the user
- `feedback`: reusable corrections or rules the user wants followed in later work
- `reference`: durable external-system facts or research worth retrieving later
- `procedural`: reusable commands, diagnostics, or implementation patterns

Write facts, not a transcript. Distinguish direct facts from inferences, preserve uncertainty, and skip casual conversation, short-lived status, duplicate information, and unverified guesses. If there is nothing durable and new, report that no memory change was needed and stop.

Never store passwords, API keys, tokens, private keys, exact birth dates, detailed addresses, legal or business registration numbers, transaction or account identifiers, public IP addresses, banking or card data, or unnecessary direct contact information. Generalize sensitive paths and provider details when the location itself could enable credential discovery.

## Prefer an existing topic file

Search filenames, frontmatter, and body text for each candidate before writing. Update the best existing topic file whenever one already covers the subject. Do not create a second file merely because the wording differs.

When updating an existing file:

- Preserve its existing frontmatter and filename.
- Append or revise the smallest relevant section.
- Add `[YYYY-MM-DD]` to each newly added fact.
- Add `[[related-memory-name]]` links when they materially help later navigation.
- Do not retrofit older files that lack the new frontmatter format.

## Create a new topic file only when necessary

Use a short kebab-case `name` and the repository's filename convention. New files must begin with exactly this schema:

```markdown
---
name: short-kebab-case-slug
description: one-line summary used to judge recall relevance
metadata:
  type: user | feedback | project | reference | procedural
---

[YYYY-MM-DD] Durable fact or decision.

Related: [[another-memory]]
```

Keep `description` on one line and make it useful for future relevance decisions. YAML-quote it when punctuation could change YAML meaning.

## Update the correct index

Add an index line only when creating a new topic file:

- `procedural` goes to `MEMORY-PROCEDURAL.md`.
- `feedback` goes to `MEMORY-FEEDBACK.md`.
- `user`, `project`, and `reference` go to `MEMORY.md`.

Use the existing index style and link the new file once. Do not add another index line when only updating an existing topic. Keep procedural and feedback entries out of `MEMORY.md` so its 200-line loading limit remains protected.

## Verify and report

After writing:

1. Re-read every changed memory and index file.
2. Confirm new frontmatter parses as YAML and `metadata.type` is one of the five allowed values.
3. Confirm every new fact has a date tag and every newly created file has exactly one index entry in the correct index.
4. Scan the changed text for secrets and unnecessary personal identifiers.
5. If a background indexer watches the memory directory, let it propagate the change on its own schedule; do not force a manual refresh.

Report the files updated or created, their memory types, and any candidate deliberately skipped. Keep the report concise and never repeat sensitive values.
