import json
import logging
import time
from typing import Any, Dict


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Обогащение полями контекста если заданы
        for key in ("user_id", "chat_id", "request_id"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class RequestContextFilter(logging.Filter):
    """Фильтр для добавления контекста запроса в запись, если он установлен глобально."""
    def filter(self, record: logging.LogRecord) -> bool:
        # Тут можно внедрять глобальный контекст (например, contextvars) при желании
        return True


