-- хеш нужен только анонимам, у account-owned поста он всегда остаётся пустым
ALTER TABLE posts ADD COLUMN deletion_password_hash text NULL;

-- индекс ускоряет проверку собственного контента без раскрытия автора в api
CREATE INDEX posts_author_user_self_delete_idx
ON posts (author_user_id, id) WHERE author_user_id IS NOT NULL;
