from __future__ import annotations

import logging
import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.config import GEMINI_ECONOMY_MODEL
from app.errors import is_error_message, strip_error_tag
from app.games.trivia_similarity import FactIdentity
from app.providers.router import get_provider_router
from app.repos import daily_trivia as repo
from app.utils.json_compat import json

logger = logging.getLogger(__name__)

TRIVIA_MODEL = GEMINI_ECONOMY_MODEL
MAX_TIME_PER_QUESTION_MS = 15000
BASE_CORRECT_POINTS = 200
MAX_SPEED_BONUS = 100
GENERATION_ATTEMPTS = 3


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

        if len(options) != 4 or not question_text:
            continue

        raw_correct_idx = int(raw.get("correct_index", 0))
        if raw_correct_idx < 0 or raw_correct_idx >= len(options):
            raw_correct_idx = 0

        correct_option_text = options[raw_correct_idx]

        identity = None
        key = raw.get("key") or raw.get("identity")
        if isinstance(key, dict):
            subject = str(key.get("subject") or key.get("object") or "").strip()
            relation = str(key.get("relation") or key.get("subobject") or "").strip()
            answer = str(key.get("answer") or correct_option_text).strip()
            if subject and relation and answer:
                identity = FactIdentity.create(subject=subject, relation=relation, answer=answer)

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
                identity=identity,
            )
        )

    return clean_questions


SYSTEM_PROMPT_TRIVIA = """Ты — эксперт по составлению интеллектуальных викторин и увлекательных тривиа-игр.
Сгенерируй ровно 5 уникальных, высококачественных вопросов для ежедневной викторины.

ТРЕБОВАНИЯ К ВОПРОСАМ:
1. Качество и темы: 5 разных сфер знаний (например: наука/космос, история мира, искусство/культура, география/природа, удивительные факты/технологии).
2. Варианты ответа (options): РОВНО 4 варианта ответа на каждый вопрос.
3. Сложность: Вопросы должны быть интересными и нетривиальными — не слишком очевидными (вроде «Какой цвет у снега?» или «Столица Франции»), но и не экспертными. Целься в уровень «любопытный, начитанный человек». Дистракторы ДОЛЖНЫ быть правдоподобными — схожими по длине, категории и стилю с правильным ответом.
4. Язык и стиль (ВАЖНО): Пиши простым, живым языком — так, чтобы вопрос и объяснение понял человек любого возраста и без специальных знаний. Одно короткое предложение вопроса. Никакого академического или витиеватого слога, никаких профессиональных терминов без объяснения прямо в тексте. Если идея сложная — упрости формулировку, но сохрани суть.
5. Объяснение (explanation): 2-3 простых предложения. Раскрывает суть ответа + 1-2 любопытных факта. Тон — дружелюбный и увлекательный, как у умного друга.
6. Идентичность факта (key): Для каждого вопроса ОБЯЗАТЕЛЬНО укажи subject (конкретная сущность), relation (что именно о ней спрашивается) и answer (канонический правильный ответ). Это идентификатор факта, а не тема вопроса.
7. Язык: Русский.

ОТВЕТ ДОЛЖЕН БЫТЬ СТРОГО В ФОРМАТЕ JSON (БЕЗ ЛИШНЕГО ТЕКСТА) СО СЛЕДУЮЩЕЙ СТРУКТУРОЙ:
[
  {
    "id": 1,
    "topic": "Космос и Наука",
    "question": "Текст вопроса...",
    "options": ["Вариант A", "Вариант B", "Вариант C", "Вариант D"],
    "correct_index": 0,
    "explanation": "Познавательное объяснение простым языком...",
    "key": { "subject": "Bluetooth", "relation": "происхождение названия", "answer": "король Харальд Синезубый" }
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
5. Идентичность факта (key): Для каждого вопроса ОБЯЗАТЕЛЬНО укажи subject, relation и канонический answer. Не используй широкую тему вместо конкретного факта.
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
    "key": { "subject": "Объект", "relation": "Конкретное отношение", "answer": "Канонический ответ" }
  },
  ...
]
"""


def _bank_context(facts: list[repo.StoredTriviaFact]) -> str:
    if not facts:
        return ""
    claims = "\n".join(f"- {fact.identity.canonical_claim}" for fact in facts[:400])
    return f"\n\nФАКТЫ ИЗ БАНКА, КОТОРЫЕ НЕЛЬЗЯ ПОВТОРЯТЬ ИЛИ ПЕРЕФРАЗИРОВАТЬ:\n{claims}"


async def generate_question_lane(
    puzzle_date: date,
    *,
    lane: str,
    model_name: str,
    router=None,
) -> list[repo.TriviaQuestion]:
    """Generate one lane; publication and cross-lane checks happen separately."""
    if lane not in {"main", "super"}:
        raise ValueError("lane must be 'main' or 'super'")
    count = 5 if lane == "main" else 3
    system_prompt = SYSTEM_PROMPT_TRIVIA if lane == "main" else SYSTEM_PROMPT_SUPER_TRIVIA
    label = "обычных вопросов" if lane == "main" else "СУПЕР-вопросов"
    bank = await repo.get_recent_bank_facts(reference_date=puzzle_date, days=90)
    prompt = f"Сгенерируй ровно {count} {label} на дату {puzzle_date.isoformat()}.{_bank_context(bank)}"
    provider_router = router or get_provider_router()
    last_error: Exception | None = None
    for attempt in range(1, GENERATION_ATTEMPTS + 1):
        try:
            response_text, _ = await provider_router.get_response(
                preferred_model=model_name,
                history=[{"role": "user", "parts": [{"text": prompt}]}],
                system_instruction=system_prompt,
                timeout=45.0,
            )
            if is_error_message(response_text):
                raise RuntimeError(f"LLM provider error: {strip_error_tag(response_text)}")
            start_idx = response_text.find("[")
            end_idx = response_text.rfind("]")
            clean_json = (
                response_text[start_idx : end_idx + 1]
                if start_idx >= 0 and end_idx > start_idx
                else response_text.strip()
            )
            parsed = json.loads(clean_json)
            if not isinstance(parsed, list):
                raise ValueError("LLM output is not a JSON list")
            questions = shuffle_options_and_update_correct_index(parsed)
            if len(questions) != count or any(question.identity is None for question in questions):
                raise ValueError(f"LLM returned {len(questions)}/{count} valid identified questions")
            return questions
        except Exception as exc:
            last_error = exc
            logger.warning(
                "trivia: %s generation attempt %d/%d failed for %s: %s",
                lane,
                attempt,
                GENERATION_ATTEMPTS,
                puzzle_date,
                exc,
            )
    assert last_error is not None
    raise last_error


async def prepare_daily_puzzle(puzzle_date: date, *, force: bool = False, mode: str = "all") -> repo.DailyTriviaPuzzle:
    """Generate, validate against the bank, and atomically publish one day."""
    if mode not in {"all", "main", "super"}:
        raise ValueError("mode must be 'all', 'main', or 'super'")
    existing = await repo.get_puzzle(puzzle_date)
    if (
        existing
        and len(existing.questions) == 5
        and len(existing.super_questions) == 3
        and not force
        and existing.status == "ready"
    ):
        return existing

    from app.games import daily_trivia_authoring as authoring
    from app.repos.settings_repo import get_global_setting

    model_name = await get_global_setting("daily_trivia_llm_model", TRIVIA_MODEL)
    expected_revision = existing.revision if existing else 0
    regenerate_main = mode in {"all", "main"} or not existing or len(existing.questions) != 5
    regenerate_super = mode in {"all", "super"} or not existing or len(existing.super_questions) != 3
    preserved_main = list(existing.questions) if existing else []
    preserved_super = list(existing.super_questions) if existing else []
    last_conflict: authoring.DuplicateQuestionError | None = None

    for attempt in range(1, GENERATION_ATTEMPTS + 1):
        questions = (
            await generate_question_lane(puzzle_date, lane="main", model_name=model_name)
            if regenerate_main
            else preserved_main
        )
        super_questions = (
            await generate_question_lane(puzzle_date, lane="super", model_name=model_name)
            if regenerate_super
            else preserved_super
        )
        try:
            return await authoring.publish_authored_day(
                puzzle_date,
                main=questions,
                super_questions=super_questions,
                model_name=model_name,
                expected_revision=expected_revision,
                actor="admin" if force else "scheduler",
            )
        except authoring.DuplicateQuestionError as exc:
            last_conflict = exc
            logger.warning(
                "trivia: duplicate candidate on authoring attempt %d/%d for %s: %s",
                attempt,
                GENERATION_ATTEMPTS,
                puzzle_date,
                exc,
            )
    assert last_conflict is not None
    raise last_conflict


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
    puzzles: list[repo.DailyTriviaPuzzle] = []
    for puzzle_date in dates:
        puzzles.append(await prepare_daily_puzzle(puzzle_date))
    return puzzles
