# Развёртывание на новом сервере

Пошаговая инструкция, чтобы поднять сервис из этого репозитория с нуля.
Проверено на Ubuntu 24.04, Docker 29, Compose 2.40, Caddy 2.6.

## 1. Что нужно установить на сервере

```bash
# Docker Engine + Compose plugin
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # перелогиниться после этого

# Caddy (обратный прокси с автоматическим TLS)
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

# Git
sudo apt install -y git
```

## 2. Код и настройки

```bash
git clone https://github.com/gptchatseo1-blip/report.git
cd report
cp .env.example .env
```

В `.env` обязательно заполнить:

| Переменная | Зачем |
|---|---|
| `DJANGO_SECRET_KEY` | стойкий случайный ключ, иначе приложение не стартует в проде |
| `DJANGO_ALLOWED_HOSTS` | домен сервиса, например `report.example.com` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://report.example.com` |
| `POSTGRES_PASSWORD` | пароль базы |
| `CREDENTIAL_ENCRYPTION_KEY` | ключ Fernet для токенов OAuth и API-ключей Topvisor |
| `DJANGO_SECURE_SSL_REDIRECT=1`, `DJANGO_TRUST_X_FORWARDED_PROTO=1` | когда TLS терминирует Caddy |

`CREDENTIAL_ENCRYPTION_KEY` генерируется один раз и **должен сохраняться навсегда**:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Потеря или замена этого ключа делает сохранённые токены и ключи нечитаемыми —
их придётся выдавать заново. Храните его вне сервера (secret-хранилище, менеджер паролей).

## 3. Запуск приложения

```bash
docker compose -f compose.yaml -f deploy/compose.prod.yaml up -d --build
docker compose -f compose.yaml -f deploy/compose.prod.yaml exec web python manage.py createsuperuser
curl --fail http://127.0.0.1:8000/health/    # {"status": "ok"}
```

Слой `deploy/compose.prod.yaml` делает три вещи: публикует порт только на
`127.0.0.1`, задаёт `restart: unless-stopped` и фиксирует имя образа
`report-web:latest` (чтобы обновление не тянуло образ из реестра).

Миграции применяются автоматически при старте контейнера `web`, статика
собирается там же (`collectstatic`) и раздаётся WhiteNoise из приложения.

## 4. Caddy

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo editor /etc/caddy/Caddyfile      # заменить report.example.com на реальный домен
sudo systemctl reload caddy
sudo systemctl status caddy
curl -fsSI https://report.example.com/health/
```

DNS-запись домена должна уже указывать на сервер: Caddy сам получит сертификат.
Статику проксируем в приложение, отдельный `file_server` для `/static/`
добавлять нельзя — иначе после обновления браузер получит старые CSS/JS.

## 5. Обновление до последней версии

```bash
cd report
git pull --ff-only
docker compose -f compose.yaml -f deploy/compose.prod.yaml up -d --build
curl --fail http://127.0.0.1:8000/health/
```

## 6. Данные живут вне git

В репозитории только код. На сервере отдельно хранятся:

- том `postgres_data` — база (проекты, снимки источников, версии отчётов);
- том `media_data` — сгенерированные DOCX/PDF/XLSX;
- файл `.env` — секреты.

Резервная копия и восстановление:

```bash
# база
docker compose exec -T db pg_dump -U report report | gzip > "backup-$(date +%F).sql.gz"
gunzip -c backup-2026-11-16.sql.gz | docker compose exec -T db psql -U report report

# файлы отчётов
docker run --rm -v report_media_data:/media -v "$PWD":/out alpine \
  tar czf /out/media-$(date +%F).tar.gz -C /media .
docker run --rm -v report_media_data:/media -v "$PWD":/in alpine \
  tar xzf /in/media-2026-11-16.tar.gz -C /media
```

Копии нужно хранить **вне сервера** (S3, второй сервер, внешний диск): бэкап на
том же диске не спасает при потере машины.

## 7. Автозапуск после перезагрузки

- контейнеры: `restart: unless-stopped` в `deploy/compose.prod.yaml`;
- Docker: `sudo systemctl enable --now docker`;
- Caddy: `sudo systemctl enable --now caddy`.

## 8. Частые ошибки

| Симптом | Причина |
|---|---|
| `DisallowedHost` | домен не добавлен в `DJANGO_ALLOWED_HOSTS` |
| `CSRF verification failed` | домен не добавлен в `DJANGO_CSRF_TRUSTED_ORIGINS` |
| пустые токены, «нужно повторно авторизовать» | сменился `CREDENTIAL_ENCRYPTION_KEY` |
| старые стили после обновления | перед приложением настроен `file_server` для `/static/` |
| `502` от Caddy | контейнер `web` не поднялся: `docker compose logs web` |
