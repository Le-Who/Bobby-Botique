import time
from datetime import datetime


class SettingsMock:
    DAILY_LIMITS = {"gemini-pro": 100}
    TAVILY_MONTHLY_CREDIT_LIMIT = 1000

settings = SettingsMock()

def format_key_for_display(k):
    return k[:4] + "..."

def get_metrics_content_concat(metrics, gemini_data, tavily_data):
    # Build main text
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


def get_metrics_content_join(metrics, gemini_data, tavily_data):
    parts = []

    parts.append(
        "📊 *Полная сводка системы:*\n\n"
        "*🚀 Производительность:*\n"
        f"• Всего запросов: `{metrics['total_requests']}`\n"
        f"• Среднее время ответа: `{metrics['average_response_time']:.2f}s`\n"
        f"• Процент ошибок: `{metrics['error_rate']:.1f}%`\n"
        f"• Попадания в кэш: `{metrics['cache_hit_rate']:.1f}%`\n"
        f"• Поисковых запросов: `{metrics['search_queries']}`\n\n"
    )

    if metrics.get("api_calls"):
        parts.append("*🔌 Использование API:*\n")
        for api, count in metrics["api_calls"].items():
            if isinstance(api, str) and isinstance(count, (int, float)):
                parts.append(f"• {api}: `{count}`\n")
        parts.append("\n")

    if metrics.get("model_usage"):
        parts.append("*🤖 Использование моделей:*\n")
        invalid_chars = ["/", "\\", ".pdf", ".docx", ".doc"]
        for model, count in metrics["model_usage"].items():
            if (
                isinstance(model, str)
                and isinstance(count, (int, float))
                and not any(char in model for char in invalid_chars)
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


def run_benchmark():
    # Large dataset
    N = 100000
    metrics = {
        "total_requests": 1000,
        "average_response_time": 0.5,
        "error_rate": 2.5,
        "cache_hit_rate": 80.0,
        "search_queries": 150,
        "api_calls": {f"api_{i}": i for i in range(N)},
        "model_usage": {f"model_{i}": i for i in range(N)},
        "daily_metrics": {f"2023-10-{i:02d}": {"requests": i, "errors": i % 5} for i in range(1, 31)},
        "recent_errors": [{"type": f"Error{i}", "message": f"Message{i}"} for i in range(10)]
    }

    gemini_data = {
        "keys": [{"api_key": f"key_{i}", "key_hash": f"hash_{i}"} for i in range(N)],
        "usage_map": {f"hash_{i}": [{"model_name": "gemini-pro", "request_count": i}] for i in range(N)},
        "reset_time": "00:00"
    }

    tavily_data = {
        "keys": [{"api_key": f"tkey_{i}", "key_hash": f"thash_{i}"} for i in range(N)],
        "usage_map": {f"thash_{i}": i for i in range(N)}
    }

    # Warmup
    get_metrics_content_concat(metrics, gemini_data, tavily_data)
    get_metrics_content_join(metrics, gemini_data, tavily_data)

    iters = 1

    t0 = time.perf_counter()
    for _ in range(iters):
        res1 = get_metrics_content_concat(metrics, gemini_data, tavily_data)
    t1 = time.perf_counter()
    concat_time = (t1 - t0) / iters

    t0 = time.perf_counter()
    for _ in range(iters):
        res2 = get_metrics_content_join(metrics, gemini_data, tavily_data)
    t1 = time.perf_counter()
    join_time = (t1 - t0) / iters

    # Just to check correctness.
    if len(res1) != len(res2):
        print(f"Lengths differ: concat={len(res1)}, join={len(res2)}")

    print(f"Concat average: {concat_time:.5f}s")
    print(f"Join average:   {join_time:.5f}s")
    print(f"Improvement:    {concat_time / join_time:.2f}x faster")

run_benchmark()
