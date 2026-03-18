import timeit

missing = {"gemini-pro-vision", "gemini-ultra"}
available = {"gemini-pro", "gemini-1.5-pro", "gemini-1.5-flash"}


def original_method():
    validation = "\n\n*🔍 Проверка конфигурации:*\n"
    validation += f"✅ Доступны: `{', '.join(sorted(available)) or 'нет'}`\n"
    if missing:
        validation += f"❌ НЕ найдены в API: `{', '.join(sorted(missing))}`\n"
        validation += "⚠️ Запросы к этим моделям будут вызывать ошибки ключей!\n"
    else:
        validation += "✅ Все настроенные модели доступны в API\n"
    return validation


def optimized_method():
    validation_parts = [
        "\n\n*🔍 Проверка конфигурации:*\n",
        f"✅ Доступны: `{', '.join(sorted(available)) or 'нет'}`\n",
    ]
    if missing:
        validation_parts.append(
            f"❌ НЕ найдены в API: `{', '.join(sorted(missing))}`\n"
            "⚠️ Запросы к этим моделям будут вызывать ошибки ключей!\n"
        )
    else:
        validation_parts.append("✅ Все настроенные модели доступны в API\n")
    return "".join(validation_parts)


if __name__ == "__main__":
    t1 = timeit.timeit(original_method, number=100000)
    t2 = timeit.timeit(optimized_method, number=100000)
    if t1 > 0:
        improvement = ((t1 - t2) / t1) * 100
