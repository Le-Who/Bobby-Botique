import time
import asyncio

async def original_code(keys_result, usage_result):
    report = f"📋 Найдено {len(keys_result)} ключей Tavily API:\n\n"

    for i, row in enumerate(keys_result, 1):
        key_hash = row["key_hash"]
        api_key = row["api_key"]
        report += f"🔑 *Ключ {i}:*\n"
        report += f"   Хэш: `{key_hash[:16]}...`\n"
        report += f"   API: `{api_key[:10]}...{api_key[-4:]}`\n\n"

    current_month = "2023-10"

    if usage_result:
        report += f"📊 *Использование за {current_month}:*\n"
        for row in usage_result:
            key_preview = row["key_hash"][:16] + "..."
            usage = row["credit_usage"]
            report += f"   `{key_preview}`: {usage} кредитов\n"
    else:
        report += f"📊 *Использование за {current_month}:*\n   Нет данных\n"

    report += "\n⚡ *Лимиты:*\n"
    report += f"   Месячный лимит: {1000} кредитов\n"
    report += f"   Порог предупреждения: {0.8 * 100}%\n"
    return report

async def optimized_code(keys_result, usage_result):
    report_parts = [f"📋 Найдено {len(keys_result)} ключей Tavily API:\n\n"]

    for i, row in enumerate(keys_result, 1):
        key_hash = row["key_hash"]
        api_key = row["api_key"]
        report_parts.append(f"🔑 *Ключ {i}:*\n   Хэш: `{key_hash[:16]}...`\n   API: `{api_key[:10]}...{api_key[-4:]}`\n\n")

    current_month = "2023-10"

    if usage_result:
        report_parts.append(f"📊 *Использование за {current_month}:*\n")
        for row in usage_result:
            key_preview = row["key_hash"][:16] + "..."
            usage = row["credit_usage"]
            report_parts.append(f"   `{key_preview}`: {usage} кредитов\n")
    else:
        report_parts.append(f"📊 *Использование за {current_month}:*\n   Нет данных\n")

    report_parts.append("\n⚡ *Лимиты:*\n")
    report_parts.append(f"   Месячный лимит: {1000} кредитов\n")
    report_parts.append(f"   Порог предупреждения: {0.8 * 100}%\n")

    return "".join(report_parts)

async def run_benchmark():
    keys_result = [
        {"key_hash": f"hash_{i}_" * 5, "api_key": f"key_{i}_" * 10}
        for i in range(50000)
    ]
    usage_result = [
        {"key_hash": f"hash_{i}_" * 5, "credit_usage": i}
        for i in range(50000)
    ]

    print(f"Benchmarking with {len(keys_result)} keys and {len(usage_result)} usages.")

    start_orig = time.perf_counter()
    res1 = await original_code(keys_result, usage_result)
    end_orig = time.perf_counter()
    time_orig = end_orig - start_orig
    print(f"Original Code Time: {time_orig:.4f} seconds")

    start_opt = time.perf_counter()
    res2 = await optimized_code(keys_result, usage_result)
    end_opt = time.perf_counter()
    time_opt = end_opt - start_opt
    print(f"Optimized Code Time: {time_opt:.4f} seconds")

    print(f"Improvement: {time_orig / time_opt:.2f}x faster")
    assert res1 == res2, "Output mismatch!"

if __name__ == "__main__":
    asyncio.run(run_benchmark())
