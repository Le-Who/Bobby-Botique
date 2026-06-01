"""
Общие типы и перечисления для всего приложения
"""
from enum import Enum
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Any, Optional


class TaskStatus(Enum):
    """Статусы задач"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(Enum):
    """Приоритеты задач"""
    LOW = 1
    NORMAL = 2
    HIGH = 3
    URGENT = 4


@dataclass
class Task:
    """Задача для выполнения в очереди"""
    id: str
    user_id: int
    task_type: str
    data: Dict[str, Any]
    priority: TaskPriority = TaskPriority.NORMAL
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = None
    completed_at: Optional[datetime] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    max_retries: int = 3
    retry_count: int = 0

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        """Преобразует задачу в словарь для сериализации"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'task_type': self.task_type,
            'data': self.data,
            'priority': self.priority.value,
            'status': self.status.value,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'result': self.result,
            'error': self.error,
            'max_retries': self.max_retries,
            'retry_count': self.retry_count
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Task':
        """Создает задачу из словаря"""
        return cls(
            id=data['id'],
            user_id=data['user_id'],
            task_type=data['task_type'],
            data=data['data'],
            priority=TaskPriority(data['priority']),
            status=TaskStatus(data['status']),
            created_at=datetime.fromisoformat(data['created_at']) if data.get('created_at') else None,
            completed_at=datetime.fromisoformat(data['completed_at']) if data.get('completed_at') else None,
            result=data.get('result'),
            error=data.get('error'),
            max_retries=data.get('max_retries', 3),
            retry_count=data.get('retry_count', 0)
        )
