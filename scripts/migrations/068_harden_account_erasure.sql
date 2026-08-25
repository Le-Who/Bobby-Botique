-- Complete GDPR account erasure by making every user-owned legacy table
-- participate in the users(user_id) ownership graph. Shared group/graph
-- attribution is retained but anonymised or transferred explicitly by the
-- application before a user row is removed.

DELETE FROM public.natal_reports AS report
WHERE NOT EXISTS (
    SELECT 1 FROM public.users AS app_user WHERE app_user.user_id = report.user_id
);

DELETE FROM public.daily_trivia_prompt_messages AS prompt
WHERE NOT EXISTS (
    SELECT 1 FROM public.users AS app_user WHERE app_user.user_id = prompt.user_id
);

DELETE FROM public.daily_trivia_super_results AS result
WHERE NOT EXISTS (
    SELECT 1 FROM public.users AS app_user WHERE app_user.user_id = result.user_id
);

DELETE FROM public.inline_boards AS board
WHERE NOT EXISTS (
    SELECT 1 FROM public.users AS app_user WHERE app_user.user_id = board.creator_id
);

UPDATE public.memory_nodes AS node
SET actor_user_id = NULL
WHERE actor_user_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM public.users AS app_user WHERE app_user.user_id = node.actor_user_id
  );

UPDATE public.memory_edges AS edge
SET actor_user_id = NULL
WHERE actor_user_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM public.users AS app_user WHERE app_user.user_id = edge.actor_user_id
  );

WITH replacements AS (
    SELECT group_chat.chat_id,
           (
               SELECT member.user_id
               FROM public.group_members AS member
               JOIN public.users AS candidate ON candidate.user_id = member.user_id
               WHERE member.chat_id = group_chat.chat_id
                 AND candidate.is_authorized = 1
               ORDER BY member.is_admin DESC,
                        member.joined_at ASC NULLS LAST,
                        member.user_id ASC
               LIMIT 1
           ) AS replacement_user_id
    FROM public.group_chats AS group_chat
    WHERE NOT EXISTS (
        SELECT 1 FROM public.users AS app_user
        WHERE app_user.user_id = group_chat.admin_user_id
    )
), transferred AS (
    UPDATE public.group_chats AS group_chat
    SET admin_user_id = replacement.replacement_user_id
    FROM replacements AS replacement
    WHERE group_chat.chat_id = replacement.chat_id
      AND replacement.replacement_user_id IS NOT NULL
    RETURNING group_chat.chat_id, group_chat.admin_user_id
)
UPDATE public.group_members AS member
SET is_admin = TRUE
FROM transferred
WHERE member.chat_id = transferred.chat_id
  AND member.user_id = transferred.admin_user_id;

DELETE FROM public.group_chats AS group_chat
WHERE NOT EXISTS (
    SELECT 1 FROM public.users AS app_user
    WHERE app_user.user_id = group_chat.admin_user_id
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'natal_reports_user_fk'
          AND conrelid = 'public.natal_reports'::regclass
    ) THEN
        ALTER TABLE public.natal_reports
            ADD CONSTRAINT natal_reports_user_fk
            FOREIGN KEY (user_id) REFERENCES public.users(user_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'daily_trivia_prompt_messages_user_fk'
          AND conrelid = 'public.daily_trivia_prompt_messages'::regclass
    ) THEN
        ALTER TABLE public.daily_trivia_prompt_messages
            ADD CONSTRAINT daily_trivia_prompt_messages_user_fk
            FOREIGN KEY (user_id) REFERENCES public.users(user_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'daily_trivia_super_results_user_fk'
          AND conrelid = 'public.daily_trivia_super_results'::regclass
    ) THEN
        ALTER TABLE public.daily_trivia_super_results
            ADD CONSTRAINT daily_trivia_super_results_user_fk
            FOREIGN KEY (user_id) REFERENCES public.users(user_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'inline_boards_creator_fk'
          AND conrelid = 'public.inline_boards'::regclass
    ) THEN
        ALTER TABLE public.inline_boards
            ADD CONSTRAINT inline_boards_creator_fk
            FOREIGN KEY (creator_id) REFERENCES public.users(user_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'group_chats_admin_user_fk'
          AND conrelid = 'public.group_chats'::regclass
    ) THEN
        ALTER TABLE public.group_chats
            ADD CONSTRAINT group_chats_admin_user_fk
            FOREIGN KEY (admin_user_id) REFERENCES public.users(user_id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'memory_nodes_actor_user_fk'
          AND conrelid = 'public.memory_nodes'::regclass
    ) THEN
        ALTER TABLE public.memory_nodes
            ADD CONSTRAINT memory_nodes_actor_user_fk
            FOREIGN KEY (actor_user_id) REFERENCES public.users(user_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'memory_edges_actor_user_fk'
          AND conrelid = 'public.memory_edges'::regclass
    ) THEN
        ALTER TABLE public.memory_edges
            ADD CONSTRAINT memory_edges_actor_user_fk
            FOREIGN KEY (actor_user_id) REFERENCES public.users(user_id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_inline_boards_creator_fk
    ON public.inline_boards (creator_id);
CREATE INDEX IF NOT EXISTS idx_group_chats_admin_user_fk
    ON public.group_chats (admin_user_id);
CREATE INDEX IF NOT EXISTS idx_memory_nodes_actor_user_fk
    ON public.memory_nodes (actor_user_id) WHERE actor_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_edges_actor_user_fk
    ON public.memory_edges (actor_user_id) WHERE actor_user_id IS NOT NULL;
