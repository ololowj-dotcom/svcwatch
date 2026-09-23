# svcwatch

[![CI](https://github.com/ololowj-dotcom/svcwatch/actions/workflows/ci.yml/badge.svg)](https://github.com/ololowj-dotcom/svcwatch/actions/workflows/ci.yml)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![License MIT](https://img.shields.io/badge/license-MIT-green)

**Небольшой сторож для Linux-серверов.** Следит за systemd-сервисами, Docker-контейнерами, процессами,
HTTP-адресами и TCP-портами и сразу пишет в **Telegram** (или на почту / в любой webhook), когда что-то
сломалось, с приложенным traceback, и ещё раз, когда всё починилось.

*[Read in English](README.md)*

```
$ curl -fsSL https://raw.githubusercontent.com/ololowj-dotcom/svcwatch/main/install.sh | sudo bash
```

- **Настройка за две минуты.** Мастер подключает Telegram-бота, сам находит ваш chat_id, шлёт тестовое
  сообщение, подхватывает ваши сервисы и ставит фоновую службу.
- **Сообщает только важное.** Одна ошибка - одно уведомление (числа, id и время при сравнении игнорируются),
  весь traceback приходит одним сообщением, сетевой шум уходит в суточную сводку, а не на телефон.
- **Ловит то, что пропускает «а оно запущено?».** Сервис, который постоянно падает и перезапускается, для
  `systemctl` выглядит здоровым. svcwatch считает перезапуски и предупреждает.
- **Направляется точно туда, куда вы скажете.** У каждого сервиса, контейнера, адреса, порта или процесса
  могут быть своё имя, шаблоны ошибок, пороги и свой чат для уведомлений.
- **Не теряет уведомления.** Если Telegram недоступен, сообщение ждёт в очереди и отправляется повторно.
- **Маленький.** Только стандартная библиотека (на Python 3.11+ ни одной зависимости), один файл настроек,
  один процесс.

## Установка

**Одной командой** (Debian/Ubuntu и другие дистрибутивы с systemd; нужны Python 3.9+, `git`, `python3-venv`):

```bash
curl -fsSL https://raw.githubusercontent.com/ololowj-dotcom/svcwatch/main/install.sh | sudo bash
```

Скрипт создаёт отдельное виртуальное окружение в `/opt/svcwatch`, кладёт команду `svcwatch` в
`/usr/local/bin` и запускает мастер. Только установить, без мастера: `SVCWATCH_NO_SETUP=1` перед `bash`.

**Через pip** (где угодно):

```bash
pip install git+https://github.com/ololowj-dotcom/svcwatch
```

## Быстрый старт

```bash
sudo svcwatch setup
```

Мастеру нужно одно: токен бота от [@BotFather](https://t.me/BotFather). Дальше он:

1. проверяет токен и показывает имя бота;
2. просит нажать **Start** у бота (или добавить его в группу) и сам определяет чат;
3. отправляет тестовое сообщение;
4. показывает найденные сервисы и сколько из них будет под наблюдением;
5. записывает `/etc/svcwatch/svcwatch.toml` и закрытый файл `.env` с токеном;
6. предлагает установить и запустить фоновую службу.

После этого в любой момент:

```bash
svcwatch check          # что под наблюдением и в каком состоянии сейчас
svcwatch test-notify    # тестовое уведомление во все каналы
```

### Менять настройки позже, не открывая конфиг

```bash
svcwatch add telegram --name team              # ещё один бот или чат (chat_id найдётся сам)
svcwatch watch nginx --label "Web server"      # systemd-сервис
svcwatch watch --docker web db                 # Docker-контейнеры
svcwatch watch --http https://example.com/health --contains ok
svcwatch watch --tcp 5432 --name postgres      # порт (HOST:PORT или просто PORT для этой машины)
svcwatch watch --process "python -m app.worker" --name worker --min-count 2
svcwatch watch payments --notify team          # уведомления по этому сервису только в чат "team"
```

Команды дописывают файл (ваши комментарии остаются), проверяют результат до сохранения, хранят прошлую версию
как `svcwatch.toml.bak`, не принимают несуществующие имена (без `--force`), сразу пробуют новый URL, порт или
процесс, чтобы вы увидели, работает ли он, и перезапускают фоновую службу, если она запущена.

## Telegram

Создайте бота у [@BotFather](https://t.me/BotFather) (`/newbot`). Для svcwatch лучше отдельный бот:
Telegram разрешает читать сообщения бота только одной программе, а команды из чата (ниже) это используют.

Так выглядят уведомления:

```
🔴 Service down: payments
web-01 · 2026-03-01 04:12:09

failed/failed (result: exit-code)
```

После починки приходит `🟢 ... recovered` с временем простоя. Восстановления и сводки приходят без звука.

**Команды в чате** (отвечают только в настроенном чате и в `allowed_chats`, если вы их указали):

| Команда | Что делает |
|---|---|
| `/status` | что работает, а что нет, прямо сейчас |
| `/mute 30m` | приостановить уведомления (`s`, `m`, `h`, `d`; по умолчанию 1 час), удобно на время деплоя |
| `/unmute` | включить обратно |
| `/ping` | жив ли сам сторож |

Поддерживаются несколько ботов и чатов, темы форума и фильтр по важности, см.
[полный пример](examples/svcwatch.example.toml). Если `api.telegram.org` с вашего сервера открывается
нестабильно, укажите свой релей: `svcwatch setup --api-base https://...` (параметр `api_base`).

Почта (SMTP) и webhook работают так же; в payload webhook есть поля `text` и `content`, поэтому Slack,
Mattermost и Discord принимают его как есть.

## Что именно отслеживать

По умолчанию svcwatch следит за **сервисами, которые вы развернули сами** (unit-файлы в
`/etc/systemd/system`), а не за системными. Всё остальное включается явно и точечно:

```toml
[systemd]
discover = "custom"            # custom = мои сервисы | all = все | off = только перечисленные
exclude  = ["svcwatch", "backup-*"]
units    = ["nginx", "postgresql"]          # следить всегда

[[systemd.watch]]              # правила для ОДНОГО сервиса (или маски вроде "worker-*")
match = "payments"
label = "Payments API"                       # имя в уведомлениях
alert_on = ["failed", "inactive"]            # сообщать и если сервис остановили
immediate_extra = ["ERROR", "re:timeout after \\d+s"]   # свои шаблоны; "re:" = регулярное выражение
ignore_extra = ["health check passed"]       # эти строки игнорировать
fail_threshold = 1
notify = ["oncall"]                          # слать только в этот чат

[[systemd.watch]]
match = "nightly-report"
logs = false                                 # следить за состоянием, но не за логом
```

Кроме systemd: контейнеры Docker (`[docker]`), проверки адресов (`[[http]]`), портов (`[[tcp]]`) и процессов
(`[[process]]`, поиск по полной командной строке). У каждого правила и проверки есть `label`, `notify`,
`fail_threshold` и `remind_after`. Опечатка в настройках не молча игнорируется, а объясняется с подсказкой
(`unknown setting 'intervall' (did you mean 'interval'?)`).

## Как работают уведомления

| Ситуация | Что вы получите |
|---|---|
| сервис в состоянии `failed` (или `inactive`, если так настроено) | одно уведомление «упал», потом «восстановился» с временем простоя |
| сервис сам перезапустился (`Restart=always`) | предупреждение со счётчиком перезапусков |
| ошибка в логе (`Traceback`, `CRITICAL`, ваши шаблоны) | одно уведомление с traceback; такая же ошибка молчит `dedup_window` (30 мин) |
| «шум» (`Bad Gateway`, `Connection reset`...) | считается и приходит раз в сутки в сводке |
| не прошла проверка адреса / порта / процесса | «упал» после `fail_threshold` неудач подряд, затем «восстановился» |
| всё ещё сломано спустя часы | необязательное напоминание (`remind_after`) |
| Telegram / SMTP недоступны | уведомление ждёт в очереди и отправляется повторно (до 24 часов) |
| шторм ошибок | не больше `rate_limit_per_hour` (60) уведомлений и одно предупреждение; восстановления не теряются |
| первый запуск | старая история **не** пересылается: сообщается только о том, что случилось после старта |

## Безопасность

- Токен бота хранится только в `.env` (права `0600`), не попадает в конфиг, не печатается и не пишется в лог.
- Чтобы читать журналы чужих сервисов, нужен root (или группа `systemd-journal`). Поставляемый unit запускается
  от root с `NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome`, `PrivateTmp`. Чтобы работать от
  обычного пользователя, добавьте его в группы `systemd-journal` (и `docker`, если следите за контейнерами).
- Команды из чата принимаются только из настроенного чата (и `allowed_chats`), остальное игнорируется и
  записывается в лог.
- Открытых портов нет: svcwatch только сам подключается к тому, что вы настроили.

## Разработка

```bash
git clone https://github.com/ololowj-dotcom/svcwatch && cd svcwatch
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest && ruff check src tests
```

Тесты не трогают настоящую систему: `systemctl`, `journalctl`, `docker` и `pgrep` заменены записанными
ответами, а Telegram, webhook и веб-страницы изображают маленькие локальные заглушки. В CI тесты идут на
Python 3.9-3.13.

## Лицензия

MIT, см. [LICENSE](LICENSE).
