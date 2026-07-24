from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.config import GEMINI_ECONOMY_MODEL
from app.providers.router import get_provider_router
from app.repos import daily_trivia as repo
from app.utils.json_compat import json

logger = logging.getLogger(__name__)

TRIVIA_MODEL = GEMINI_ECONOMY_MODEL
MAX_TIME_PER_QUESTION_MS = 15000
BASE_CORRECT_POINTS = 200
MAX_SPEED_BONUS = 100


def calculate_question_score(is_correct: bool, elapsed_ms: int) -> int:
    if not is_correct:
        return 0
    score = BASE_CORRECT_POINTS
    if elapsed_ms < MAX_TIME_PER_QUESTION_MS and elapsed_ms >= 0:
        ratio = (MAX_TIME_PER_QUESTION_MS - elapsed_ms) / MAX_TIME_PER_QUESTION_MS
        score += int(MAX_SPEED_BONUS * ratio)
    return score


def shuffle_options_and_update_correct_index(questions_raw: list[dict[str, Any]]) -> list[repo.TriviaQuestion]:
    """Randomly shuffle option choices so the correct answer is never in a fixed position.

    Ensures plausible distractors and unpredictable placement across indices 0..3.
    """
    clean_questions: list[repo.TriviaQuestion] = []

    for idx, raw in enumerate(questions_raw):
        topic = str(raw.get("topic", "Общие знания")).strip()
        question_text = str(raw.get("question", "")).strip()
        options = [str(opt).strip() for opt in raw.get("options", []) if str(opt).strip()]
        explanation = str(raw.get("explanation", "")).strip()

        if len(options) < 2 or not question_text:
            continue

        raw_correct_idx = int(raw.get("correct_index", 0))
        if raw_correct_idx < 0 or raw_correct_idx >= len(options):
            raw_correct_idx = 0

        correct_option_text = options[raw_correct_idx]

        # Shuffle options randomly
        shuffled_options = list(options)
        random.shuffle(shuffled_options)

        new_correct_idx = shuffled_options.index(correct_option_text)

        clean_questions.append(
            repo.TriviaQuestion(
                id=idx + 1,
                topic=topic,
                question=question_text,
                options=shuffled_options,
                correct_index=new_correct_idx,
                explanation=explanation,
            )
        )

    return clean_questions


SYSTEM_PROMPT_TRIVIA = """Ты — эксперт по составлению интеллектуальных викторин и увлекательных тривиа-игр.
Сгенерируй ровно 5 уникальных, высококачественных вопросов для ежедневной викторины.

ТРЕБОВАНИЯ К ВОПРОСАМ:
1. Качество и темы: 5 разных сфер знаний (например: наука/космос, история мира, искусство/культура, география/природа, удивительные факты/технологии).
2. Варианты ответа (options): РОВНО 4 варианта ответа на каждый вопрос.
3. Сложность и реалистичность: Неправильные варианты ответа (дистракторы) ДОЛЖНЫ быть правдоподобными, иметь схожую длину и категорию с правильным ответом. Избегай очевидных, шуточных или банальных вариантов!
4. Объяснение (explanation): К каждому вопросу напиши интересное, познавательное объяснение на 2-3 предложения, раскрывающее суть ответа и содержащее 1-2 дополнительных любопытных факта.
5. Ключевая пара (key): Для каждого вопроса ОБЯЗАТЕЛЬНО укажи объект и конкретный факт/аспект (субобъект), о котором задан вопрос. Избегай широких категорий!
6. Язык: Русский.

ОТВЕТ ДОЛЖЕН БЫТЬ СТРОГО В ФОРМАТЕ JSON (БЕЗ ЛИШНЕГО ТЕКСТА) СО СЛЕДУЮЩЕЙ СТРУКТУРОЙ:
[
  {
    "id": 1,
    "topic": "Космос и Наука",
    "question": "Текст вопроса...",
    "options": ["Вариант A", "Вариант B", "Вариант C", "Вариант D"],
    "correct_index": 0,
    "explanation": "Подробное познавательное объяснение...",
    "key": { "object": "Bluetooth", "subobject": "происхождение названия" }
  },
  ...
]
"""

SYSTEM_PROMPT_SUPER_TRIVIA = """Ты — эксперт по составлению интеллектуальных викторин высшего уровня сложности (СУПЕРИГРА).
Сгенерируй ровно 3 УНИКАЛЬНЫХ, ПОВЫШЕННОЙ СЛОЖНОСТИ (expert-level) вопроса для Суперигры.

ТРЕБОВАНИЯ К ВОПРОСАМ:
1. Качество и уровень: Вопросы должны быть сложными, глубокими и нетривиальными. Избегай общеизвестных фактов.
2. Варианты ответа (options): РОВНО 4 варианта ответа на каждый вопрос.
3. Правдоподобные дистракторы: Все варианты должны звучать максимально убедительно.
4. Объяснение (explanation): Познавательное объяснение на 2-3 предложения с интересными деталями.
5. Ключевая пара (key): Для каждого вопроса ОБЯЗАТЕЛЬНО укажи объект и конкретный факт/аспект (субобъект).
6. Язык: Русский.

ОТВЕТ ДОЛЖЕН БЫТЬ СТРОГО В ФОРМАТЕ JSON:
[
  {
    "id": 1,
    "topic": "История науки",
    "question": "Текст сложного вопроса...",
    "options": ["Вариант A", "Вариант B", "Вариант C", "Вариант D"],
    "correct_index": 0,
    "explanation": "Подробное познавательное объяснение...",
    "key": { "object": "Объект", "subobject": "Аспект" }
  },
  ...
]
"""


async def prepare_daily_puzzle(
    puzzle_date: date, *, force: bool = False, mode: str = "all"
) -> repo.DailyTriviaPuzzle:
    """Generate/return the daily trivia puzzle.

    mode: "all"   – regenerate both main (5) and super (3) questions (default)
          "main"  – regenerate only the 5 regular questions; keep existing super
          "super" – regenerate only the 3 expert questions; keep existing main
    """
    existing = await repo.get_puzzle(puzzle_date)

    # Quick-return if everything already exists and we're not forcing
    if (
        existing
        and existing.questions
        and len(existing.questions) >= 5
        and existing.super_questions
        and len(existing.super_questions) >= 3
        and not force
        and existing.status == "ready"
    ):
        return existing

    if force:
        # Remove only the used_keys that correspond to the parts being regenerated.
        # "all" or "main": delete all for this date (main keys will be re-saved).
        # "super": we also delete to allow fresh super-topic selection.
        try:
            deleted = await repo.delete_used_keys_for_date(puzzle_date)
            if deleted:
                logger.info("trivia: cleared %d stale used_keys for %s (mode=%s)", deleted, puzzle_date, mode)
        except Exception as exc:
            logger.warning("trivia: failed to clear used_keys for %s: %s", puzzle_date, exc)

    router = get_provider_router()

    from app.repos.settings_repo import get_global_setting
    model_name = await get_global_setting("daily_trivia_llm_model", TRIVIA_MODEL)

    used_keys = await repo.get_used_keys(days=90)
    all_used_keys = list(used_keys)

    def _build_used_context(keys_list: list[dict[str, str]]) -> str:
        if not keys_list:
            return ""
        formatted = "\n".join(f"- {k['object']} → {k['subobject']}" for k in keys_list[:40])
        return f"\n\nУЖЕ ИССЛЕДОВАННЫЕ ТЕМЫ И ФАКТЫ (НЕ ПОВТОРЯЙ ИХ):\n{formatted}"

    keys_to_save: list[dict[str, str]] = []

    # ── Regular questions (5) ────────────────────────────────────────────────
    if mode in ("all", "main"):
        prompt = f"Сгенерируй 5 вопросов для тривиа-викторины на дату {puzzle_date.isoformat()}.{_build_used_context(all_used_keys)}"
        try:
            response_text, _ = await router.get_response(
                preferred_model=model_name,
                history=[{"role": "user", "parts": [{"text": prompt}]}],
                system_instruction=SYSTEM_PROMPT_TRIVIA,
                timeout=45.0,
            )
            start_idx = response_text.find("[")
            end_idx = response_text.rfind("]")
            clean_json = response_text[start_idx: end_idx + 1] if start_idx != -1 and end_idx != -1 and start_idx < end_idx else response_text.strip()
            parsed = json.loads(clean_json)
            if not isinstance(parsed, list):
                raise ValueError("Parsed LLM output is not a JSON list")
            for item in parsed:
                if isinstance(item, dict):
                    k = item.get("key")
                    if isinstance(k, dict) and k.get("object") and k.get("subobject"):
                        k_obj = {"object": str(k["object"]), "subobject": str(k["subobject"])}
                        keys_to_save.append(k_obj)
                        all_used_keys.append(k_obj)
            questions = shuffle_options_and_update_correct_index(parsed)
            if len(questions) < 5:
                logger.warning("LLM generated fewer than 5 valid trivia questions (%d), using fallback", len(questions))
                questions = _get_fallback_questions()
        except Exception as e:
            logger.error("Failed to generate Daily Trivia via LLM for date %s: %s", puzzle_date, e, exc_info=True)
            questions = _get_fallback_questions()
    else:
        # mode == "super": keep existing main questions untouched
        questions = existing.questions if (existing and existing.questions) else _get_fallback_questions()

    # ── Super questions (3 expert level) ────────────────────────────────────
    if mode in ("all", "super"):
        super_prompt = f"Сгенерируй 3 СУПЕР-вопроса повышенной сложности на дату {puzzle_date.isoformat()}.{_build_used_context(all_used_keys)}"
        try:
            response_text_super, _ = await router.get_response(
                preferred_model=model_name,
                history=[{"role": "user", "parts": [{"text": super_prompt}]}],
                system_instruction=SYSTEM_PROMPT_SUPER_TRIVIA,
                timeout=45.0,
            )
            start_idx = response_text_super.find("[")
            end_idx = response_text_super.rfind("]")
            clean_json_super = response_text_super[start_idx: end_idx + 1] if start_idx != -1 and end_idx != -1 and start_idx < end_idx else response_text_super.strip()
            parsed_super = json.loads(clean_json_super)
            if not isinstance(parsed_super, list):
                raise ValueError("Parsed super LLM output is not a JSON list")
            for item in parsed_super:
                if isinstance(item, dict):
                    k = item.get("key")
                    if isinstance(k, dict) and k.get("object") and k.get("subobject"):
                        keys_to_save.append({"object": str(k["object"]), "subobject": str(k["subobject"])})
            super_questions = shuffle_options_and_update_correct_index(parsed_super)
            if len(super_questions) < 3:
                logger.warning("LLM generated fewer than 3 valid super questions (%d), using fallback", len(super_questions))
                super_questions = _get_fallback_super_questions()
        except Exception as e:
            logger.error("Failed to generate Daily Super Trivia via LLM for date %s: %s", puzzle_date, e, exc_info=True)
            super_questions = _get_fallback_super_questions()
    else:
        # mode == "main": keep existing super questions untouched
        super_questions = existing.super_questions if (existing and existing.super_questions) else _get_fallback_super_questions()

    # ── Persist ──────────────────────────────────────────────────────────────
    puzzle = await repo.save_puzzle(puzzle_date, questions, super_questions=super_questions, status="ready")

    if keys_to_save:
        try:
            await repo.save_used_keys(keys_to_save, puzzle_date)
        except Exception as ex:
            logger.warning("Failed to save trivia used keys for date %s: %s", puzzle_date, ex)

    return puzzle



def _get_fallback_questions() -> list[repo.TriviaQuestion]:
    """Fallback static trivia set in case LLM is completely unreachable."""
    fallbacks = [
        {
            "id": 1,
            "topic": "Астрономия",
            "question": "Какая планета Солнечной системы имеет самый короткий день (оборот вокруг своей оси)?",
            "options": ["Юпитер", "Венера", "Марс", "Меркурий"],
            "correct_index": 0,
            "explanation": "Юпитер совершает полный оборот вокруг своей оси всего за 9 часов и 55 минут. Из-за столь быстрого вращения планета заметно сплюснута у полюсов.",
        },
        {
            "id": 2,
            "topic": "История",
            "question": "Какое из этих древних сооружений было построено раньше остальных?",
            "options": ["Пирамида Хеопса", "Великая Китайская стена", "Колизей в Риме", "Мачу-Пикчу"],
            "correct_index": 0,
            "explanation": "Пирамида Хеопса была построена около 2560 года до н.э., то есть более чем на 2000 лет раньше Колизея и Великой Китайской стены.",
        },
        {
            "id": 3,
            "topic": "Биология",
            "question": "Какое млекопитающее имеет самое высокое кровяное давление?",
            "options": ["Жираф", "Синий кит", "Африканский слон", "Гепард"],
            "correct_index": 0,
            "explanation": "Из-за длинной шеи сердцу жирафа требуется качать кровь на высоту до 2 метров до мозга. Его кровяное давление достигает 280/180 мм рт. ст., что вдвое выше человеческой нормы.",
        },
        {
            "id": 4,
            "topic": "География",
            "question": "Какое государство не имеет официальной столицы?",
            "options": ["Науру", "Монако", "Лихтенштейн", "Исландия"],
            "correct_index": 0,
            "explanation": "У тихоокеанского государства Науру нет официально закрепленной столицы. Органы власти расположены в округе Ярен.",
        },
        {
            "id": 5,
            "topic": "Искусство",
            "question": "Кто из знаменитых художников написал картину «Девочка с персиками»?",
            "options": ["Валентин Серов", "Илья Репин", "Иван Шишкин", "Карл Брюллов"],
            "correct_index": 0,
            "explanation": "Валентин Серов написал «Девочку с персиками» в 1887 году, когда ему было всего 22 года. На картине изображена 12-летняя Вера Мамонтова.",
        },
    ]
    return shuffle_options_and_update_correct_index(fallbacks)


def _get_fallback_super_questions() -> list[repo.TriviaQuestion]:
    """Fallback static super trivia set in case LLM is completely unreachable."""
    fallbacks = [
        {
            "id": 1,
            "topic": "Квантовая физика",
            "question": "Какой ученый впервые предсказал существование антиматерии на основе математического уравнения?",
            "options": ["Поль Дирак", "Вернер Гейзенберг", "Эрвин Шрёдингер", "Ричард Фейнман"],
            "correct_index": 0,
            "explanation": "В 1928 году Поль Дирак вывел уравнение, объединившее квантовую механику и специальную теорию относительности, которое предсказывало существование позитрона.",
        },
        {
            "id": 2,
            "topic": "Древняя история",
            "question": "Какая из этих древних цивилизаций использовала систему счисления с основанием 60?",
            "options": ["Шумеры", "Древние египтяне", "Майя", "Хетты"],
            "correct_index": 0,
            "explanation": "Шумеры и вавилоняне использовали шестидесятеричную систему счисления, благодаря которой мы до сих пор делим час на 60 минут, а круг — на 360 градусов.",
        },
        {
            "id": 3,
            "topic": "Лингвистика",
            "question": "Какой язык считается единственным сохранившимся доиндоевропейским языком Западной Европы?",
            "options": ["Баскский", "Ирландский", "Албанский", "Мальтийский"],
            "correct_index": 0,
            "explanation": "Баскский язык (эускара) является изолированным языком и существовал в Европе еще до прихода индоевропейских племен.",
        },
    ]
    return shuffle_options_and_update_correct_index(fallbacks)


async def ensure_prepared_puzzles(*, now: datetime | None = None) -> list[repo.DailyTriviaPuzzle]:
    from app.repos.crocodile_daily import today_puzzle_date

    start_date = today_puzzle_date(now)
    dates = [start_date + timedelta(days=offset) for offset in range(repo.DAILY_TRIVIA_PREP_DAYS_AHEAD + 1)]
    return list(await asyncio.gather(*[prepare_daily_puzzle(d) for d in dates]))

