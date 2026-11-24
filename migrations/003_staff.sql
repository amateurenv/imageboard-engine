-- сотрудники проекта: владельцы, админы, модераторы
CREATE TABLE users (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    login text NOT NULL,
    password_hash text NOT NULL,
    role text NOT NULL DEFAULT 'owner',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT users_login_format CHECK (login ~ '^[a-z0-9_]{3,30}$'),
    CONSTRAINT users_role_check CHECK (role IN ('owner', 'admin', 'moderator'))
);
CREATE UNIQUE INDEX users_login_unique_idx ON users (login);


-- сессии: токен + ссылка на пользователя + дата истечения
CREATE TABLE user_sessions (
    token_hash text PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX user_sessions_user_idx ON user_sessions (user_id);


-- журнал действий модерации
CREATE TABLE moderation_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_id bigint NULL REFERENCES users (id) ON DELETE SET NULL,
    action text NOT NULL,
    target_type text NOT NULL,
    target_id bigint NOT NULL,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX moderation_log_actor_idx ON moderation_log (actor_id);
CREATE INDEX moderation_log_target_idx ON moderation_log (target_type, target_id);


-- IP-баны: адреса, которым запрещено публиковать треды и посты
CREATE TABLE ip_bans (
    ip text PRIMARY KEY,
    reason text,
    actor_id bigint NULL REFERENCES users (id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
