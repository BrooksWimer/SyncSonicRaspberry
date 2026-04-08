---
name: prepare-review
description: Compile a comprehensive review package for human evaluation when a workstream is moving from verification to review.
---

# Prepare Review

Compile a comprehensive review package for human evaluation.

## When to use
Use this skill when a workstream is transitioning from Verification to Review state.

## Process

1. **Gather all changes**: Collect diffs, new files, modified files, deleted files.
2. **Summarize changes**: Group changes by concern (feature, fix, refactor, config, test).
3. **Include verification evidence**: Reference the verification report.
4. **Highlight decisions made**: List any decisions made during implementation with rationale.
5. **Flag items needing attention**: Anything unusual, risky, or that deviates from the plan.
6. **Produce the review package**: Structured document ready for human review.

## Output format

```markdown
## Review Package: [workstream name]

### Summary
[2-3 sentence overview of what was done and why]

### Changes
**Files modified**: [count]
**Files added**: [count]
**Files deleted**: [count]

#### By concern:
- **[concern]**: [brief description of changes]

### Verification
[Reference verification report - all checks passing / details of any exceptions]

### Decisions made
- [decision]: [rationale]

### Items needing attention
- [item]: [why it needs attention]

### Recommendation
[Ship as-is / Ship with noted caveats / Needs changes before shipping]
```
