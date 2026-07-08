# /draw Pre-Canvas UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Modify the `/draw` command to display the interactive Canvas keyboard (Pre-Canvas UI) instead of immediately starting image generation, allowing users to modify generation settings prior to execution.

**Architecture:** We will intercept the `/draw` handler (`draw_command`) in `app/handlers/cmd_image.py`. Instead of directly calling `_run_generation()`, we will parse the prompt, update the session state via `_set_draw_state()`, construct the inline keyboard via `_build_main_menu()`, and respond with the formatted confirmation text and keyboard.

**Tech Stack:** Python 3.12+, python-telegram-bot

---

### Task 1: Update `draw_command` to use Pre-Canvas UI

**Files:**
- Modify: `app/handlers/cmd_image.py:734-750` (approximate location of `draw_command`)

- [ ] **Step 1: Write the minimal implementation**

Locate `draw_command` in `app/handlers/cmd_image.py`.
Replace the code that calls `_run_generation(...)` with code that sets the state and builds the keyboard.

```python
    prompt = " ".join(context.args).strip()
    if len(prompt) < 3:
        await update.message.reply_text(
            "⚠️ Слишком короткий запрос. Пожалуйста, введите хотя бы одно слово.",
        )
        return

    # NEW FLOW: Save state and render Pre-Canvas UI
    state = _set_draw_state(
        context,
        prompt=prompt,
        awaiting_prompt=False,
    )
    
    auto_text = f"🎨 **Запрос на генерацию:**\n`{_escape_md(prompt)}`"
    from app.utils.formatting import TelegramFormatter
    formatted, parse_mode = TelegramFormatter.format_text(auto_text)
    
    keyboard = _build_main_menu(state)
    await update.message.reply_text(formatted, parse_mode=parse_mode, reply_markup=keyboard)
```

- [ ] **Step 2: Run linter to verify syntax**

Run: `ruff check app/handlers/cmd_image.py`
Expected: Clean output.

- [ ] **Step 3: Run type checker**

Run: `python -m mypy app/handlers/cmd_image.py --ignore-missing-imports`
Expected: Clean output.

- [ ] **Step 4: Commit**

```bash
git add app/handlers/cmd_image.py
git commit -m "feat: use Pre-Canvas interactive UI for /draw command"
```
