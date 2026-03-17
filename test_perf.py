import timeit
import time

def dummy_safe_decrypt(api_key):
    return api_key

keys_result = [
    {"key_hash": f"hash_{i}_" * 4, "api_key": f"apikey_{i}_" * 4}
    for i in range(1000)
]

def method1():
    report_parts = [f"📋 Найдено {len(keys_result)} ключей Tavily API:\n\n"]
    for i, row in enumerate(keys_result, 1):
        key_hash = row["key_hash"]
        api_key = dummy_safe_decrypt(row["api_key"])
        report_parts.append(f"🔑 *Ключ {i}:*\n")
        report_parts.append(f"   Хэш: `{key_hash[:16]}...`\n")
        report_parts.append(f"   API: `{api_key[:10]}...{api_key[-4:]}`\n\n")

def method2():
    report_parts = [f"📋 Найдено {len(keys_result)} ключей Tavily API:\n\n"]
    for i, row in enumerate(keys_result, 1):
        key_hash = row["key_hash"]
        api_key = dummy_safe_decrypt(row["api_key"])
        report_parts.append(
            f"🔑 *Ключ {i}:*\n"
            f"   Хэш: `{key_hash[:16]}...`\n"
            f"   API: `{api_key[:10]}...{api_key[-4:]}`\n\n"
        )

if __name__ == "__main__":
    t1 = timeit.timeit(method1, number=1000)
    t2 = timeit.timeit(method2, number=1000)
    print(f"Multiple appends: {t1:.4f} seconds")
    print(f"Single append with implicit string concat: {t2:.4f} seconds")
    if t1 > 0:
        improvement = ((t1 - t2) / t1) * 100
        print(f"Improvement: {improvement:.2f}%")
