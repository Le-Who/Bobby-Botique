## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-18 - Replacing dict stringification with text extraction
**Learning:** Calling `str()` on massive dictionaries containing base64 images or large binary blobs inside an LLM parts array just to check message length causes massive memory allocation and blocks the main thread.
**Action:** Use an explicit helper function to safely check lengths, prioritizing extracting the "text" key for dictionaries and explicitly skipping large binary objects like bytes and bytearrays without stringifying them.
