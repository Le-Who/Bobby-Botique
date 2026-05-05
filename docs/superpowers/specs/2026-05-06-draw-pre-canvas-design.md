# /draw Pre-Canvas UI Design

## 1. Goal
Modify the `/draw <prompt>` command to pause execution and render an interactive settings menu (Pre-Canvas UI) instead of immediately starting image generation. This gives the user the opportunity to change the generation model, aspect ratio, or prompt enhancement settings prior to consuming API limits.

## 2. Approach Selected
**Strict Pre-Canvas**: When a user inputs `/draw <prompt>`, the bot saves the prompt in the conversational state and immediately renders the Interactive Canvas keyboard inline. The user must manually tap "🔄 Сгенерировать" to trigger the generation.

## 3. Architecture Changes

### Component: `app/handlers/cmd_image.py`
- **`draw_command` function**:
  - **Current Flow**: Receives `prompt` from args, saves it, and immediately calls `_run_generation(..., prompt=prompt)`.
  - **New Flow**:
    1. Parse and extract the prompt as usual.
    2. Save the prompt to `context.user_data` via `_set_draw_state(..., prompt=prompt, awaiting_prompt=False)`.
    3. Generate the UI by calling `_build_main_menu(state)`.
    4. Send a message containing the parsed prompt and the generated inline keyboard, exactly like the voice flow's confirmation text (`"🎨 **Запрос на генерацию:**\n`prompt`"`).

### Component: Voice Handoff Consistency
- Ensure that the text and structure shown by `/draw` align with `_auto_route_to_image` (the voice draw interceptor) to maintain a cohesive user experience across text and voice multimodal commands.

## 4. Error Handling & Edge Cases
- **Empty Prompts**: Handled as usual (`/draw` without args prompts the user to enter text).
- **Callback Routing**: The "🔄 Сгенерировать" button (callback `draw:execute`) already exists in the `_build_main_menu` UI and points to the `draw_execute_callback` which initiates `_run_generation`. No new callback logic needs to be written.

## 5. Verification Plan
- Send `/draw cute cat` to the bot.
- Verify that generation *does not* start.
- Verify that the message shows the prompt and the setting buttons (Models, Formats, Enhance).
- Click "🔄 Сгенерировать" and ensure the image generates correctly with the selected settings.
