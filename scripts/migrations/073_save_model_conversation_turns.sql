-- Preserve the application's Gemini role and stable turn order when exporting
-- an active chat. Keep the original service-message filters and invoker rights.
CREATE OR REPLACE PROCEDURE public.save_chat_to_conversation(
    p_user_id BIGINT,
    p_conv_id INTEGER
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.conversation_messages (conversation_id, role, content, owner_user_id)
    SELECT p_conv_id, source.role, source.content, p_user_id
    FROM public.active_chat_messages AS source
    JOIN public.conversations AS target
      ON target.id = p_conv_id AND target.user_id = p_user_id
    WHERE source.user_id = p_user_id
      AND source.role IN ('user', 'assistant', 'model')
      AND source.content NOT ILIKE '/%'
      AND source.content NOT ILIKE '%🖼️ обрабатываю изображение%'
      AND source.content NOT ILIKE '%🤔 думаю%'
      AND source.content NOT ILIKE '%📄 обрабатываю документ%'
      AND source.content NOT ILIKE '%✅ новый чат создан%'
      AND source.content NOT ILIKE '%опишите, какую роль хотите создать%'
      AND source.content NOT ILIKE '%не удалось сгенерировать роль%'
      AND source.content NOT ILIKE '%сервер перегружен%'
    ORDER BY source.id;
END;
$$;
