"""
Модуль для управления миграциями базы данных с использованием Alembic-подобного подхода
"""
import logging
from typing import List, Dict, Any
from datetime import datetime
from .database import db_query


class Migration:
    """Базовый класс для миграций"""
    def __init__(self, version: str, description: str):
        self.version = version
        self.description = description
        self.created_at = datetime.now()
    
    async def up(self):
        """Применить миграцию"""
        raise NotImplementedError("Subclasses must implement up() method")
    
    async def down(self):
        """Откатить миграцию"""
        raise NotImplementedError("Subclasses must implement down() method")


class Migration_20250101_001_AddIndexes(Migration):
    """Добавляет индексы для оптимизации производительности"""
    
    def __init__(self):
        super().__init__("20250101_001", "Add performance indexes")
    
    async def up(self):
        """Добавляет индексы"""
        indexes = [
            # Индексы для таблицы metrics
            "CREATE INDEX IF NOT EXISTS idx_metrics_date ON metrics(metric_date);",
            "CREATE INDEX IF NOT EXISTS idx_metrics_type ON metrics(metric_type);",
            "CREATE INDEX IF NOT EXISTS idx_metrics_date_type ON metrics(metric_date, metric_type);",
            
            # Индексы для таблицы chat_sessions
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_id ON chat_sessions(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_created_at ON chat_sessions(created_at);",
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_created ON chat_sessions(user_id, created_at);",
            
            # Индексы для таблицы api_usage_stats
            "CREATE INDEX IF NOT EXISTS idx_api_usage_user_date ON api_usage_stats(user_id, usage_date);",
            "CREATE INDEX IF NOT EXISTS idx_api_usage_date ON api_usage_stats(usage_date);",
            
            # Индексы для таблицы tavily_keys
            "CREATE INDEX IF NOT EXISTS idx_tavily_keys_usage ON tavily_keys(monthly_usage);",
            "CREATE INDEX IF NOT EXISTS idx_tavily_keys_limit ON tavily_keys(monthly_limit);",
            
            # Индексы для таблицы bot_settings
            "CREATE INDEX IF NOT EXISTS idx_bot_settings_name ON bot_settings(setting_name);",
            "CREATE INDEX IF NOT EXISTS idx_bot_settings_updated ON bot_settings(updated_at);",
            
            # Индексы для таблицы error_logs
            "CREATE INDEX IF NOT EXISTS idx_error_logs_created ON error_logs(created_at);",
            "CREATE INDEX IF NOT EXISTS idx_error_logs_type ON error_logs(error_type);",
            "CREATE INDEX IF NOT EXISTS idx_error_logs_type_created ON error_logs(error_type, created_at);",
        ]
        
        for index_sql in indexes:
            try:
                await db_query(index_sql)
                logging.info(f"Successfully created index: {index_sql.split('idx_')[1].split(' ')[0] if 'idx_' in index_sql else 'unnamed'}")
            except Exception as e:
                logging.error(f"Failed to create index: {index_sql} - {e}")
                raise
    
    async def down(self):
        """Удаляет индексы"""
        indexes_to_drop = [
            "DROP INDEX IF EXISTS idx_metrics_date;",
            "DROP INDEX IF EXISTS idx_metrics_type;",
            "DROP INDEX IF EXISTS idx_metrics_date_type;",
            "DROP INDEX IF EXISTS idx_chat_sessions_user_id;",
            "DROP INDEX IF EXISTS idx_chat_sessions_created_at;",
            "DROP INDEX IF EXISTS idx_chat_sessions_user_created;",
            "DROP INDEX IF EXISTS idx_api_usage_user_date;",
            "DROP INDEX IF EXISTS idx_api_usage_date;",
            "DROP INDEX IF EXISTS idx_tavily_keys_usage;",
            "DROP INDEX IF EXISTS idx_tavily_keys_limit;",
            "DROP INDEX IF EXISTS idx_bot_settings_name;",
            "DROP INDEX IF EXISTS idx_bot_settings_updated;",
            "DROP INDEX IF EXISTS idx_error_logs_created;",
            "DROP INDEX IF EXISTS idx_error_logs_type;",
            "DROP INDEX IF EXISTS idx_error_logs_type_created;",
        ]
        
        for drop_sql in indexes_to_drop:
            try:
                await db_query(drop_sql)
                logging.info(f"Successfully dropped index: {drop_sql}")
            except Exception as e:
                logging.warning(f"Failed to drop index (may not exist): {drop_sql} - {e}")


class Migration_20250101_002_OptimizeColumns(Migration):
    """Оптимизация типов колонок и добавление ограничений"""
    
    def __init__(self):
        super().__init__("20250101_002", "Optimize column types and constraints")
    
    async def up(self):
        """Оптимизирует колонки"""
        optimizations = [
            # Добавляем ограничения на таблицу metrics
            "ALTER TABLE metrics ADD CONSTRAINT IF NOT EXISTS chk_metrics_value_positive CHECK (metric_value >= 0);",
            
            # Добавляем ограничения на api_usage_stats
            "ALTER TABLE api_usage_stats ADD CONSTRAINT IF NOT EXISTS chk_api_calls_positive CHECK (api_calls >= 0);",
            "ALTER TABLE api_usage_stats ADD CONSTRAINT IF NOT EXISTS chk_tokens_positive CHECK (tokens_used >= 0);",
            
            # Добавляем ограничения на tavily_keys
            "ALTER TABLE tavily_keys ADD CONSTRAINT IF NOT EXISTS chk_tavily_usage_positive CHECK (monthly_usage >= 0);",
            "ALTER TABLE tavily_keys ADD CONSTRAINT IF NOT EXISTS chk_tavily_limit_positive CHECK (monthly_limit > 0);",
            "ALTER TABLE tavily_keys ADD CONSTRAINT IF NOT EXISTS chk_tavily_usage_limit CHECK (monthly_usage <= monthly_limit);",
        ]
        
        for optimization_sql in optimizations:
            try:
                await db_query(optimization_sql)
                logging.info(f"Successfully applied optimization: {optimization_sql.split('ADD CONSTRAINT')[1].split(' ')[2] if 'ADD CONSTRAINT' in optimization_sql else 'unnamed'}")
            except Exception as e:
                logging.warning(f"Failed to apply optimization (may already exist): {optimization_sql} - {e}")
    
    async def down(self):
        """Удаляет ограничения"""
        constraints_to_drop = [
            "ALTER TABLE metrics DROP CONSTRAINT IF EXISTS chk_metrics_value_positive;",
            "ALTER TABLE api_usage_stats DROP CONSTRAINT IF EXISTS chk_api_calls_positive;",
            "ALTER TABLE api_usage_stats DROP CONSTRAINT IF EXISTS chk_tokens_positive;",
            "ALTER TABLE tavily_keys DROP CONSTRAINT IF EXISTS chk_tavily_usage_positive;",
            "ALTER TABLE tavily_keys DROP CONSTRAINT IF EXISTS chk_tavily_limit_positive;",
            "ALTER TABLE tavily_keys DROP CONSTRAINT IF EXISTS chk_tavily_usage_limit;",
        ]
        
        for drop_sql in constraints_to_drop:
            try:
                await db_query(drop_sql)
                logging.info(f"Successfully dropped constraint: {drop_sql}")
            except Exception as e:
                logging.warning(f"Failed to drop constraint (may not exist): {drop_sql} - {e}")


class MigrationManager:
    """Менеджер миграций"""
    
    def __init__(self):
        self.migrations: List[Migration] = [
            Migration_20250101_001_AddIndexes(),
            Migration_20250101_002_OptimizeColumns(),
        ]
    
    async def init_migration_table(self):
        """Создает таблицу для отслеживания миграций"""
        await db_query("""
            CREATE TABLE IF NOT EXISTS db_migrations (
                id SERIAL PRIMARY KEY,
                version VARCHAR(50) UNIQUE NOT NULL,
                description TEXT,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                execution_time_ms INTEGER
            )
        """)
        logging.info("Migration tracking table initialized")
    
    async def get_applied_migrations(self) -> List[str]:
        """Получает список примененных миграций"""
        try:
            result = await db_query("SELECT version FROM db_migrations ORDER BY applied_at")
            return [row['version'] for row in result] if result else []
        except Exception as e:
            logging.warning(f"Failed to get applied migrations: {e}")
            return []
    
    async def apply_migration(self, migration: Migration) -> bool:
        """Применяет одну миграцию"""
        start_time = datetime.now()
        
        try:
            logging.info(f"Applying migration {migration.version}: {migration.description}")
            await migration.up()
            
            # Записываем информацию о миграции
            execution_time = int((datetime.now() - start_time).total_seconds() * 1000)
            await db_query(
                "INSERT INTO db_migrations (version, description, execution_time_ms) VALUES ($1, $2, $3)",
                (migration.version, migration.description, execution_time)
            )
            
            logging.info(f"Migration {migration.version} applied successfully in {execution_time}ms")
            return True
            
        except Exception as e:
            logging.error(f"Failed to apply migration {migration.version}: {e}")
            return False
    
    async def rollback_migration(self, migration: Migration) -> bool:
        """Откатывает миграцию"""
        try:
            logging.info(f"Rolling back migration {migration.version}: {migration.description}")
            await migration.down()
            
            # Удаляем запись о миграции
            await db_query("DELETE FROM db_migrations WHERE version = $1", migration.version)
            
            logging.info(f"Migration {migration.version} rolled back successfully")
            return True
            
        except Exception as e:
            logging.error(f"Failed to rollback migration {migration.version}: {e}")
            return False
    
    async def migrate_up(self) -> Dict[str, Any]:
        """Применяет все неприменённые миграции"""
        await self.init_migration_table()
        applied_migrations = await self.get_applied_migrations()
        
        pending_migrations = [
            m for m in self.migrations 
            if m.version not in applied_migrations
        ]
        
        if not pending_migrations:
            logging.info("No pending migrations")
            return {"applied": 0, "total": len(self.migrations), "status": "up_to_date"}
        
        applied_count = 0
        total_start_time = datetime.now()
        
        for migration in pending_migrations:
            if await self.apply_migration(migration):
                applied_count += 1
            else:
                break
        
        total_time = int((datetime.now() - total_start_time).total_seconds() * 1000)
        
        logging.info(f"Applied {applied_count}/{len(pending_migrations)} pending migrations in {total_time}ms")
        
        return {
            "applied": applied_count,
            "total": len(pending_migrations),
            "status": "completed" if applied_count == len(pending_migrations) else "partial",
            "execution_time_ms": total_time
        }
    
    async def get_migration_status(self) -> Dict[str, Any]:
        """Получает статус миграций"""
        await self.init_migration_table()
        applied_migrations = await self.get_applied_migrations()
        
        total_migrations = len(self.migrations)
        applied_count = len(applied_migrations)
        pending_count = total_migrations - applied_count
        
        return {
            "total_migrations": total_migrations,
            "applied_migrations": applied_count,
            "pending_migrations": pending_count,
            "applied_versions": applied_migrations,
            "is_up_to_date": pending_count == 0
        }


# Глобальный экземпляр менеджера миграций
migration_manager = MigrationManager()
