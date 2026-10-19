# Repository Working Rules

Before implementation, read [the current design spec](docs/specs/2026-10-18-support-extraction-harness-design.md) and [the context contract](docs/PROMPT_CONTEXT.md). They define the approved scope, single extraction Agent, recovery budget, and model messages.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them instead of picking silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it instead of deleting it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:

- "Add validation" -> "Write tests for invalid inputs, then make them pass"
- "Fix the bug" -> "Write a test that reproduces it, then make it pass"
- "Refactor X" -> "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

```less
1. [Step] -> verify: [check]
2. [Step] -> verify: [check]
3. [Step] -> verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## 5. Commit Verified Milestones

**Complete a coherent feature, verify it, then commit it.**

- After each functional milestone is complete and its relevant tests pass, create a local Git commit for that milestone before starting the next one.
- Run the relevant tests against the final changes before committing. Fix failures first; do not claim an unverified milestone is complete.
- Review the staged diff and include only the milestone's code, tests, and directly related documentation. Preserve unrelated pre-existing changes.
- Use a clear commit message describing the implemented behavior. Report the verification result and commit ID in the progress update.
- For documentation-only milestones, verify the content, references, and `git diff --check` instead of adding artificial runtime tests.
- Keep credentials, runtime databases, logs, and raw customer/model data out of commits.
- A local commit does not authorize pushing, publishing, merging, or rewriting existing history; those require applicable user authorization.
