-- настройки тредов по умолчанию
INSERT INTO site_settings (key, value)
VALUES (
    'thread',
    '{
        "max_subject_length": 200
    }'::jsonb
)
ON CONFLICT (key) DO NOTHING;


-- настройки постов по умолчанию
INSERT INTO site_settings (key, value)
VALUES (
    'post',
    '{
        "max_post_length": 10000,
        "max_author_name_length": 50
    }'::jsonb
)
ON CONFLICT (key) DO NOTHING;


-- текстовые настройки по умолчанию
INSERT INTO site_settings (key, value)
VALUES (
    'localization',
    '{
        "default_locale": "ru",
        "anonymous_name": "Аноним"
    }'::jsonb
)
ON CONFLICT (key) DO NOTHING;


-- тема по умолчанию
INSERT INTO site_settings (key, value)
VALUES (
    'appearance',
    '{
        "active_theme": "dark"
    }'::jsonb
)
ON CONFLICT (key) DO NOTHING;