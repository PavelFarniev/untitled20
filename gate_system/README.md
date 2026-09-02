# gate_system

Локальная система 24/7 для автоматического открытия откатных ворот (**Nice Robus**,
вход STEP-BY-STEP) по распознанному автомобильному номеру.

Разработка — на macOS с программной заглушкой реле. Эксплуатация — на Raspberry Pi
(4/5, RPi OS Lite): меняется только модуль ворот, остальная логика неизменна.

> **Статус: Этап 0 — архитектура + каркас.** Ядро (домен, порты, config, logger,
> реле-заглушка, репозитории, use cases) реализовано и импортируется. Внешние
> адаптеры (камера, YOLO, EasyOCR, Telegram, веб, watchdog, диагностика) —
> подробные заглушки; наполняются пошагово по плану из `ARCHITECTURE.md` §9.

## Структура

```
gate_system/
├── main.py            # Composition Root: сборка зависимостей, запуск
├── models.py          # домен: сущности и enum
├── interfaces.py      # порты (ABC): контракты адаптеров
├── pipeline.py        # ядро: RecognitionPipeline, AccessDecision, AntiReplayGuard
├── config.py          # типизированный конфиг из config.json
├── logger.py          # logging + перехват исключений потоков
├── camera.py          # RTSPCamera (FrameSource)          [этап 2]
├── detector.py        # YoloDetector (Detector)           [этап 3]
├── ocr.py             # EasyOcrEngine (OcrEngine)          [этап 4]
├── relay.py           # StubRelay / GpioRelay / UsbRelay
├── gate_controller.py # RelayGateController (GateController)
├── whitelist.py       # Json/Sqlite WhitelistRepository
├── history.py         # HistoryStore (HistoryRepository)
├── database.py        # атомарное JSON-хранилище
├── network.py         # NetworkMonitor                    [этап 8]
├── telegram_bot.py    # TelegramNotifier + бот + настройки/логи
├── watchdog.py        # heartbeat потоков                 [этап 10]
├── diagnostics.py     # самодиагностика                   [этап 10]
├── utils.py           # нормализация номера РФ и пр.
├── config/            # config.json, allowed.json
├── logs/  photos/
├── systemd/gate_system.service
└── requirements.txt
```

## Управление через Telegram (единый интерфейс, без отдельного сайта)

Доступ двухуровневый:

- **Владелец** — задаётся вручную числовым Telegram-ID в `config.json → telegram.owner_id`
  (свой ID можно узнать у `@userinfobot`). Может всё.
- **Админы** — список ID в `config/admins.json`, которым управляет владелец из бота.
  Могут открывать ворота и смотреть статус/фото/историю/список, но не менять настройки,
  белый список, логи и список админов.

Управление — **кнопками** (без ввода команд). Отправьте боту `/start` — появится
постоянное меню внизу экрана.

Кнопки **владельца и админов**: 🚪 Открыть ворота · 📷 Фото · 📊 Статус · 🕓 История · 📋 Белый список.

Дополнительно **у владельца**: 📜 Журнал · ⚙️ Настройки · 👥 Админы.

Действия с вводом сделаны пошагово: нажимаете «➕ Добавить» под белым списком —
бот просит отправить номер; «❌ Удалить» — показывает номера кнопками; так же для
админов и настроек. При неизвестном авто бот присылает фото, номер, время и кнопки
«✅ Открыть» / «❌ Игнорировать».

Команды тоже продолжают работать (`/open`, `/status` и т.д.), но пользоваться
кнопками удобнее.

## Запуск (текущий каркас)

```bash
cd gate_system
python main.py     # соберёт ядро, реле-заглушка напечатает 'Gate opened' при открытии
```

## Перенос на Raspberry Pi

1. Скопировать проект, создать venv, `pip install -r requirements.txt` (+ раскомментировать GPIO/USB).
2. В `config/config.json` изменить `relay.backend`: `stub` → `gpio` (или `usb`).
3. Установить сервис: `sudo cp systemd/gate_system.service /etc/systemd/system/ && sudo systemctl enable --now gate_system`.

Полная архитектура, UML-диаграммы и порядок этапов — в [`ARCHITECTURE.md`](ARCHITECTURE.md).
