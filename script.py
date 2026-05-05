import sys
content = open('CHANGELOG.md', encoding='utf-8').read()
if 'FreeTheAI Multimodal Integration' not in content:
    lines = content.split('\n')
    idx = 0
    for i, line in enumerate(lines):
        if line.startswith('## [Unreleased]'):
            idx = i
            break
            
    if idx > 0:
        new_block = [
            '## [Unreleased] - 2026-05-05 - FreeTheAI Multimodal Integration',
            '',
            '### 🎨 FreeTheAI Multimodal Integration',
            '',
            '- **Added `FreeTheAIProvider` (`app/providers/freetheai.py`):** First-class integration for FreeTheAI acting as a router to diverse models including Claude, Gemini, GPT, and custom variations. Implemented strict prefix collision guards (`is_freetheai_model`) to ensure `vhr/`, `cat/`, `yng/` prefixes do not leak into OpenRouter.',
            '- **Image Generation (`app/providers/freetheai_image.py`, `app/handlers/cmd_image.py`):** Added support for models like `vhr/gpt_image_2` and `vhr/nano_banana_2`. Generates custom prompts and securely proxies requests through the FreeTheAI API, complete with specific UI messages for quota errors and provider exhaustion.',
            '- **Lyria Audio Generation (`app/providers/freetheai_audio.py`, `app/handlers/ai_chat.py`):** Chat texts targeting Lyria audio models (`or/google/lyria-3-pro-preview`) are now intercepted mid-flight. Standard text generation is bypassed in favor of a specialized audio generation pipeline, returning direct Telegram `reply_audio` messages (Base64 MP3 or fallback URLs).',
            ''
        ]
        out_lines = lines[:idx] + new_block + lines[idx:]
        open('CHANGELOG.md', 'w', encoding='utf-8').write('\n'.join(out_lines))
        print('CHANGELOG updated')
else:
    print('Already updated')
