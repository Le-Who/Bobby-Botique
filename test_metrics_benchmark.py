import timeit
import asyncio
from datetime import datetime

class MockSettings:
    DAILY_LIMITS = {"gemini-1.5-pro": 50, "gemini-1.5-flash": 1500}
    TAVILY_MONTHLY_CREDIT_LIMIT = 1000

settings = MockSettings()

def format_key_for_display(key):
    return key[:4] + "..." + key[-4:] if len(key) >= 8 else "test_key"

def get_system_status_data_sync():
    return {
        "metrics_summary": {
            "total_requests": 10000,
            "average_response_time": 1.5,
            "error_rate": 2.5,
            "cache_hit_rate": 80.0,
            "search_queries": 500,
            "api_calls": {f"api_{i}": i * 10 for i in range(50)},
            "model_usage": {f"model_{i}": i * 100 for i in range(50)},
            "daily_metrics": {f"2023-10-{10+i}": {"requests": 100+i, "errors": i} for i in range(5)},
            "recent_errors": [{"type": "TimeoutError", "message": "Connection timed out "*2} for _ in range(3)]
        },
        "gemini": {
            "keys": [{"api_key": f"test_key_{i}", "key_hash": f"hash_{i}"} for i in range(20)],
            "usage_map": {f"hash_{i}": [{"model_name": "gemini-1.5-pro", "request_count": i * 5}] for i in range(20)},
            "reset_time": "10:00"
        },
        "tavily": {
            "keys": [{"api_key": f"tav_key_{i}", "key_hash": f"thash_{i}"} for i in range(10)],
            "usage_map": {f"thash_{i}": i * 10 for i in range(10)}
        }
    }

def string_concat_metrics():
    data = get_system_status_data_sync()
    metrics = data["metrics_summary"]
    gemini_data = data["gemini"]
    tavily_data = data["tavily"]

    text = (
        "📊 *Полная сводка системы:*\n\n"
        "*🚀 Производительность:*\n"
        f"• Всего запросов: `{metrics['total_requests']}`\n"
        f"• Среднее время ответа: `{metrics['average_response_time']:.2f}s`\n"
        f"• Процент ошибок: `{metrics['error_rate']:.1f}%`\n"
        f"• Попадания в кэш: `{metrics['cache_hit_rate']:.1f}%`\n"
        f"• Поисковых запросов: `{metrics['search_queries']}`\n\n"
    )

    if metrics.get("api_calls"):
        text += "*🔌 Использование API:*\n"
        for api, count in metrics["api_calls"].items():
            if isinstance(api, str) and isinstance(count, (int, float)):
                text += f"• {api}: `{count}`\n"
        text += "\n"

    if metrics.get("model_usage"):
        text += "*🤖 Использование моделей:*\n"
        for model, count in metrics["model_usage"].items():
            if (
                isinstance(model, str)
                and isinstance(count, (int, float))
                and not any(char in model for char in ["/", "\\", ".pdf", ".docx", ".doc"])
            ):
                text += f"• {model}: `{count}`\n"
        text += "\n"

    if gemini_data["keys"]:
        text += "*🔑 Статус ключей Gemini (сегодня):*\n"
        usage_map = gemini_data["usage_map"]
        for key_row in gemini_data["keys"]:
            display_name = format_key_for_display(key_row["api_key"])
            usage_data = usage_map.get(key_row["key_hash"], [])
            if not usage_data:
                text += f"• `{display_name}`: не использовался\n"
            else:
                for usage in usage_data:
                    model_name = usage["model_name"]
                    count = usage["request_count"]
                    limit = settings.DAILY_LIMITS.get(model_name, "N/A")
                    text += f"• `{display_name}` ({model_name}): {count} / {limit}\n"
        text += f"Сброс лимитов: *{gemini_data['reset_time']}* по Киеву\n\n"

    if tavily_data["keys"]:
        text += "*💳 Кредиты Tavily (текущий месяц):*\n"
        tavily_usage_map = tavily_data["usage_map"]
        for key_row in tavily_data["keys"]:
            display_name = format_key_for_display(key_row["api_key"])
            count = tavily_usage_map.get(key_row["key_hash"], 0)
            limit = settings.TAVILY_MONTHLY_CREDIT_LIMIT
            text += f"• `{display_name}`: {count} / {limit}\n"
        text += "Сброс лимитов: 1-го числа каждого месяца\n\n"

    if metrics["daily_metrics"]:
        text += "*📈 История за последние дни:*\n"
        for date_str, daily_data in list(metrics["daily_metrics"].items())[:5]:
            requests = daily_data.get("requests", 0)
            errors = daily_data.get("errors", 0)
            text += f"• {date_str}: {requests} запросов, {errors} ошибок\n"
        text += "\n"

    if metrics["recent_errors"]:
        text += "*⚠️ Последние ошибки:*\n"
        for error in metrics["recent_errors"][:3]:
            text += f"• {error['type']}: {error['message'][:40]}...\n"

    text += f"\n_Обновлено: {datetime.now().strftime('%H:%M:%S UTC')}_"
    return text

def string_join_metrics():
    data = get_system_status_data_sync()
    metrics = data["metrics_summary"]
    gemini_data = data["gemini"]
    tavily_data = data["tavily"]

    parts = [
        "📊 *Полная сводка системы:*\n\n",
        "*🚀 Производительность:*\n",
        f"• Всего запросов: `{metrics['total_requests']}`\n",
        f"• Среднее время ответа: `{metrics['average_response_time']:.2f}s`\n",
        f"• Процент ошибок: `{metrics['error_rate']:.1f}%`\n",
        f"• Попадания в кэш: `{metrics['cache_hit_rate']:.1f}%`\n",
        f"• Поисковых запросов: `{metrics['search_queries']}`\n\n"
    ]

    if metrics.get("api_calls"):
        parts.append("*🔌 Использование API:*\n")
        for api, count in metrics["api_calls"].items():
            if isinstance(api, str) and isinstance(count, (int, float)):
                parts.append(f"• {api}: `{count}`\n")
        parts.append("\n")

    if metrics.get("model_usage"):
        parts.append("*🤖 Использование моделей:*\n")
        for model, count in metrics["model_usage"].items():
            if (
                isinstance(model, str)
                and isinstance(count, (int, float))
                and not any(char in model for char in ["/", "\\", ".pdf", ".docx", ".doc"])
            ):
                parts.append(f"• {model}: `{count}`\n")
        parts.append("\n")

    if gemini_data["keys"]:
        parts.append("*🔑 Статус ключей Gemini (сегодня):*\n")
        usage_map = gemini_data["usage_map"]
        for key_row in gemini_data["keys"]:
            display_name = format_key_for_display(key_row["api_key"])
            usage_data = usage_map.get(key_row["key_hash"], [])
            if not usage_data:
                parts.append(f"• `{display_name}`: не использовался\n")
            else:
                for usage in usage_data:
                    model_name = usage["model_name"]
                    count = usage["request_count"]
                    limit = settings.DAILY_LIMITS.get(model_name, "N/A")
                    parts.append(f"• `{display_name}` ({model_name}): {count} / {limit}\n")
        parts.append(f"Сброс лимитов: *{gemini_data['reset_time']}* по Киеву\n\n")

    if tavily_data["keys"]:
        parts.append("*💳 Кредиты Tavily (текущий месяц):*\n")
        tavily_usage_map = tavily_data["usage_map"]
        for key_row in tavily_data["keys"]:
            display_name = format_key_for_display(key_row["api_key"])
            count = tavily_usage_map.get(key_row["key_hash"], 0)
            limit = settings.TAVILY_MONTHLY_CREDIT_LIMIT
            parts.append(f"• `{display_name}`: {count} / {limit}\n")
        parts.append("Сброс лимитов: 1-го числа каждого месяца\n\n")

    if metrics["daily_metrics"]:
        parts.append("*📈 История за последние дни:*\n")
        for date_str, daily_data in list(metrics["daily_metrics"].items())[:5]:
            requests = daily_data.get("requests", 0)
            errors = daily_data.get("errors", 0)
            parts.append(f"• {date_str}: {requests} запросов, {errors} ошибок\n")
        parts.append("\n")

    if metrics["recent_errors"]:
        parts.append("*⚠️ Последние ошибки:*\n")
        for error in metrics["recent_errors"][:3]:
            parts.append(f"• {error['type']}: {error['message'][:40]}...\n")

    parts.append(f"\n_Обновлено: {datetime.now().strftime('%H:%M:%S UTC')}_")
    return "".join(parts)

n = 5000
concat_time = timeit.timeit(string_concat_metrics, number=n)
join_time = timeit.timeit(string_join_metrics, number=n)

print(f"Concat time: {concat_time:.4f}s")
print(f"Join time: {join_time:.4f}s")
print(f"Improvement: {((concat_time - join_time) / concat_time) * 100:.2f}%")
