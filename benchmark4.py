import timeit


def test_concat():
    text = "*🚀 Производительность:*\n"
    text += f"• Всего запросов: `{100}`\n"
    text += f"• Среднее время ответа: `{1.5:.2f}s`\n"
    text += f"• Процент ошибок: `{0.5:.1f}%`\n"
    text += f"• Попадания в кэш: `{90.0:.1f}%`\n"
    text += f"• Поисковых запросов: `{50}`\n\n"
    return text

def test_join_optimized():
    parts = [
        "*🚀 Производительность:*\n",
        f"• Всего запросов: `{100}`\n",
        f"• Среднее время ответа: `{1.5:.2f}s`\n",
        f"• Процент ошибок: `{0.5:.1f}%`\n",
        f"• Попадания в кэш: `{90.0:.1f}%`\n",
        f"• Поисковых запросов: `{50}`\n\n"
    ]
    return "".join(parts)

print("Running benchmarks (1000000 iterations)...")
concat_time = timeit.timeit(test_concat, number=1000000)
join_opt_time = timeit.timeit(test_join_optimized, number=1000000)

print(f"Concat: {concat_time:.4f}s")
print(f"Join: {join_opt_time:.4f}s")

assert test_concat() == test_join_optimized()
