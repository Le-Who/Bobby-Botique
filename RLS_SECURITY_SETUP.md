# 🔒 Настройка Row Level Security (RLS) для Supabase

## 🚨 Критическая проблема безопасности

**RLS отключен в публичных таблицах** - это серьезная уязвимость безопасности, которая позволяет:

- ❌ **Читать данные других пользователей** (чаты, документы, личные сообщения)
- ❌ **Изменять статус авторизации** любого пользователя
- ❌ **Получить доступ к API ключам** Gemini и Tavily
- ❌ **Просматривать содержимое документов** других пользователей

## ✅ Решение: Включение RLS

### 1. Автоматическая настройка

Код уже обновлен для автоматического включения RLS при инициализации базы данных:

```python
# В app/database.py добавлены функции:
- setup_row_level_security()
- create_rls_policies()
- set_user_context()
- clear_user_context()
```

### 2. Политики безопасности

#### **Таблица `users`**
```sql
-- Пользователи могут читать только свои данные
CREATE POLICY users_select_policy ON users
FOR SELECT USING (user_id = current_setting('app.user_id', true)::bigint OR 
                current_setting('app.is_admin', true)::boolean = true);

-- Только админы могут изменять данные пользователей
CREATE POLICY users_modify_policy ON users
FOR ALL USING (current_setting('app.is_admin', true)::boolean = true);
```

#### **Таблица `chats`**
```sql
-- Пользователи могут читать/изменять только свои чаты
CREATE POLICY chats_policy ON chats
FOR ALL USING (user_id = current_setting('app.user_id', true)::bigint OR 
             current_setting('app.is_admin', true)::boolean = true);
```

#### **Таблица `user_documents`**
```sql
-- Пользователи могут читать/изменять только свои документы
CREATE POLICY user_documents_policy ON user_documents
FOR ALL USING (user_id = current_setting('app.user_id', true)::bigint OR 
             current_setting('app.is_admin', true)::boolean = true);
```

#### **Таблицы API ключей**
```sql
-- Только админы могут работать с API ключами
CREATE POLICY api_keys_policy ON api_keys
FOR ALL USING (current_setting('app.is_admin', true)::boolean = true);
```

### 3. Контекст пользователя

Все функции работы с базой данных теперь устанавливают контекст пользователя:

```python
# Установка контекста
await set_user_context(user_id, is_admin(user_id))

try:
    # Выполнение запросов с RLS
    result = await db_query("SELECT * FROM chats WHERE user_id = $1", (user_id,))
finally:
    # Очистка контекста
    await clear_user_context()
```

## 🔧 Ручная настройка (если автоматическая не сработала)

### 1. Включить RLS для всех таблиц

```sql
-- Включить RLS для всех таблиц
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE chats ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE key_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE tavily_api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE tavily_key_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE group_chats ENABLE ROW LEVEL SECURITY;
ALTER TABLE group_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE group_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE error_logs ENABLE ROW LEVEL SECURITY;
```

### 2. Создать политики безопасности

```sql
-- Политики для таблицы users
CREATE POLICY users_select_policy ON users
FOR SELECT USING (user_id = current_setting('app.user_id', true)::bigint OR 
                current_setting('app.is_admin', true)::boolean = true);

CREATE POLICY users_modify_policy ON users
FOR ALL USING (current_setting('app.is_admin', true)::boolean = true);

-- Политики для таблицы chats
CREATE POLICY chats_policy ON chats
FOR ALL USING (user_id = current_setting('app.user_id', true)::bigint OR 
             current_setting('app.is_admin', true)::boolean = true);

-- Политики для таблицы user_documents
CREATE POLICY user_documents_policy ON user_documents
FOR ALL USING (user_id = current_setting('app.user_id', true)::bigint OR 
             current_setting('app.is_admin', true)::boolean = true);

-- Политики для API ключей
CREATE POLICY api_keys_policy ON api_keys
FOR ALL USING (current_setting('app.is_admin', true)::boolean = true);

CREATE POLICY tavily_api_keys_policy ON tavily_api_keys
FOR ALL USING (current_setting('app.is_admin', true)::boolean = true);

-- Политики для статистики использования
CREATE POLICY key_usage_policy ON key_usage
FOR ALL USING (current_setting('app.is_admin', true)::boolean = true);

CREATE POLICY tavily_key_usage_policy ON tavily_key_usage
FOR ALL USING (current_setting('app.is_admin', true)::boolean = true);

-- Политики для групповых чатов
CREATE POLICY group_chats_policy ON group_chats
FOR ALL USING (current_setting('app.is_admin', true)::boolean = true OR
             EXISTS (SELECT 1 FROM group_members gm 
                    WHERE gm.chat_id = group_chats.chat_id 
                    AND gm.user_id = current_setting('app.user_id', true)::bigint));

CREATE POLICY group_members_policy ON group_members
FOR ALL USING (current_setting('app.is_admin', true)::boolean = true OR
             EXISTS (SELECT 1 FROM group_members gm 
                    WHERE gm.chat_id = group_members.chat_id 
                    AND gm.user_id = current_setting('app.user_id', true)::bigint));

CREATE POLICY group_messages_policy ON group_messages
FOR ALL USING (current_setting('app.is_admin', true)::boolean = true OR
             EXISTS (SELECT 1 FROM group_members gm 
                    WHERE gm.chat_id = group_messages.chat_id 
                    AND gm.user_id = current_setting('app.user_id', true)::bigint));

-- Политики для метрик и логов
CREATE POLICY metrics_policy ON metrics
FOR ALL USING (current_setting('app.is_admin', true)::boolean = true);

CREATE POLICY error_logs_policy ON error_logs
FOR ALL USING (current_setting('app.is_admin', true)::boolean = true);
```

## 🧪 Тестирование RLS

### 1. Проверка включения RLS

```sql
-- Проверить, включен ли RLS для таблиц
SELECT schemaname, tablename, rowsecurity 
FROM pg_tables 
WHERE schemaname = 'public' 
AND tablename IN ('users', 'chats', 'user_documents', 'api_keys');
```

### 2. Проверка политик

```sql
-- Проверить созданные политики
SELECT schemaname, tablename, policyname, permissive, roles, cmd, qual 
FROM pg_policies 
WHERE schemaname = 'public';
```

### 3. Тестирование изоляции данных

```sql
-- Установить контекст обычного пользователя
SELECT set_config('app.user_id', '123', false);
SELECT set_config('app.is_admin', 'false', false);

-- Попытаться получить данные другого пользователя (должно вернуть пустой результат)
SELECT * FROM chats WHERE user_id = 456;

-- Очистить контекст
SELECT set_config('app.user_id', '', false);
SELECT set_config('app.is_admin', 'false', false);
```

## ⚠️ Важные замечания

### 1. Производительность
- RLS добавляет небольшие накладные расходы на проверку политик
- Для высоконагруженных систем рекомендуется мониторинг производительности

### 2. Отладка
- При проблемах с доступом проверьте контекст пользователя
- Логируйте ошибки доступа для диагностики

### 3. Миграция
- RLS включается автоматически при запуске приложения
- Существующие данные остаются доступными согласно политикам

## 🔍 Мониторинг безопасности

### 1. Логирование доступа

```python
# В функции set_user_context добавлено логирование
logging.info(f"Setting user context: user_id={user_id}, is_admin={is_admin}")
```

### 2. Проверка политик

```sql
-- Регулярно проверяйте активные политики
SELECT tablename, policyname, cmd, qual 
FROM pg_policies 
WHERE schemaname = 'public' 
ORDER BY tablename, policyname;
```

### 3. Аудит доступа

```sql
-- Включить аудит доступа к таблицам (если необходимо)
CREATE EXTENSION IF NOT EXISTS pgaudit;
```

## ✅ Результат

После включения RLS:

- ✅ **Данные пользователей изолированы** - каждый видит только свои данные
- ✅ **API ключи защищены** - доступ только у администраторов
- ✅ **Документы приватны** - пользователи видят только свои файлы
- ✅ **История чатов защищена** - конфиденциальные сообщения недоступны другим
- ✅ **Административные функции** - только для авторизованных админов

**RLS критически важен для безопасности вашего бота!** Рекомендуется включить его немедленно.
