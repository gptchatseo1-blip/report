# Уточнённый план MVP-1

## Окончательный состав

MVP-1 — однопользовательский внутренний сервис для одного администратора. Он включает:

- вход через стандартную Django-аутентификацию и создание администратора командой;
- CRUD проектов: домен, явные варианты бренда, цели, поисковые комбинации и приоритетные URL-группы;
- календарный месяц отчёта;
- импорт Topvisor CSV/XLSX с предварительной проверкой и лимитом 3000 фраз;
- синтетические адаптеры данных Яндекс Метрики и Яндекс Вебмастера;
- ручной ввод выполненных работ и проектных категорий;
- нормализацию URL, запросов, позиций и provenance;
- текущий, предыдущий равный и три календарных месяца;
- диапазоны `1–3`, `4–10`, `11–20`, `21–30`, `31–50`, `51–100`, накопительные top-10/top-30;
- неизменяемый снимок явно создаваемой версии;
- детерминированные русские выводы;
- критические ошибки и предупреждения контроля качества;
- HTML/HTMX-предпросмотр с источником показателя;
- один полный профиль DOCX, обычный PDF через LibreOffice и XLSX позиций;
- повторную генерацию из сохранённого снимка без повторного импорта;
- обезличенный демонстрационный проект и автоматический тест основной цепочки.

Не входят в MVP-1: внешние API и OAuth, секреты интеграций, DRF без отдельной задачи, Redis/Celery, Workspace/Membership и роли, несколько пользователей, S3, расширенный аудит, Google Search Console, OpenAI, уведомления, расписания, мониторинг, ротация ключей и компактный профиль.

## Сокращённая структура проекта

```text
report/
├── compose.yaml
├── Dockerfile
├── pyproject.toml
├── manage.py
├── .env.example
├── README.md
├── config/
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py
│   └── wsgi.py
├── apps/
│   ├── projects/       # проект, бренд, цели и URL-группы
│   ├── imports/        # CSV/XLSX, партии и ошибки строк
│   ├── metrics/        # позиции, synthetic adapters, расчёты
│   ├── worklog/        # выполненные работы и категории
│   ├── reports/        # отчёт, версия, snapshot, narratives, QA
│   └── exports/        # DOCX, LibreOffice PDF, XLSX
├── templates/
├── static/
├── report_templates/
├── fixtures/synthetic/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
└── docs/
    ├── report-samples-analysis.md
    ├── mvp-1-plan.md
    └── architecture.md
```

Отдельные приложения соответствуют реальным границам пользовательского потока. Будущие API добавляются адаптерами перед нормализованным слоем, не меняя контракт экспорта.

## Уточнённая модель данных

Все бизнес-объекты используют UUID и временные метки. `Project` является корнем изоляции MVP-1.

### Проект и правила

- **Project**: name, domain, normalized_domain, timezone, language, search_configuration, selected_goal configuration, report settings, work categories, active flag. Уникален `normalized_domain`.
- **ProjectBrandRule**: project, kind (`literal`/`regex`), pattern, priority, active. Уникально `(project, kind, pattern)`; поиск регистронезависимый.
- **ProjectUrlGroup**: project, name, slug, priority, active. Уникально `(project, slug)`.
- **ProjectUrlRule**: group, type (`starts_with`/`contains`/`regex`), pattern, priority, active. Уникально `(group, type, pattern)`.

Категории работ в MVP-1 хранятся в конфигурации проекта или как choices формы. Отдельная сущность вводится только при подтверждённой необходимости управления справочником.

### Импорт и исходные данные

- **ImportBatch**: project, kind, original_filename, file checksum, status, row counts, uploaded_by, timestamps, error summary. Уникально `(project, kind, checksum)` для идемпотентного повтора.
- **RawDataSnapshot**: project, source (`topvisor_import`, `synthetic_metrika`, `synthetic_webmaster`, `manual`), external/resource label, period, redacted request parameters, payload or file reference, checksum, method, retrieved_at, freshness. Уникальность включает project, source, period и checksum.
- **RankingSnapshot**: project, import batch/raw snapshot, date, search engine, region, device, optional visibility, tracked count. Уникально по источнику и комбинации измерений/даты.
- **KeywordPosition**: ranking snapshot, normalized query, raw/numeric position, status, frequency, group, target URL and normalized URL. Уникально `(ranking_snapshot, normalized_query, group)`; при появлении стабильного внешнего ID он становится предпочтительной частью ключа.

### Ручные работы

- **WorkLogItem**: project, date, category, title, status, URL, material/page name, character count, responsible, comment, result URL, created_by. Индекс `(project, date)`.

### Отчёт и экспорт

- **Report**: project, month (первый день календарного месяца), status, selected search combinations/goals/sections, created_by. Индекс `(project, month)`; допускаются черновики, версии нумеруются отдельно.
- **ReportVersion**: report, version_number, status, dataset snapshot, calculation/narrative/validator/template versions, checksum, locked_at. Уникально `(report, version_number)`.
- **ReportDatasetSnapshot**: report, schema version, immutable JSON payload, checksum, built_at. Содержит копию нужных настроек, provenance, нормализованные строки и рассчитанные факты; после фиксации не обновляется.
- **NarrativeBlock**: report version, section code, deterministic text, edited text, validated facts, confirmation status, sort order. Уникально `(report_version, section_code, sort_order)`.
- **ValidationIssue**: report version, code, severity (`error`/`warning`), section, message, context, resolution fields. Ошибки блокируют финальный экспорт.
- **GeneratedArtifact**: report version, type (`docx`/`pdf`/`xlsx`), file, filename, size, SHA-256, generator version, status, redacted log. Индекс `(report_version, type, generated_at)`.

Override рассчитанного значения не меняет исходную строку. В MVP-1 он хранится в структурированном разделе snapshot/report configuration с оригиналом, заменой, причиной, автором и временем. Отдельная таблица вводится только при необходимости нескольких ревизий.

## Последовательность реализации

1. **Каркас:** Django 5.2, PostgreSQL, Docker Compose, Carlito, LibreOffice, pytest, settings и health check.
2. **Проекты:** администратор, формы проекта, брендовые правила, цели и URL-группы; тест приоритета пересекающихся групп.
3. **Импорт позиций:** CSV/XLSX, валидация схемы и размера, preview ошибок, idempotency, нормализация и provenance.
4. **Синтетические источники и работы:** стабильные обезличенные данные трёх месяцев, формы работ.
5. **Расчёты:** периоды, изменения, процентные пункты, топ-диапазоны, URL-группы и предупреждения смены набора запросов.
6. **Версии:** явная фиксация, транзакционная сборка immutable snapshot, checksum и запрет мутации.
7. **Narratives и QA:** шаблонные выводы, проверки периода, домена, бренда, источника, арифметики, URL, плейсхолдеров и секретов.
8. **Предпросмотр:** разделы отчёта, provenance, редактирование только текста и создание следующей версии после изменения данных.
9. **Экспорт:** DOCX полного профиля, PDF из DOCX, XLSX; повторный заголовок таблиц, альбомное приложение и имена файлов.
10. **Сквозной тест и документация:** демопроект, повторная генерация, команды запуска, миграций, admin, tests, backup и ограничения MVP-1.

После каждого среза запускаются unit/integration tests, форматирование и проверка отсутствия новых миграций. Визуальная проверка запускается в Docker, где доступны LibreOffice и Carlito.

## Критерии приёмки

1. `docker compose up --build` запускает Django и PostgreSQL на одном VPS или локальной машине; persistent volume сохраняет БД и артефакты.
2. Через документированную команду создаётся единственный администратор; неавторизованный доступ закрыт.
3. Администратор создаёт проект и настраивает домен, явные брендовые правила, цели и приоритетные URL-группы.
4. CSV/XLSX до 3000 фраз импортируется идемпотентно; ошибки строк понятны, исходный клиентский архив не используется как фикстура.
5. Синтетические Метрика/Вебмастер и ручные работы покрывают текущий, предыдущий и квартальный вид отчёта.
6. Все согласованные диапазоны и top-10/top-30 рассчитаны отдельно по поисковой системе, региону и устройству.
7. Пересечение URL-групп выбирает максимальный приоритет и создаёт предупреждение.
8. Явная команда создаёт неизменяемую версию со snapshot и checksum; рендер не обращается к импорту или адаптерам.
9. Детерминированные выводы различают относительный процент и процентные пункты и не делят на ноль.
10. Валидатор ловит неверный период, чужой бренд или домен, неверный источник, неподтверждённое число, арифметическую ошибку и незакрытый плейсхолдер.
11. Критическая ошибка блокирует финальный экспорт; предупреждение даёт DOCX/PDF с отметкой «Черновик».
12. Создаются открываемые DOCX, обычный PDF и XLSX с согласованными именами и одинаковыми показателями.
13. DOCX использует Carlito, колонтитулы, номера страниц, повтор заголовков и альбомную секцию подробных позиций.
14. Повторная генерация той же версии без нового импорта даёт то же содержание и показатели; побитовая идентичность не требуется.
15. Сквозной pytest-сценарий создаёт демопроект, импортирует позиции, добавляет работу, исправляет QA-проблему и проверяет три артефакта.
16. README документирует запуск, миграции, администратора, тесты, резервное копирование и ограничения MVP-1.

## Риски, зафиксированные после анализа

- Эталоны содержат очень большие таблицы, поэтому DOCX/PDF требуют ограничения ресурсов и визуального теста.
- Структура и стили исходников непоследовательны; шаблон воспроизводит состав, а не случайные дефекты форматирования.
- LibreOffice отсутствовал в хост-окружении анализа, поэтому эталонный визуальный baseline создаётся внутри будущего Docker-образа.
- Содержание синтетических источников должно быть стабильным и полностью обезличенным, иначе проверка воспроизводимости будет ненадёжной.
