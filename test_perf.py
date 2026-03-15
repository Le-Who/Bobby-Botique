import datetime
import timeit

week_res = [{"metric_date": datetime.date.today(), "cnt": i} for i in range(1000)]
model_res = [{"model_name": f"model_{i}", "cnt": i} for i in range(1000)]
streak = 5
badge = "🔥"
engagement = {"longest_streak": 10, "total_requests_7d": 1500, "active_days_7d": 7}
today_count = 500
doc_count = 10
conv_count = 20

def old_way():
    text = "📊 **Ваша статистика**\n\n"

    if streak > 0:
        text += f"{badge} **Серия:** `{streak}` {'день' if streak == 1 else 'дней'}\n"
        if engagement["longest_streak"] > streak:
            text += f"🏆 **Рекорд:** `{engagement['longest_streak']}` дней\n"
        text += "\n"

    text += f"📅 **Сегодня:** `{today_count}` запросов\n"
    text += f"📈 **7 дней:** `{engagement['total_requests_7d']}` запросов ({engagement['active_days_7d']}/7 дней)\n\n"

    if week_res:
        text += "📊 **По дням:**\n"
        for row in week_res:
            date_str = (
                row["metric_date"].strftime("%d.%m")
                if hasattr(row["metric_date"], "strftime")
                else str(row["metric_date"])[:5]
            )
            bar = "█" * min(int(row["cnt"]), 20)
            text += f"  `{date_str}` {bar} `{row['cnt']}`\n"
        text += "\n"

    if model_res:
        text += "🤖 **Модели сегодня:**\n"
        for row in model_res:
            text += f"  • `{row['model_name']}`: `{row['cnt']}` запросов\n"
        text += "\n"

    text += f"📄 **Документов:** `{doc_count}`\n📝 **Сохранённых бесед:** `{conv_count}`\n"
    return text

def new_way():
    text_parts = ["📊 **Ваша статистика**\n\n"]

    if streak > 0:
        text_parts.append(f"{badge} **Серия:** `{streak}` {'день' if streak == 1 else 'дней'}\n")
        if engagement["longest_streak"] > streak:
            text_parts.append(f"🏆 **Рекорд:** `{engagement['longest_streak']}` дней\n")
        text_parts.append("\n")

    text_parts.append(f"📅 **Сегодня:** `{today_count}` запросов\n")
    text_parts.append(f"📈 **7 дней:** `{engagement['total_requests_7d']}` запросов ({engagement['active_days_7d']}/7 дней)\n\n")

    if week_res:
        text_parts.append("📊 **По дням:**\n")
        for row in week_res:
            date_str = (
                row["metric_date"].strftime("%d.%m")
                if hasattr(row["metric_date"], "strftime")
                else str(row["metric_date"])[:5]
            )
            bar = "█" * min(int(row["cnt"]), 20)
            text_parts.append(f"  `{date_str}` {bar} `{row['cnt']}`\n")
        text_parts.append("\n")

    if model_res:
        text_parts.append("🤖 **Модели сегодня:**\n")
        for row in model_res:
            text_parts.append(f"  • `{row['model_name']}`: `{row['cnt']}` запросов\n")
        text_parts.append("\n")

    text_parts.append(f"📄 **Документов:** `{doc_count}`\n📝 **Сохранённых бесед:** `{conv_count}`\n")
    return "".join(text_parts)

print("Same text output?", old_way() == new_way())

old_time = timeit.timeit(old_way, number=1000)
new_time = timeit.timeit(new_way, number=1000)

print(f"Old time: {old_time}")
print(f"New time: {new_time}")
print(f"Improvement: {(old_time - new_time) / old_time * 100:.2f}%")
