# Развёртывание

## Требования

- Linux-сервер с Docker и Docker Compose
- Доступ к серверу из интернета по HTTPS — жюри и зрители голосуют с мобильного интернета
- (опционально) nginx в качестве reverse proxy

## Установка

```bash
git clone https://github.com/GostWarrir186/hackathon-jury-vote.git
cd hackathon-jury-vote
cp .env.example .env
sed -i "s/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=$(openssl rand -base64 12)/" .env
nano .env                    # укажите BASE_URL и EVENT_TITLE
bash update.sh
```

Контейнер слушает порт **8010** на хосте (8000 внутри). База данных хранится в `./data/jury.db` и переживает пересборку контейнера.

> ⚠️ `BASE_URL` должен совпадать с реальным публичным адресом — он зашивается в QR-коды.
> Если поменять его после печати карточек, карточки придётся распечатать заново.

## Работа под префиксом пути (nginx)

Приложение может работать как в корне домена, так и под префиксом, например `https://example.com/jury`.
Префикс берётся из `BASE_URL`. Добавьте в `server`-блок nginx содержимое [`deploy/nginx/jury-location.conf`](../deploy/nginx/jury-location.conf):

```nginx
location /jury/ {
    proxy_pass http://127.0.0.1:8010/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
location = /jury { return 301 /jury/; }
```

Слэш в конце `proxy_pass` обязателен — он срезает префикс `/jury` перед передачей запроса в приложение.

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## Обновление

```bash
git pull
bash update.sh
```

Скрипт `update.sh`:
1. делает бэкап базы в `backups/jury-<дата>.db`;
2. пересобирает и перезапускает контейнер;
3. проверяет, что сервис отвечает HTTP 200.

`.env`, база `data/` и QR-коды при обновлении сохраняются. Схема БД мигрирует автоматически при старте.

## Резервные копии

База — один файл SQLite. Бэкап = копирование файла:

```bash
cp data/jury.db backups/jury-manual-$(date +%Y%m%d-%H%M).db
```

Восстановление:

```bash
docker compose stop
cp backups/jury-XXXX.db data/jury.db
docker compose start
```

## Диагностика

| Симптом | Что проверить |
|---|---|
| 502 Bad Gateway | `docker ps` — запущен ли контейнер; `docker logs --tail 50 jury-vote` |
| Страницы без стилей / ссылки ведут в корень | `BASE_URL` в `.env` не совпадает с реальным адресом или префиксом |
| QR ведёт не туда | Неверный `BASE_URL` на момент печати карточек |
| Пульт не принимает пароль | Проверьте `ADMIN_USER` / `ADMIN_PASSWORD` в `.env`, затем `docker compose up -d` |

## Безопасность

- Обязательно задайте надёжный `ADMIN_PASSWORD` — пульт защищён HTTP Basic Auth.
- Используйте HTTPS, иначе пароль пульта передаётся в открытом виде.
- Токены жюри — случайные строки (`secrets.token_urlsafe`); при утечке ссылку можно перевыпустить в `/admin/setup`.
- Файл `.env` и каталог `data/` исключены из git.
