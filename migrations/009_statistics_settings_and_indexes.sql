-- окно активности можно менять без новой миграции
INSERT INTO site_settings (key, value)
VALUES ('statistics', '{"activity_window_hours": 24}'::jsonb)
ON CONFLICT (key) DO NOTHING;

-- ускоряет выборку свежих постов для счётчиков доски
CREATE INDEX posts_created_at_idx ON posts (created_at);
