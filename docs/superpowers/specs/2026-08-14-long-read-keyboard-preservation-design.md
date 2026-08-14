# Long-read keyboard preservation design

## Problem

When a streamed AI response exceeds Telegram's message limit, `stream_and_display()` freezes the chat message, stores the full response for the Mini App Reader, and edits the message with a Reader button. It also starts background Telegraph publication as durable fallback storage.

The calling handler then performs another `edit_reply_markup()` with its standard action keyboard. Telegram replaces the entire inline keyboard rather than merging rows, so the Reader button disappears. The final text still says that the continuation is available through the Reader button, while only the standard action buttons remain. A later successful Telegraph log entry is unrelated to the visible keyboard: the Telegraph URL is generated in the background for cold-storage fallback and is not the primary link shown to the user.

The same ownership conflict exists in regular chat, quick search, image analysis, and document analysis.

## Requirements

- Every successfully prepared long read must expose a working Reader entry point in the final Telegram message.
- The Reader button must be the first keyboard row, followed by the feature's normal action rows.
- Short streamed responses must retain their existing action keyboards.
- Dynamic chat buttons derived from response tags, code blocks, memory citations, and forwarding state must keep their current behavior.
- Non-streaming fallbacks must keep their current behavior.
- The fix must cover regular chat, quick search, image analysis, and document analysis.
- Telegraph background publication and Redis fallback behavior must remain unchanged.

## Considered approaches

### 1. Give `stream_and_display()` sole ownership of the final streamed keyboard

Each caller supplies its complete action markup before the stream is finalized. `stream_and_display()` attaches that markup atomically with the final short response or prepends the Reader row when it produces a long read. Callers do not perform a second keyboard replacement after a successful stream.

This is the selected approach because it uses the existing `reply_markup` and `post_processor` extension points, avoids a public return-type change, and removes the conflicting second writer.

### 2. Return long-read metadata to every caller

`stream_and_display()` could return the generated Reader row or URL as a seventh tuple item, and every handler could merge it with its own buttons afterward. This is explicit but expands a widely mocked tuple interface and duplicates merge responsibility across handlers.

### 3. Skip the late edit when a long read is detected

Handlers could detect the long-read marker and avoid replacing the keyboard. This is small but would omit normal action buttons, relies on presentation text as state, and leaves keyboard ownership ambiguous.

## Design

### Keyboard construction

Regular chat will use a focused pure helper to build its standard keyboard from the already available context: language, branch state, parsed intent and suggestions, forward-batch state, response code block, and memory citation count. Its existing stream post-processor will parse and remove response tags, store the parsed metadata, build the keyboard from the cleaned response, and return both cleaned text and markup to `stream_and_display()`.

Quick search, image analysis, and document analysis have static or request-known action rows. They will construct the same markup they use today before calling `stream_and_display()` and pass it through the existing `reply_markup` argument.

### Finalization flow

For a short streamed response:

1. The caller provides the complete feature action markup.
2. `StreamingWriter.finalize()` edits the final text and markup atomically.
3. The caller persists state and performs its other side effects without another keyboard edit.

For a long streamed response:

1. The caller provides the complete feature action markup.
2. `stream_and_display()` stores the full text and creates the Reader URL.
3. It prepends the Reader row to the supplied action rows.
4. It atomically edits the frozen message with the summary and merged keyboard.
5. Background Telegraph publication continues to provide durable fallback storage.
6. The caller does not replace the merged keyboard.

For a non-streaming fallback, the existing `send_long_message()` paths continue to attach their current action markup.

### Error handling

Existing long-read degradation remains intact: a failed Redis write falls back to synchronous Telegraph publication, and failed Telegraph publication falls back to Telegram long-message delivery. Removing the late keyboard replacement also removes a race in which a successful Reader edit was immediately undone.

The change will not infer long-read state from visible text and will not depend on the local `Message.reply_markup` snapshot, which can be stale after an edit.

## Testing

Regression tests will first demonstrate the current failure, then verify:

- regular chat passes its complete dynamic keyboard into streaming and does not replace it afterward;
- quick search, image analysis, and document analysis pass their action keyboards into streaming and do not replace them after successful streaming;
- the long-read path prepends the Reader row while preserving every supplied action row;
- the Telegraph-only path prepends its article link while preserving every supplied action row;
- short streamed responses still receive their action markup;
- non-streaming fallback paths still send their action markup;
- existing response-tag, suggestion, citation, and code-copy behavior remains covered.

Targeted handler and streaming tests will run first, followed by the broader relevant test set, lint checks for changed Python files, and the repository UTF-8 verifier.

## Scope boundaries

This change does not redesign Reader UI, alter Redis TTLs, change Telegraph content generation, or change the visible labels of existing buttons. It only establishes one owner for the final streamed keyboard and preserves all required rows.
