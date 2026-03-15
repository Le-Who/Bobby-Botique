import time
import timeit

# Mock settings
class MockSettings:
    TAVILY_MONTHLY_CREDIT_LIMIT = 1000
    TAVILY_LIMIT_THRESHOLD_PERCENT = 0.8
settings = MockSettings()

# Mock safe_decrypt
def safe_decrypt(key):
    return key

# Mock data
num_keys = 100000
keys_result = [{"key_hash": f"hash_{i}_" * 10, "api_key": f"key_{i}_" * 10} for i in range(num_keys)]
usage_result = [{"key_hash": f"hash_{i}_" * 10, "credit_usage": i} for i in range(num_keys)]
current_month = "2023-10"

def baseline_report():
    report = f"📋 Найдено {len(keys_result)} ключей Tavily API:\n\n"

    for i, row in enumerate(keys_result, 1):
        key_hash = row["key_hash"]
        api_key = safe_decrypt(row["api_key"])
        report += f"🔑 *Ключ {i}:*\n"
        report += f"   Хэш: `{key_hash[:16]}...`\n"
        report += f"   API: `{api_key[:10]}...{api_key[-4:]}`\n\n"

    if usage_result:
        report += f"📊 *Использование за {current_month}:*\n"
        for row in usage_result:
            key_preview = row["key_hash"][:16] + "..."
            usage = row["credit_usage"]
            report += f"   `{key_preview}`: {usage} кредитов\n"
    else:
        report += f"📊 *Использование за {current_month}:*\n   Нет данных\n"

    report += "\n⚡ *Лимиты:*\n"
    report += f"   Месячный лимит: {settings.TAVILY_MONTHLY_CREDIT_LIMIT} кредитов\n"
    report += f"   Порог предупреждения: {settings.TAVILY_LIMIT_THRESHOLD_PERCENT * 100}%\n"

    return report

def optimized_report():
    report_parts = [f"📋 Найдено {len(keys_result)} ключей Tavily API:\n\n"]

    for i, row in enumerate(keys_result, 1):
        key_hash = row["key_hash"]
        api_key = safe_decrypt(row["api_key"])
        report_parts.append(f"🔑 *Ключ {i}:*\n   Хэш: `{key_hash[:16]}...`\n   API: `{api_key[:10]}...{api_key[-4:]}`\n\n")

    if usage_result:
        report_parts.append(f"📊 *Использование за {current_month}:*\n")
        for row in usage_result:
            key_preview = row["key_hash"][:16] + "..."
            usage = row["credit_usage"]
            report_parts.append(f"   `{key_preview}`: {usage} кредитов\n")
    else:
        report_parts.append(f"📊 *Использование за {current_month}:*\n   Нет данных\n")

    report_parts.append("\n⚡ *Лимиты:*\n")
    report_parts.append(f"   Месячный лимит: {settings.TAVILY_MONTHLY_CREDIT_LIMIT} кредитов\n")
    report_parts.append(f"   Порог предупреждения: {settings.TAVILY_LIMIT_THRESHOLD_PERCENT * 100}%\n")

    return "".join(report_parts)

if __name__ == "__main__":
    baseline_time = timeit.timeit(baseline_report, number=1)
    print(f"Baseline Time: {baseline_time:.4f} seconds")

    optimized_time = timeit.timeit(optimized_report, number=1)
    print(f"Optimized Time: {optimized_time:.4f} seconds")

    print(f"Speedup: {baseline_time / optimized_time:.2f}x")
