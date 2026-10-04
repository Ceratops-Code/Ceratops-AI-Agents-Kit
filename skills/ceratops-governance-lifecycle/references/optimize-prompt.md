# Optimize Prompt Action

## Goal

Rewrite the user's raw prompt into a concise, structured prompt that is easier
for an AI system to execute while preserving the original intent.

## Context

### Inputs To Capture

- The raw prompt, target audience, expected deliverable, and explicit
  constraints.
- Terminology, placeholders, variables, examples, and strict requirements that
  must be preserved.
- Missing context that can be filled with a safe default without narrowing the
  user's intent.

Ask for the missing raw prompt only when none was provided.

## Constraints

### Skill-Specific Rules

- Do not change the target task, intent, audience, output target, terminology,
  placeholders, variables, examples, or strict constraints.
- Add only assumptions or defaults that reduce execution ambiguity.
- Convert emotional emphasis, repeated wording, and vague pressure into
  observable behavior.
- Prefer concrete actions, evidence, checks, triggers, format requirements, and
  exclusions over adjectives such as "carefully", "thoroughly", or "properly".
- Do not weaken safety, review, testing, security, or `do not` requirements.
- Add a creativity section only when it improves the task; omit it when
  creativity would distract.
- If the raw prompt is already strong, make only a light improvement.

### Boundaries

- Use this action for prompt rewrites, prompt templates, and prompt-clarity
  improvements.
- If the requested change is to a durable rule or instruction file, return to
  the parent skill and select `propose-rules-update`.
- If the requested change is to a skill workflow, return to the parent skill
  and select `optimize-skill`.

### Workflow

1. Identify the real objective, audience, deliverable, constraints, and
   exclusions.
2. Preserve strict wording where changing it would alter intent.
3. Add small missing pieces only when they reduce ambiguity.
4. Structure the prompt with role, goal, constraints, and output format when
   that helps.
5. Remove filler, duplication, motivational wording, and unnecessary verbosity.

## Done When

### Completion Gate

- The optimized prompt preserves the original task and strict constraints.
- The optimized prompt is directly usable as a prompt.
- The response does not include explanation unless the user asked for it.

### Output Contract

Return only the optimized prompt unless the user asks for explanation.
