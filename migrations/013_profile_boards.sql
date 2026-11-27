-- обычный пользователь и staff используют одну таблицу users и одни сессии
ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users DROP CONSTRAINT users_login_format;
ALTER TABLE users ALTER COLUMN role SET DEFAULT 'user';
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('user', 'owner', 'admin', 'moderator'));
ALTER TABLE users ADD CONSTRAINT users_login_format
    CHECK (login ~ '^[a-z0-9][a-z0-9_-]{0,62}$');
CREATE UNIQUE INDEX users_login_case_insensitive_unique_idx ON users (lower(login));

-- title остаётся совместимым именем отображаемого названия доски
ALTER TABLE boards
ADD COLUMN board_type text NOT NULL DEFAULT 'public',
ADD COLUMN rules text NOT NULL DEFAULT '',
ADD COLUMN thread_creation_policy text NOT NULL DEFAULT 'anyone',
ADD COLUMN reply_policy text NOT NULL DEFAULT 'anyone',
ADD COLUMN owner_user_id bigint NULL REFERENCES users (id) ON DELETE CASCADE,
ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
ADD COLUMN next_thread_number bigint NOT NULL DEFAULT 1,
ADD CONSTRAINT boards_type_check CHECK (board_type IN ('public', 'profile')),
ADD CONSTRAINT boards_thread_creation_policy_check
    CHECK (thread_creation_policy IN ('owner_only', 'authenticated', 'anyone')),
ADD CONSTRAINT boards_reply_policy_check
    CHECK (reply_policy IN ('owner_only', 'authenticated', 'anyone')),
ADD CONSTRAINT profile_board_owner_check
    CHECK ((board_type = 'profile') = (owner_user_id IS NOT NULL));

CREATE UNIQUE INDEX boards_profile_owner_unique_idx
ON boards (owner_user_id) WHERE board_type = 'profile';

-- публичный номер треда независим от технического id и уникален внутри доски
ALTER TABLE threads ADD COLUMN thread_number bigint;
WITH numbered_threads AS (
    SELECT id, row_number() OVER (PARTITION BY board_id ORDER BY id) AS thread_number
    FROM threads
)
UPDATE threads AS target
SET thread_number = numbered_threads.thread_number
FROM numbered_threads
WHERE target.id = numbered_threads.id;
ALTER TABLE threads ALTER COLUMN thread_number SET NOT NULL;
ALTER TABLE threads ADD CONSTRAINT threads_board_number_unique UNIQUE (board_id, thread_number);

UPDATE boards AS board
SET next_thread_number = COALESCE(
    (SELECT max(thread_number) + 1 FROM threads WHERE board_id = board.id),
    1
);

-- авторство не меняет анонимные посты и позже позволит добавить posting claims
ALTER TABLE posts
ADD COLUMN author_user_id bigint NULL REFERENCES users (id) ON DELETE SET NULL,
ADD COLUMN is_hidden boolean NOT NULL DEFAULT false;
CREATE INDEX posts_author_user_idx ON posts (author_user_id) WHERE author_user_id IS NOT NULL;

-- ограничения имён применяются только к будущему созданию, старые записи не трогаются
INSERT INTO site_settings (key, value)
VALUES (
    'identifiers',
    '{"board_slug_min_length": 1, "board_slug_max_length": 16, "profile_handle_min_length": 3, "profile_handle_max_length": 32}'::jsonb
)
ON CONFLICT (key) DO NOTHING;
