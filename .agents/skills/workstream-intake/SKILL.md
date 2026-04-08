---
name: workstream-intake
description: Turn a vague request into a scoped, actionable plan with acceptance criteria, risks, and a recommendation.
---

# Workstream Intake

Turn a vague request into a scoped, actionable plan with clear acceptance criteria.

## When to use
Use this skill when a new workstream is created or when a task description is too vague
to act on directly.

## Process

1. **Analyze the request**: What is being asked? What's the desired outcome?
2. **Identify the owning lane**: Which epic branch should own this work? If the
   request crosses multiple epic lanes, recommend splitting it instead of
   blending concerns.
3. **Identify scope boundaries**: What's in scope? What's explicitly out of scope?
4. **Define acceptance criteria**: How will we know this is done? Be specific and testable.
5. **Identify risks and unknowns**: What could go wrong? What information is missing?
6. **Estimate complexity**: Small (< 1 hour), Medium (1-4 hours), Large (> 4 hours).
7. **Produce the intake document**: Structured summary ready for planning phase.

## Output format

```markdown
## Intake Summary
**Request**: [one-sentence summary]
**Epic lane**: [owning epic / split-needed / foundation-only]
**Scope**: [what's included]
**Out of scope**: [what's excluded]
**Acceptance criteria**:
- [ ] [criterion 1]
- [ ] [criterion 2]
**Risks**: [list of risks]
**Complexity**: [Small/Medium/Large]
**Recommendation**: [proceed / needs-clarification / split-into-multiple]
```

## Escalation
If the request is too ambiguous to scope, escalate with specific questions.
Do not proceed to Planning with an unclear scope.
If the request spans multiple epic lanes, recommend a split plan instead of
forcing multi-lane work into one branch.
