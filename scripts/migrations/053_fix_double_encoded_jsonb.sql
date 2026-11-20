
UPDATE user_state
SET tarot_session = (tarot_session #>> '{}')::jsonb
WHERE tarot_session IS NOT NULL
  AND jsonb_typeof(tarot_session) = 'string';

UPDATE user_state
SET generated_role = (generated_role #>> '{}')::jsonb
WHERE generated_role IS NOT NULL
  AND jsonb_typeof(generated_role) = 'string';

UPDATE user_state
SET role_diaries = (role_diaries #>> '{}')::jsonb
WHERE role_diaries IS NOT NULL
  AND jsonb_typeof(role_diaries) = 'string';
