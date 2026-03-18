import datetime
import timeit

from app.utils.formatting import TelegramFormatter


def create_mock_data():
    week_res = [
        {"metric_date": datetime.date(2023, 10, 1), "cnt": 10},
        {"metric_date": datetime.date(2023, 10, 2), "cnt": 20},
        {"metric_date": datetime.date(2023, 10, 3), "cnt": 30},
        {"metric_date": datetime.date(2023, 10, 4), "cnt": 40},
        {"metric_date": datetime.date(2023, 10, 5), "cnt": 50},
        {"metric_date": datetime.date(2023, 10, 6), "cnt": 60},
        {"metric_date": datetime.date(2023, 10, 7), "cnt": 70},
    ] * 20  # duplicate to simulate more entries

    model_res = [
        {"model_name": "gpt-3.5-turbo", "cnt": 100},
        {"model_name": "gpt-4", "cnt": 50},
        {"model_name": "claude-2", "cnt": 20},
    ] * 10  # duplicate to simulate more entries

    return week_res, model_res


def baseline(week_res, model_res):
    text = ""
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

    doc_count = 5
    conv_count = 10
    text += f"📄 **Документов:** `{doc_count}`\n📝 **Сохранённых бесед:** `{conv_count}`\n"
    return text


def optimized(week_res, model_res):
    parts = []
    if week_res:
        parts.append("📊 **По дням:**\n")
        for row in week_res:
            date_str = (
                row["metric_date"].strftime("%d.%m")
                if hasattr(row["metric_date"], "strftime")
                else str(row["metric_date"])[:5]
            )
            bar = "█" * min(int(row["cnt"]), 20)
            parts.append(f"  `{date_str}` {bar} `{row['cnt']}`\n")
        parts.append("\n")

    if model_res:
        parts.append("🤖 **Модели сегодня:**\n")
        for row in model_res:
            parts.append(f"  • `{row['model_name']}`: `{row['cnt']}` запросов\n")
        parts.append("\n")

    doc_count = 5
    conv_count = 10
    parts.append(f"📄 **Документов:** `{doc_count}`\n📝 **Сохранённых бесед:** `{conv_count}`\n")
    return "".join(parts)


if __name__ == "__main__":
    week_res, model_res = create_mock_data()

    # Verify correctness
    assert baseline(week_res, model_res) == optimized(week_res, model_res)

    n_runs = 10000
    t_baseline = timeit.timeit(lambda: baseline(week_res, model_res), number=n_runs)
    t_optimized = timeit.timeit(lambda: optimized(week_res, model_res), number=n_runs)

    if t_optimized < t_baseline:
        improvement = (t_baseline - t_optimized) / t_baseline * 100
    else:
        pass
