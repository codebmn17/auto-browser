# LLM Adapter Pattern

The browser harness should be **model-agnostic**.

## Shared tool contract

Every model should call the same small set of tools:
- `create_session`
- `observe`
- `click`
- `type`
- `press`
- `scroll`
- `upload`
- `save_storage_state`
- `request_human_takeover`
- `close_session`

The model does not need shell access or direct browser access.

## Recommended loop

1. `observe`
2. inspect screenshot + interactables
3. choose one action
4. execute one action only
5. `observe` again
6. repeat until the goal or a takeover gate

## Why this loop works

It gives the model:
- a current screenshot for visual grounding
- stable `element_id` handles for precise actions
- console/page errors when the UI breaks
- a recovery path through human takeover

Treat `element_id` values as **observation-scoped** in the POC. If the page rerenders or navigates, refresh with `observe` before reusing a target.

## Example orchestration pseudocode

```python
session = create_session(name="signup-run", start_url="https://example.com")

while True:
    observation = observe(session["id"])
    decision = model.plan(
        goal="fill the allowed form and stop before submission",
        screenshot=observation["screenshot_url"],
        interactables=observation["interactables"],
        console=observation["console_messages"],
        errors=observation["page_errors"],
    )

    if decision["type"] == "takeover":
        request_human_takeover(session["id"], reason=decision["reason"])
        break

    execute(decision)
```

## Model-specific notes

### OpenAI
Use the same loop whether you wrap it with a custom browser tool or use the Responses API computer-use style pattern. The key shape is still screenshot → action → screenshot.

### Claude
Claude works well when you keep the action space small and deterministic. It benefits from explicit “one action at a time” discipline.

### Gemini
Gemini can use the same controller API. Treat it as another planner over the same observation payload.

## Strong opinion

Do not build a separate browser stack for each LLM.

Build one controller. Give every model the same tool contract. That is what keeps the system maintainable.
