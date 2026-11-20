---
name: delete-comment
description: Remove redundant source comments in this repository when asked to clean them up or advance the comment ratchet. Use review mode for requests to assess comments without edits.
---

# Delete comments

1. Read [AGENTS.md](../../../AGENTS.md) and the [source comment policy](../../../CONTRIBUTING.md#source-comments). Inspect `git status --short`. Use the requested scope. For a repository-wide pass, `uv run --locked python scripts/check_comments.py --inventory` provides the starting count. Preserve legal notices, operative directives, migration history, and comment-shaped literals.
2. Read the relevant code and tests. Record each prose comment by original file and line, then assign the category and corresponding action.

   | Category | Evidence and action |
   | --- | --- |
   | Tautology | The current code already says the same thing. Delete the comment. |
   | Excuse | The comment justifies a workaround that should be fixed. File or link an existing issue for each distinct workaround, state the desired behavior and a verification condition, and delete the comment. |
   | Stale | The named behavior, implementation or context has changed. Verify against the current source and delete the comment. |
   | Decision Record | The comment records an important, non-obvious choice or constraint. Move the rationale into the owning [ADR](../../../docs/adr/) or create one in the established format before deleting the comment. |

3. Use the requested action scope. For a review, report the categories and proposed actions without editing or filing issues. For an applied pass, use the issue tracker within the user's authorization. Follow the repository's sensitive finding route for private details. When an issue cannot be filed or reused, record the blocker and leave the associated comment pending. If you cannot substantiate a decision, retain it and record what evidence is missing.
4. Inspect the diff for changes to behavior, literals, directives, or unrelated work. Verify affected behavior, then run `uv run --locked python scripts/check_comments.py` and `git diff --check`. Report physical source lines removed separately from ADR prose. Include the original file and line, category, and outcome for each distinct finding, with an issue URL for each Excuse and the ADR path or heading for each Decision Record.
