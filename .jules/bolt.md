## 2024-04-13 - [O(N) Stringification on Multimodal payloads]
**Learning:** Calling `str()` blindly on message parts that contain base64 binary values (like `inline_data`, `file_data`, `image_url`) causes massive O(N) memory allocations, leading to blocking overhead in text extraction paths like `_extract_text` and `_extract_message_content`.
**Action:** Always filter out `bytes`, `bytearray` directly, and explicitly check inner dictionary keys when joining text content from multimodal arrays, bypassing `str(part)` when the dict contains binary placeholders.
