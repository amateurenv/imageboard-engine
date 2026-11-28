-- очередь хранит только области обновления и переживает сбой redis
CREATE TABLE realtime_outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_id bigint NOT NULL CHECK (board_id > 0),
    thread_id bigint CHECK (thread_id > 0),
    unavailable boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- отзыв подписок на доску и её треды после потери публичного доступа
CREATE FUNCTION queue_realtime_board_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.is_active THEN
            INSERT INTO realtime_outbox (board_id, unavailable) VALUES (OLD.id, true);
        END IF;
    ELSIF OLD.is_active AND NOT NEW.is_active THEN
        INSERT INTO realtime_outbox (board_id, unavailable) VALUES (OLD.id, true);
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER realtime_board_change
AFTER UPDATE OF is_active OR DELETE ON boards
FOR EACH ROW EXECUTE FUNCTION queue_realtime_board_change();

-- изменения видимого треда обновляют каталог и открытую страницу треда
CREATE FUNCTION queue_realtime_thread_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    affected_board bigint;
    affected_thread bigint;
    unavailable boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.is_hidden THEN
            RETURN NULL;
        END IF;
        affected_board := OLD.board_id;
        affected_thread := OLD.id;
        unavailable := true;
    ELSE
        IF TG_OP = 'INSERT' THEN
            IF NEW.is_hidden THEN
                RETURN NULL;
            END IF;
        ELSIF (OLD.is_hidden AND NEW.is_hidden) OR
              (OLD.subject, OLD.is_sticky, OLD.is_locked, OLD.is_archived, OLD.is_hidden)
              IS NOT DISTINCT FROM
              (NEW.subject, NEW.is_sticky, NEW.is_locked, NEW.is_archived, NEW.is_hidden) THEN
            RETURN NULL;
        END IF;
        affected_board := NEW.board_id;
        affected_thread := NEW.id;
        unavailable := NEW.is_hidden;
    END IF;

    INSERT INTO realtime_outbox (board_id, thread_id, unavailable)
    SELECT id, affected_thread, unavailable FROM boards WHERE id = affected_board AND is_active;
    RETURN NULL;
END;
$$;

CREATE TRIGGER realtime_thread_change
AFTER INSERT OR UPDATE OF subject, is_sticky, is_locked, is_archived, is_hidden OR DELETE ON threads
FOR EACH ROW EXECUTE FUNCTION queue_realtime_thread_change();

-- первый пост уже покрыт событием создания треда, каскадное удаление - событием треда
CREATE FUNCTION queue_realtime_post_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    affected_thread bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.is_hidden THEN
            RETURN NULL;
        END IF;
        affected_thread := OLD.thread_id;
    ELSE
        IF TG_OP = 'INSERT' THEN
            IF NEW.is_hidden OR NOT EXISTS (
                SELECT 1 FROM posts WHERE thread_id = NEW.thread_id AND id < NEW.id
            ) THEN
                RETURN NULL;
            END IF;
        ELSIF OLD.is_hidden = NEW.is_hidden THEN
            RETURN NULL;
        END IF;
        affected_thread := NEW.thread_id;
    END IF;

    INSERT INTO realtime_outbox (board_id, thread_id)
    SELECT t.board_id, t.id
    FROM threads AS t JOIN boards AS b ON b.id = t.board_id
    WHERE t.id = affected_thread AND NOT t.is_hidden AND b.is_active;
    RETURN NULL;
END;
$$;

CREATE TRIGGER realtime_post_change
AFTER INSERT OR UPDATE OF is_hidden OR DELETE ON posts
FOR EACH ROW EXECUTE FUNCTION queue_realtime_post_change();
