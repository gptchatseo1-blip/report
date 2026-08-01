# SEO Report — первый технический срез

Каркас внутреннего сервиса на Django 5.2: PostgreSQL, административный интерфейс,
проекты, брендовые правила и приоритетные URL-группы. Импорт, источники данных,
расчёты и экспорт намеренно не входят в этот срез.

## Запуск

```bash
cp .env.example .env
# Замените DJANGO_SECRET_KEY и пароли в .env
docker compose up --build -d
docker compose exec web python manage.py createsuperuser
```

Интерфейс администратора: <http://localhost:8000/admin/>. Проверка состояния:
<http://localhost:8000/health/> (`{"status": "ok"}`). При старте web-контейнера
миграции применяются автоматически.

## Команды разработки

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py makemigrations --check --dry-run
docker compose exec web pytest
docker compose exec web ruff check .
docker compose exec web python manage.py check
docker compose ps
curl --fail http://localhost:8000/health/
```

Локально можно установить зависимости командой `pip install -e '.[dev]'`.
При отсутствии переменной `POSTGRES_HOST` тесты используют SQLite; контейнеры
всегда используют PostgreSQL из `.env`.

## Принятые решения

- Домен приводится к нижнему регистру, без `www`, порта, пути и завершающей точки;
  международные домены сохраняются в IDNA. Нормализованное значение уникально.
- Брендовые строковые и regex-правила сопоставляются без учёта регистра, а
  уникальность шаблонов также регистронезависима.
- Если URL соответствует нескольким активным группам, выбирается группа с большим
  приоритетом; результат классификации содержит весь список совпадений для диагностики.
- Regex проверяются на синтаксис, длину, обратные ссылки и распространённые вложенные
  повторения, способные вызвать катастрофический backtracking.
- Управление реализовано стандартным защищённым Django Admin. Для однопользовательского
  режима создаётся один superuser командой `createsuperuser`.

## Шрифты и документы

Docker-образ содержит LibreOffice Writer в headless-режиме и метрически совместимый
с Calibri шрифт Carlito, включая кириллицу. Клиентский `reference_reports.zip` и все
DOCX исключены из build context через `.dockerignore`.
