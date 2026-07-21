<!-- MINDVAULT_MEMORY_START -->
### MindVault memory file format

Use this format for every newly created Codex memory file:

```markdown
---
name: <short-kebab-case-slug>
description: <one-line summary — 회수 시 관련성 판단에 사용>
metadata:
  type: user | feedback | project | reference | procedural
---

<본문. 사실마다 [YYYY-MM-DD] 태그. 관련 메모리는 [[name]] 위키링크.>
```

Memory maintenance rules:

1. Search the active `memory/` directory first and update an existing relevant file instead of creating a duplicate.
2. When a new file is necessary, add one index line to `MEMORY.md`; put `procedural` entries in `MEMORY-PROCEDURAL.md` and `feedback` entries in `MEMORY-FEEDBACK.md` instead, preserving the 200-line limit of `MEMORY.md`.
3. Keep the existing secret and personal-information exclusions in force. Never store credentials, authentication material, financial identifiers, detailed addresses, or unnecessary direct contact information.
4. Do not retrofit older Codex memory files that lack this frontmatter. The indexer already accepts them; apply the schema only to newly created files.

Date newly added facts with `[YYYY-MM-DD]`, distinguish facts from inferences, and add `[[name]]` links only when they improve later navigation. Verify every memory and index file after writing.
<!-- MINDVAULT_MEMORY_END -->
