"""Catalog status for historical templates with no current runtime reader."""

_REPLACEMENTS = {
    "qna_localization": "search.native",
    "url_selection": "research_agent_system",
    "synthesis": "research.synthesis.system",
    "summarization_system": "summary.system",
    "summarization_chunk": "summary.chunk",
    "summarization_refine_first": "summary.first",
    "summarization_refine_subsequent": "summary.refine",
}


def prompt_control_metadata(name: str) -> dict[str, object]:
    if name.startswith("live."):
        return {
            "editable": True,
            "apply": "new_session",
            "note": "Активный голосовой сеанс сохраняет прежнюю инструкцию.",
        }
    if name.startswith("role."):
        return {
            "editable": True,
            "apply": "new_role",
            "note": "Используется при новом выборе этого пресета. Сохранённые персональные копии роли не переписываются.",
        }
    replacement = _REPLACEMENTS.get(name)
    if replacement is None:
        return {"editable": True, "apply": "next_request"}
    return {
        "editable": False,
        "apply": "inactive",
        "current_prompt": replacement,
        "note": f"Этот исторический шаблон не используется текущими обработчиками. Действующая инструкция: {replacement}. "
        "Сохранённое старое значение показано для совместимости и не управляет новыми запросами.",
    }


def ensure_prompt_editable(name: str) -> None:
    metadata = prompt_control_metadata(name)
    if not metadata["editable"]:
        raise ValueError(str(metadata["note"]))
