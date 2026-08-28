# movie_pars

Автоматический мониторинг данных ЕАИС для предсеансового обслуживания и связанных технических короткометражек.

## EAIS Spider-Man 4 monitor

Workflow: `.github/workflows/eais-spiderman-monitor.yml`

Источники: `https://newgenres.duckdns.org/eais/api/v1`.

Расписание GitHub Actions синхронизировано с московским временем (UTC+3): 00:30, 06:30, 12:30 и 18:30 МСК ежедневно. Основной контроль закрытого предыдущего дня — срез 06:30 МСК.

Список отслеживаемых технических проектов хранится в `config/spiderman4-films.json`.

Для работы необходимо добавить repository secret `NEWGENRES_API_KEY` в Settings → Secrets and variables → Actions. Секрет используется только как Bearer token и не выводится в artifacts/logs.

Каждый успешный запуск сохраняет полный JSON ЕАИС (`request`, `summary`, `data.films`, `metadata`, `daily_stats`, `daily_schedule`, `hourly_schedule`, `errors`) как GitHub Actions artifact на 90 дней, а также отдельные компактные файлы со сводкой и поддержкой проектов.
