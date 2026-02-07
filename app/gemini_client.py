from typing import Optional

class GeminiClient:
    def _find_available_key(self, model_name: str) -> Optional[str]:
        # Stub implementation
        pass

    def _set_current_key(self):
        # Stub implementation
        pass

    def _rotate_key(self, model_name: str):
        """Переключается на следующий доступный ключ для данной модели."""
        available_key = self._find_available_key(model_name)

        if not available_key:
            # Если для этой модели нет ключей, возможно они есть для других (но тут мы застряли)
            raise Exception(f"No available API keys for model {model_name}. All keys are either in cooldown or at limit.")

        self._set_current_key()
