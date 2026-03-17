import timeit

class DummySettings:
    GEMINI_API_KEYS = list(range(10))
    TAVILY_API_KEYS = list(range(5))
    OPENROUTER_API_KEYS = list(range(3))
    AVAILABLE_MODELS = list(range(20))
    OPENROUTER_AVAILABLE_MODELS = list(range(15))
    DEFAULT_MODEL = "gemini-pro"
    PORT = 8080
    ADMIN_ID = 123456789
    DAILY_LIMITS = list(range(100))

new_settings = DummySettings()

def original_method():
    report = "✅ *Конфигурация перезагружена*\n\n"
    report += "🔑 *API ключи:*\n"
    report += f"• Gemini: `{len(new_settings.GEMINI_API_KEYS)}` ключей\n"
    report += f"• Tavily: `{len(new_settings.TAVILY_API_KEYS)}` ключей\n"
    report += f"• OpenRouter: `{len(new_settings.OPENROUTER_API_KEYS)}` ключей\n\n"
    report += "🤖 *Модели:*\n"
    report += f"• Gemini: `{len(new_settings.AVAILABLE_MODELS)}` моделей\n"
    report += f"• OpenRouter: `{len(new_settings.OPENROUTER_AVAILABLE_MODELS)}` моделей\n"
    report += f"• По умолчанию: `{new_settings.DEFAULT_MODEL}`\n\n"
    report += "⚙️ *Настройки:*\n"
    report += f"• PORT: `{new_settings.PORT}`\n"
    report += f"• ADMIN_ID: `{new_settings.ADMIN_ID}`\n"
    report += f"• Лимитов моделей: `{len(new_settings.DAILY_LIMITS)}`\n\n"
    report += "💡 Все настройки загружены из переменных окружения."
    return report

def optimized_method():
    report_parts = [
        "✅ *Конфигурация перезагружена*\n\n",
        "🔑 *API ключи:*\n",
        f"• Gemini: `{len(new_settings.GEMINI_API_KEYS)}` ключей\n",
        f"• Tavily: `{len(new_settings.TAVILY_API_KEYS)}` ключей\n",
        f"• OpenRouter: `{len(new_settings.OPENROUTER_API_KEYS)}` ключей\n\n",
        "🤖 *Модели:*\n",
        f"• Gemini: `{len(new_settings.AVAILABLE_MODELS)}` моделей\n",
        f"• OpenRouter: `{len(new_settings.OPENROUTER_AVAILABLE_MODELS)}` моделей\n",
        f"• По умолчанию: `{new_settings.DEFAULT_MODEL}`\n\n",
        "⚙️ *Настройки:*\n",
        f"• PORT: `{new_settings.PORT}`\n",
        f"• ADMIN_ID: `{new_settings.ADMIN_ID}`\n",
        f"• Лимитов моделей: `{len(new_settings.DAILY_LIMITS)}`\n\n",
        "💡 Все настройки загружены из переменных окружения."
    ]
    return "".join(report_parts)

if __name__ == "__main__":
    t1 = timeit.timeit(original_method, number=100000)
    t2 = timeit.timeit(optimized_method, number=100000)
    print(f"Original += method: {t1:.4f} seconds")
    print(f"Optimized join method: {t2:.4f} seconds")
    if t1 > 0:
        improvement = ((t1 - t2) / t1) * 100
        print(f"Improvement: {improvement:.2f}%")
