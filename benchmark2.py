import timeit

documents = [
    {"filename": f"document_{i}.pdf", "pages": i % 100, "created_at": "2023-10-25 12:00:00", "file_size": 1024 * i}
    for i in range(100)
]


def test_concat():
    text = f"📄 **Документы** ({len(documents)})\n\n"
    for i, doc in enumerate(documents[:10], 1):
        text += f"{i}. **{doc['filename']}**\n"
        text += f"   📄 Страниц: {doc['pages']}\n"
        text += f"   📅 Загружен: {doc['created_at'][:10]}\n"
        text += f"   📊 Размер: {doc['file_size']:,} символов\n\n"
    if len(documents) > 10:
        text += f"… и ещё {len(documents) - 10} документов\n\n"
    text += "📎 Отправьте новый файл для загрузки."
    return text


def test_join_optimized():
    parts = [f"📄 **Документы** ({len(documents)})\n\n"]
    for i, doc in enumerate(documents[:10], 1):
        parts.append(
            f"{i}. **{doc['filename']}**\n"
            f"   📄 Страниц: {doc['pages']}\n"
            f"   📅 Загружен: {doc['created_at'][:10]}\n"
            f"   📊 Размер: {doc['file_size']:,} символов\n\n"
        )
    if len(documents) > 10:
        parts.append(f"… и ещё {len(documents) - 10} документов\n\n")
    parts.append("📎 Отправьте новый файл для загрузки.")
    return "".join(parts)


concat_time = timeit.timeit(test_concat, number=1000000)
join_opt_time = timeit.timeit(test_join_optimized, number=1000000)


# Ensure outputs match
assert test_concat() == test_join_optimized()
