"""простая страница для локального управления api"""

# ruff: noqa: E501 - строка html и javascript важнее искусственного переноса в python

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["admin-ui"])


@router.get("/admin", include_in_schema=False, response_class=HTMLResponse)
async def admin_page() -> HTMLResponse:
    """отдать статичную страницу управления"""

    # здесь нет пользовательских данных - браузер получает один и тот же безопасный файл
    return HTMLResponse(ADMIN_PAGE)


ADMIN_PAGE = """<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Imageboard - управление</title>
<style>
body { max-width: 760px; margin: 2rem auto; padding: 0 1rem; font: 16px system-ui; }
section { border: 1px solid #ccc; border-radius: 8px; padding: 1rem; margin: 1rem 0; }
input, button, textarea { font: inherit; margin: .25rem 0; padding: .45rem; }
textarea { box-sizing: border-box; min-height: 14rem; width: 100%; }
#status { min-height: 1.5rem; } .error { color: #a00; } .ok { color: #075; }
.row { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; }
.row label { display: grid; gap: .15rem; } .danger { color: #a00; }
</style>
<body>
<h1>Управление</h1>
<p id="status">Войдите, чтобы продолжить</p>
<section id="login-box">
<h2>Вход</h2>
<form id="login-form"><input name="login" placeholder="Логин" required>
<input name="password" type="password" placeholder="Пароль" required>
<button>Войти</button></form>
</section>
<section id="admin-box" hidden>
<p>Вы вошли как <strong id="user"></strong>. <button id="logout">Выйти</button></p>
<section><h2>Доски</h2><button id="load-boards">Обновить список</button><pre id="boards"></pre>
<form id="board-form" hidden>
<p>Создание и изменение доступны только owner и admin</p>
<div class="row"><label>Slug<input id="board-slug" pattern="[a-z0-9][a-z0-9_-]*" required></label>
<label>Название<input id="board-title" required></label>
<label>Позиция<input id="board-position" type="number" value="0"></label></div>
<label>Описание<input id="board-description"></label>
<div class="row"><label><input id="board-active" type="checkbox" checked> Активна</label>
<label><input id="board-nsfw" type="checkbox"> 18+</label></div>
<details><summary>Лимиты медиа для этой доски</summary>
<p>Пустое поле берёт общий лимит сайта. Единая policy: 10 вложений, до 20 MiB каждое, до 200 MiB всего.</p>
<div class="row"><label>Вложений<input id="media-count" type="number" min="1" max="20" placeholder="Общий"></label>
<label>Одно вложение, MiB<input id="media-attachment-size" type="number" min="1" max="100" placeholder="Общий"></label>
<label>Всего, MiB<input id="media-total-size" type="number" min="1" max="500" placeholder="Общий"></label></div>
<label>Анонимные вложения<select id="media-anonymous"><option value="">Как на сайте</option><option value="true">Разрешить</option><option value="false">Запретить</option></select></label>
<small>Лимит выше Telegram-safe значения может помешать синхронизации с ботом.</small></details>
<button id="create-board" type="button">Создать доску</button>
<button id="update-board" type="button">Сохранить изменения</button>
<button id="delete-board" class="danger" type="button">Удалить доску</button></form></section>
<section id="settings-section"><h2>Настройки сайта</h2><button id="load-settings">Загрузить</button>
<form id="settings-form"><textarea id="settings" spellcheck="false"></textarea>
<button>Сохранить настройки</button></form>
</section></section>
<script>
const api = '/api/v1';
const status = document.querySelector('#status');
function csrf() { return document.cookie.split('; ').find(x => x.startsWith('csrf_token='))?.split('=')[1]; }
function show(text, error = false) { status.textContent = text; status.className = error ? 'error' : 'ok'; }
async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', csrf() || '');
  const response = await fetch(api + path, {...options, headers, credentials: 'same-origin'});
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `Ошибка ${response.status}`);
  return response.status === 204 ? null : response.json();
}
async function loadBoards() { document.querySelector('#boards').textContent = JSON.stringify(await request('/admin/boards'), null, 2); }
async function loadSettings() { document.querySelector('#settings').value = JSON.stringify(await request('/admin/settings'), null, 2); }
async function start() {
  const user = await request('/auth/me').catch(() => null);
  if (!user) return;
  document.querySelector('#login-box').hidden = true; document.querySelector('#admin-box').hidden = false;
  document.querySelector('#user').textContent = `${user.login} (${user.role})`; show('Готово');
  const canManageBoards = user.role === 'owner' || user.role === 'admin';
  document.querySelector('#board-form').hidden = !canManageBoards;
  document.querySelector('#settings-section').hidden = user.role !== 'owner';
  await loadBoards();
  if (user.role === 'owner') await loadSettings();
}
function boardPayload() {
  const numberOrNull = id => { const value = document.querySelector(id).value; return value === '' ? null : Number(value); };
  const mediaOverrides = {max_attachments_per_post: numberOrNull('#media-count'), max_attachment_size_mib: numberOrNull('#media-attachment-size'), max_total_size_mib: numberOrNull('#media-total-size')};
  const anonymous = document.querySelector('#media-anonymous').value;
  if (anonymous !== '') mediaOverrides.allow_anonymous_uploads = anonymous === 'true';
  return {slug: document.querySelector('#board-slug').value.trim(), title: document.querySelector('#board-title').value.trim(),
    description: document.querySelector('#board-description').value.trim(), position: Number(document.querySelector('#board-position').value),
    is_active: document.querySelector('#board-active').checked, is_nsfw: document.querySelector('#board-nsfw').checked,
    media_overrides: mediaOverrides};
}
async function boardAction(method, suffix = '') {
  const board = boardPayload(); if (!board.slug) throw new Error('Укажите slug доски');
  const options = {method}; if (method !== 'DELETE') { options.headers = {'Content-Type': 'application/json'}; options.body = JSON.stringify(board); }
  const path = method === 'POST' ? '/admin/boards' : '/admin/boards/' + encodeURIComponent(board.slug) + suffix;
  await request(path, options); await loadBoards();
}
document.querySelector('#login-form').addEventListener('submit', async event => {
  event.preventDefault(); const data = Object.fromEntries(new FormData(event.target));
  try { await request('/auth/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)}); await start(); }
  catch (error) { show(error.message, true); }
});
document.querySelector('#load-boards').onclick = () => loadBoards().catch(error => show(error.message, true));
document.querySelector('#load-settings').onclick = () => loadSettings().catch(error => show(error.message, true));
document.querySelector('#create-board').onclick = () => boardAction('POST', '').catch(error => show(error.message, true));
document.querySelector('#update-board').onclick = () => boardAction('PATCH').catch(error => show(error.message, true));
document.querySelector('#delete-board').onclick = async () => { if (confirm('Удалить доску со всеми тредами?')) { try { await boardAction('DELETE'); } catch (error) { show(error.message, true); } } };
document.querySelector('#settings-form').addEventListener('submit', async event => {
  event.preventDefault(); try { await request('/admin/settings', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: document.querySelector('#settings').value}); show('Настройки сохранены'); }
  catch (error) { show(error.message, true); }
});
document.querySelector('#logout').onclick = async () => { try { await request('/auth/logout', {method: 'POST'}); location.reload(); } catch (error) { show(error.message, true); } };
start();
</script>
</body></html>"""
