import time
import timeit

from app.crypto import safe_decrypt


# Dummy safe_decrypt for testing
def dummy_safe_decrypt(api_key):
    return api_key

# Mock keys
keys_result = [
    {"key_hash": f"hash_{i}_" * 4, "api_key": f"apikey_{i}_" * 4}
    for i in range(1000)
]

def original_method():
    report = f"📋 Найдено {len(keys_result)} ключей Tavily API:\n\n"
    for i, row in enumerate(keys_result, 1):
        key_hash = row["key_hash"]
        api_key = dummy_safe_decrypt(row["api_key"])
        report += f"🔑 *Ключ {i}:*\n"
        report += f"   Хэш: `{key_hash[:16]}...`\n"
        report += f"   API: `{api_key[:10]}...{api_key[-4:]}`\n\n"

    usage_result = [{"key_hash": f"hash_{i}_" * 4, "credit_usage": 10} for i in range(1000)]
    current_month = "2024-05"
    if usage_result:
        report += f"📊 *Использование за {current_month}:*\n"
        for row in usage_result:
            key_preview = row["key_hash"][:16] + "..."
            usage = row["credit_usage"]
            report += f"   `{key_preview}`: {usage} кредитов\n"
    else:
        report += f"📊 *Использование за {current_month}:*\n   Нет данных\n"

def optimized_method():
    report_parts = [f"📋 Найдено {len(keys_result)} ключей Tavily API:\n\n"]
    for i, row in enumerate(keys_result, 1):
        key_hash = row["key_hash"]
        api_key = dummy_safe_decrypt(row["api_key"])
        report_parts.append(
            f"🔑 *Ключ {i}:*\n"
            f"   Хэш: `{key_hash[:16]}...`\n"
            f"   API: `{api_key[:10]}...{api_key[-4:]}`\n\n"
        )

    usage_result = [{"key_hash": f"hash_{i}_" * 4, "credit_usage": 10} for i in range(1000)]
    current_month = "2024-05"
    if usage_result:
        report_parts.append(f"📊 *Использование за {current_month}:*\n")
        for row in usage_result:
            key_preview = row["key_hash"][:16] + "..."
            usage = row["credit_usage"]
            report_parts.append(f"   `{key_preview}`: {usage} кредитов\n")
    else:
        report_parts.append(f"📊 *Использование за {current_month}:*\n   Нет данных\n")

    report = "".join(report_parts)

if __name__ == "__main__":
    t1 = timeit.timeit(original_method, number=100)
    t2 = timeit.timeit(optimized_method, number=100)
    print(f"Original method: {t1:.4f} seconds")
    print(f"Optimized method: {t2:.4f} seconds")
    if t1 > 0:
        improvement = ((t1 - t2) / t1) * 100
        print(f"Improvement: {improvement:.2f}%")
