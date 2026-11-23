-- настройки конкретного сайта
CREATE TABLE site_settings (
    -- ключ группы настроек, например posting или appearance
    key text PRIMARY KEY,

    -- json объект со значениями этой группы
    value jsonb NOT NULL,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    -- у каждой группы настроек должен быть json объект
    CONSTRAINT site_settings_value_object
        CHECK (jsonb_typeof(value) = 'object')
);


-- доски вроде /b/, /tech/ или /news/
CREATE TABLE boards (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- короткое имя доски для url
    slug text NOT NULL,

    -- название и описание для интерфейса
    title text NOT NULL,
    description text NOT NULL DEFAULT '',

    -- порядок досок в списке
    position integer NOT NULL DEFAULT 0,

    -- доску можно скрыть без удаления данных
    is_active boolean NOT NULL DEFAULT true,

    -- отметка 18+
    is_nsfw boolean NOT NULL DEFAULT false,

    created_at timestamptz NOT NULL DEFAULT now(),

    -- slug только из маленьких латинских букв, цифр, _ и -
    CONSTRAINT boards_slug_format
        CHECK (slug ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),

    -- аварийные потолки, реальные лимиты задаются в site_settings
    CONSTRAINT boards_title_length
        CHECK (char_length(title) <= 1000),
    CONSTRAINT boards_description_length
        CHECK (char_length(description) <= 10000)
);

-- две доски не могут иметь одинаковый slug
CREATE UNIQUE INDEX boards_slug_unique_idx ON boards (slug);


-- треды внутри досок
CREATE TABLE threads (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    board_id bigint NOT NULL REFERENCES boards (id) ON DELETE RESTRICT,

    -- заголовок может быть пустым
    subject text NOT NULL DEFAULT '',

    -- закреплённый тред всегда выше обычных
    is_sticky boolean NOT NULL DEFAULT false,

    -- в закрытый тред нельзя будет писать новые посты
    is_locked boolean NOT NULL DEFAULT false,

    -- архивный тред не поднимается новыми ответами
    is_archived boolean NOT NULL DEFAULT false,

    created_at timestamptz NOT NULL DEFAULT now(),

    -- по этому времени треды будут сортироваться на доске
    bumped_at timestamptz NOT NULL DEFAULT now(),

    -- аварийный потолок, реальный лимит задаётся в site_settings
    CONSTRAINT threads_subject_length
        CHECK (char_length(subject) <= 1000)
);

-- быстрый список тредов одной доски
CREATE INDEX threads_board_listing_idx
    ON threads (board_id, is_archived, is_sticky DESC, bumped_at DESC, id DESC);


-- все посты идут плоским списком внутри треда
CREATE TABLE posts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    thread_id bigint NOT NULL REFERENCES threads (id) ON DELETE CASCADE,

    -- null значит анонимный автор без собственного имени
    author_name text,

    -- текст пока может быть пустым, позже добавим посты только с медиа
    body text NOT NULL DEFAULT '',

    created_at timestamptz NOT NULL DEFAULT now(),

    -- аварийные потолки, реальные лимиты задаются в site_settings
    CONSTRAINT posts_author_name_length
        CHECK (author_name IS NULL OR char_length(author_name) <= 500),
    CONSTRAINT posts_body_length
        CHECK (char_length(body) <= 100000)
);

-- быстрый вывод всех постов треда по порядку
CREATE INDEX posts_thread_listing_idx ON posts (thread_id, id);


-- ссылки вида >>123 между постами
CREATE TABLE post_references (
    source_post_id bigint NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    target_post_id bigint NOT NULL REFERENCES posts (id) ON DELETE CASCADE,

    -- один и тот же пост не может сослаться сам на себя
    CONSTRAINT post_references_not_self
        CHECK (source_post_id <> target_post_id),

    -- одна ссылка между двумя постами хранится только один раз
    PRIMARY KEY (source_post_id, target_post_id)
);

-- быстрый поиск всех ответов на конкретный пост
CREATE INDEX post_references_target_idx ON post_references (target_post_id);