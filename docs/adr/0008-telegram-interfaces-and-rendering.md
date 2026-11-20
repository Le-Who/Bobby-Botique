# ADR 0008. Telegram interfaces and rendering

- **Status** Accepted
- **Date** 2026-11-20, recorded from existing behavior

## Telegram routing

Inline queries bypass the per-user lane for long callbacks because Telegram's
answer IDs expire before queued provider work could finish. Inline images use
uploaded Telegram file IDs for placeholders because a local Bot API server may
not be able to fetch external placeholder URLs. Editing inline media accepts a
file ID or URL, so new bytes are uploaded to acquire a reusable ID. A placeholder
markup button lets chosen results carry `inline_message_id`. The follow-up button
puts a compact marker in the visible query because its Telegram interface has no
hidden payload. Mini App links prefer the registered Telegram short name when
available. Local Bot API health checks target the configured server so they
remain valid after its token has been logged out of the cloud Bot API. See
[inline handlers](../../app/handlers/inline.py), [callback
registration](../../app/handlers/callbacks.py) and [startup](../../bot.py).

Local Bot API method calls and file downloads use distinct endpoints. Deriving
both from the configured host supports HTTP downloads when the worker does not
share the file volume. Administrator alerts can link to a Telegram profile using
the client-defined `tg://user` scheme. See [local file handling](../../app/utils/tg_file.py).

The bot subscribes to handled update types and gives inline queries priority
over long callbacks. It synchronizes Telegram's command menu with the localized
help catalog. A transient menu installation failure leaves startup available.
Text message guards keep edits and channel posts out of the ingress path.
Special handling for slash-prefixed source continuations leaves other slash
commands on their normal registrations. The debounce window combines rapid
authored text and forwarded fragments while preserving attribution. Forwarded
photos can share that window with text so the multimodal handler receives one
request. Telegram message identity distinguishes redelivery from two genuinely
distinct messages with the same text. See [message ingress](../../app/handlers/messages.py),
[media ingress](../../app/handlers/msg_media.py) and [update processing](../../app/update_processor.py).

Command inventory preserves the complete PTB filter for attribution. Negative
filters cannot introduce positive aliases. Custom filters declare static inputs
at class level. Conversation free text stays data entry unless it explicitly
shares a command callback. Concurrent message admission retains an earlier
physical source in its open debounce window even if a short later source was
seen first. Eligible weather and currency queries use a direct lookup before
reserving a chat LLM attempt. See [command inventory](../../app/command_inventory.py).

When a buffered album falls back to a single image, the adapter constructs
the update fields expected by the shared request dispatcher.

Board replies use Telegram's `via_bot` exception and are intercepted before
ordinary message deduplication and authorization. A board binds to its chat
once, and claimed replies do not fall through to standard handling after a
board error. Forwarded-memory callbacks acknowledge immediately because their
callback can be answered once and embedding can outlive its deadline. A bot's
own reactions cannot count as user feedback. The feedback keyboard reveals the
two choices behind one button because Telegram only allows the bot one reaction
on a message and the response keyboard has finite space. Tarot choice sends a
fresh message when the previous message used `ReplyKeyboardRemove`. See
[board handlers](../../app/handlers/board_handler.py),
[board storage](../../app/repos/boards_repo.py), [forwarded memory](../../app/handlers/cb_fwd_save.py)
and [feedback presentation](../../app/utils/ux_improvements.py).

Old Telegram keyboards can still provide full model IDs and stored callback
values. Current buttons use compact identifiers. Confirming a transcript
consumes the pending turn before asynchronous persistence, while re-transcribing
retains the pending record for editing. Reminder intent checks English word
boundaries to keep an action substring in an unrelated word from matching.
Voice intent tolerates leading ASR filler and rejects short false positives.

A Tarot session replacement is published after the welcome message succeeds.
Newer commands can fence that publication, allowing a failed or superseded
welcome to leave the saved session intact. Memory pagination clamps and rereads
after deletion can shrink the final page. See [Tarot session creation](../../app/handlers/cmd_tarot.py)
and [memory commands](../../app/handlers/memory_commands.py).

Classic Tarot keeps its original spread identity and localized callback text
for older keyboards. Explicit research mode and a continuing deep-dive
session each select the agentic handler. Their conditions stay separate so
each can change independently. Draw prompt controls omit clipboard actions
when the installed Telegram library lacks support. Telegram introduced the
copy button in 7.11. See [the Bot API changelog](https://core.telegram.org/bots/api-changelog#october-31-2024).

## Response delivery

Private responses stay in Telegram and skip the Redis Reader and Telegraph
publication paths. Reader markdown expires after 24 hours by default. A separately
cached public Telegraph reference has no automatic expiry. Stream heartbeat
cleanup also handles failure and cancellation before any visible text. A
successful first content write establishes the render receipt even when later
split parts or publication bookkeeping fail. Canonical completed content and
normalized streamed drafts both qualify. Status-only writes do not create
receipts. The delivery owner's final presentation survives handler persistence
failure. See [ADR 0001](0001-single-owner-ai-response-delivery.md),
[the renderer](../../app/response_delivery/renderer.py), [delivery routing](../../app/response_delivery/delivery.py)
and [Reader cache](../../app/cache.py).

Deferred storage omits binary inline payloads because Redis is not the audio
or image transport. Reader fallback can recover content through the separately
stored public reference when the primary key expires and publication is enabled.

## Input and rendering

Fast intent routing accepts affirmative, standalone requests. Quoted, negated
and multi-task input remains available to normal chat. Single-quote delimiters
use word boundaries to preserve Ukrainian apostrophes. Colloquial weather and
currency matches need both a short request and extractable arguments. Currency
conversion direction follows the sentence even when alias recognition scans
by longest match. Future or multi-day forecasts use the normal grounded path.
Image editing requires an image noun to avoid treating unrelated editing
commands as generation. Private compatibility replies bypass the generic denied
message path to keep birth input out of administrator alerts. Routing metadata
continues to claim late date replies. See [intent matching](../../app/intent_router.py),
[inline image intent](../../app/handlers/inline.py) and [compatibility routing](../../app/handlers/compatibility.py).

Fiat aliases normalize to ISO 4217 codes. Natal date-only compatibility
samples the entire range of possible UTC instants and avoids publishing
representative exact positions or aspect orbs for unknown times. Angular
distance can reach extrema between endpoints at multiples of 180 degrees.
Existing matrix interpretation text is Russian. The formatter escapes HTML
before inserting its own markup. It uses Telegram's defined message effects
and expandable blockquote markup. Administrative monitoring tokens and Mini
App initData use their respective authorization paths, with JSON refusal for
API calls. Reader identifiers are validated before storage lookup. See
[date uncertainty](../../app/natal/compatibility.py), [angle bounds](../../app/natal/synastry.py)
and [web guards](../../app/web.py).

Compose the system instruction, including the per-request Moscow timestamp,
before sizing history. Mint 16:9 placeholder JPEGs for Telegram's inline tiles.
Respect the Telegram message limit. Memory pagination slices the canonical
input to preserve whitespace. A 1,400-codepoint slice uses at most 2,800 UTF-16
units. Crocodile's status pill stays in flow so it cannot cover the choice
chips or chat bubbles at narrow layouts.

Reader heading IDs follow the same slug sequence as the table of contents.
Heading discovery excludes fenced code. Transform only eligible text nodes
for bionic reading and keep the server-generated markup ready for a CSS
toggle. The code language annotation supports browser downloads. The plain
text path strips tags before unescaping entities so decoded angle brackets
remain text. Highlighting runs after paint so it cannot block first content.
The Telegram formatter handles legacy underscore emphasis while preserving
Python dunder names. It guards zero-progress splits because carrying the open
tags forward can make the remaining input the same length as the initial input.
The [splitter follow-up](https://github.com/Le-Who/Bobby-Botique/issues/677)
tracks the separate length-accounting problem. See [Reader helpers](../../app/utils/reader_utils.py),
[Reader template](../../app/templates/reader.html) and [Telegram formatting](../../app/utils/text_format.py).

The settings Mini App uses the current hover capability for destructive reset
because hybrid devices can change pointer type mid-session. Delete controls
deduplicate swipe and click actions, and teardown sends an existing owned
delete with best-effort browser keepalive. Layout flexes with the iOS keyboard
and controls overscroll to avoid Telegram's close gesture. Transitions name
the intended properties to avoid unintended resize and to preserve swipe
bounce. Graph zoom uses a CSS transform for CSS animation, and node placement
keeps SVG transform attributes. Natal diagrams preserve theme colors without
JavaScript and use the scroll-padding offset for selecting the active section.
The SVG renderer also specifies text presentation for legacy zodiac symbols.
Stored diagrams pass a drawing-only allowlist before delivery because they can
contain active content. A natal smoke check verifies its existing user before
paid generation, and Pollinations downloads omit generation credentials on
redirect. See [settings Mini App](../../app/templates/miniapp.html), [natal
report](../../app/natal/report_builder.py) and [image download](../../app/providers/pollinations.py).
