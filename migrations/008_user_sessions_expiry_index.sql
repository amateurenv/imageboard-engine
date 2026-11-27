-- ускоряем штатную очистку истёкших staff-сессий
CREATE INDEX user_sessions_expires_at_idx ON user_sessions (expires_at);
