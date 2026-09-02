"""Загрузка и валидация конфигурации из config/config.json.

Конфиг типизирован через dataclass'ы: доступ к параметрам идёт как
`cfg.camera.rtsp_url`, а не по «сырым» ключам словаря. Отсутствующие поля
заполняются значениями по умолчанию, что упрощает первый запуск.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from typing import Any, Dict, List

_log = logging.getLogger("gate_system.config")


def _only_known(cls, raw: Any) -> Dict[str, Any]:
    """Оставить только известные поля dataclass'а; иначе — пустой словарь.

    Защищает Config.load от падения на неизвестных ключах или секции не того
    типа (например, `camera` задан списком) — вместо TypeError берём дефолты.
    """
    if not isinstance(raw, dict):
        if raw not in (None, {}):
            _log.warning("Секция конфига имеет неверный тип — использую значения по умолчанию")
        return {}
    valid = {f.name for f in fields(cls)}
    unknown = set(raw) - valid
    if unknown:
        _log.warning("Неизвестные ключи в конфиге проигнорированы: %s", ", ".join(sorted(unknown)))
    return {k: v for k, v in raw.items() if k in valid}


@dataclass
class CameraConfig:
    rtsp_url: str = "rtsp://user:pass@192.168.1.10:554/stream1"
    reconnect_min_delay_s: float = 1.0
    reconnect_max_delay_s: float = 30.0
    process_every_n_frame: int = 3  # прореживание для экономии CPU на RPi


@dataclass
class CameraSpec:
    """Описание одной камеры (для конфигурации с несколькими камерами)."""

    name: str = "camera"
    rtsp_url: str = ""
    role: str = "entry"            # entry (въезд) | exit (выезд)
    require_motion: bool = False   # выезд: реагировать только на движущийся номер
    process_every_n_frame: int = 3
    reconnect_min_delay_s: float = 1.0
    reconnect_max_delay_s: float = 30.0


@dataclass
class DetectionConfig:
    model_path: str = "models/yolov8n.pt"
    plate_model_path: str = ""  # пусто = распознавать номер по области авто (без спец-модели)
    vehicle_conf: float = 0.45
    plate_conf: float = 0.35
    require_vehicle: bool = True  # False = искать номер по всему кадру (для Nomeroff)
    require_motion: bool = False  # True = открывать только если номер движется (выездная камера)
    motion_threshold: float = 0.02  # порог смещения рамки (доля кадра) = «движение»
    motion_confirm_frames: int = 2  # сколько кадров подряд с движением нужно
    # --- Адаптивная обработка (экономия CPU на Raspberry Pi) ---
    # В простое крутится ТОЛЬКО лёгкий детектор рамки (best.pt) с редким шагом
    # idle_process_every_n. Как только он находит рамку с уверенностью ≥ plate_conf,
    # система переходит в активный режим (частый шаг process_every_n_frame камеры) и
    # подключает тяжёлый распознаватель (Nomeroff). Через active_hold_s секунд без
    # номера — возврат в простой. Порог пробуждения = plate_conf.
    adaptive: bool = False
    idle_process_every_n: int = 30  # шаг в простое (≈1 кадр/сек при 30 fps)
    active_hold_s: float = 5.0      # держать активный режим N c после последней рамки


@dataclass
class OcrConfig:
    engine: str = "easyocr"  # easyocr | nomeroff
    languages: list = field(default_factory=lambda: ["en", "ru"])
    gpu: bool = False
    min_confidence: float = 0.4
    upscale: float = 1.0  # во сколько раз увеличивать кадр перед OCR (помогает мелким номерам)


@dataclass
class RelayConfig:
    backend: str = "stub"      # stub | gpio | usb
    gpio_pin: int = 17
    usb_device: str = ""
    pulse_ms: int = 500
    active_high: bool = True


@dataclass
class TelegramConfig:
    enabled: bool = True
    token: str = ""
    owner_id: int = 0
    reconnect_delay_s: float = 5.0
    proxy_url: str = ""  # активный прокси для бота (может управляться ProxyManager)


@dataclass
class ProxyConfig:
    """Автопокупка/ротация прокси через proxy6.net (опционально)."""

    enabled: bool = False
    api_key: str = ""
    version: int = 4            # 4 = IPv4 (для Telegram; не 6/IPv6 и не 5/MTProto)
    country: str = "nl"        # iso2
    period_days: int = 30
    buy_before_days: int = 3   # покупать новый за N дней до окончания
    max_buys_per_30d: int = 3  # жёсткий лимит покупок за 30 дней
    cooldown_hours: float = 24.0
    descr: str = "gate_system"
    state_path: str = "config/proxy_state.json"
    check_interval_hours: float = 6.0


@dataclass
class AccessConfig:
    antireplay_window_s: int = 30
    confirm_frames: int = 0  # >0: собирать номер голосованием по N кадрам (устойчивость к смазу)


@dataclass
class PathsConfig:
    allowed: str = "config/allowed.json"
    history_db: str = "config/history.json"
    admins: str = "config/admins.json"
    photos_dir: str = "photos"
    logs_dir: str = "logs"


@dataclass
class DiagnosticsConfig:
    interval_s: int = 60
    min_disk_free_percent: float = 10.0
    max_cpu_temp_c: float = 75.0


@dataclass
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    relay: RelayConfig = field(default_factory=RelayConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    access: AccessConfig = field(default_factory=AccessConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    diagnostics: DiagnosticsConfig = field(default_factory=DiagnosticsConfig)
    log_level: str = "INFO"
    log_max_mb: float = 5.0   # размер одного файла лога, МБ
    log_backups: int = 3      # число архивных копий (итого ≈ max_mb*(backups+1) МБ)
    photos_max_files: int = 500  # хранить не больше N последних фото (0 = без лимита)
    cameras: List[CameraSpec] = field(default_factory=list)  # несколько камер (опционально)

    @staticmethod
    def load(path: str | Path = "config/config.json") -> "Config":
        """Загрузить конфигурацию из JSON-файла.

        Если файл отсутствует, возвращает конфиг по умолчанию (полезно для
        первого запуска и тестов).
        """
        p = Path(path)
        if not p.exists():
            return Config()
        raw: Dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            _log.warning("config.json не является объектом — использую значения по умолчанию")
            return Config()
        cams_raw = raw.get("cameras", [])
        cameras = [CameraSpec(**_only_known(CameraSpec, c))
                   for c in (cams_raw if isinstance(cams_raw, list) else []) if isinstance(c, dict)]
        return Config(
            camera=CameraConfig(**_only_known(CameraConfig, raw.get("camera", {}))),
            detection=DetectionConfig(**_only_known(DetectionConfig, raw.get("detection", {}))),
            ocr=OcrConfig(**_only_known(OcrConfig, raw.get("ocr", {}))),
            relay=RelayConfig(**_only_known(RelayConfig, raw.get("relay", {}))),
            telegram=TelegramConfig(**_only_known(TelegramConfig, raw.get("telegram", {}))),
            proxy=ProxyConfig(**_only_known(ProxyConfig, raw.get("proxy", {}))),
            access=AccessConfig(**_only_known(AccessConfig, raw.get("access", {}))),
            paths=PathsConfig(**_only_known(PathsConfig, raw.get("paths", {}))),
            diagnostics=DiagnosticsConfig(**_only_known(DiagnosticsConfig, raw.get("diagnostics", {}))),
            log_level=raw.get("log_level", "INFO"),
            log_max_mb=raw.get("log_max_mb", 5.0),
            log_backups=raw.get("log_backups", 3),
            photos_max_files=raw.get("photos_max_files", 500),
            cameras=cameras,
        )

    def effective_cameras(self) -> List[CameraSpec]:
        """Вернуть список камер. Если раздел `cameras` не задан — одна камера из
        секции `camera` (обратная совместимость с одиночной конфигурацией)."""
        if self.cameras:
            return self.cameras
        return [CameraSpec(
            name="camera",
            rtsp_url=self.camera.rtsp_url,
            role="entry",
            require_motion=self.detection.require_motion,
            process_every_n_frame=self.camera.process_every_n_frame,
            reconnect_min_delay_s=self.camera.reconnect_min_delay_s,
            reconnect_max_delay_s=self.camera.reconnect_max_delay_s,
        )]

    def to_dict(self) -> Dict[str, Any]:
        """Сериализовать конфиг в словарь."""
        return asdict(self)


# Секции конфига, доступные для правки из Telegram, и их «чувствительные» ключи.
_EDITABLE_SECTIONS = {"camera", "detection", "ocr", "relay", "telegram", "access", "diagnostics", "proxy"}
_SECRET_KEYS = {"token", "api_key"}
# Ключи, изменение которых требует перезапуска процесса (нельзя применить «на лету»).
_RESTART_KEYS = {"camera.rtsp_url", "telegram.token", "telegram.owner_id",
                 "detection.model_path", "detection.plate_model_path"}


class ConfigStore:
    """Чтение и правка config.json по «плоским» ключам вида section.key.

    Используется Telegram-командами /settings и /set. Значение приводится к типу
    текущего значения (int/float/bool/str). Запись атомарная.
    """

    def __init__(self, path: str = "config/config.json") -> None:
        self._path = str(path)

    def raw(self) -> Dict[str, Any]:
        """Актуальный словарь конфига (с дефолтами, если файла нет)."""
        import database
        return database.read_json(self._path, Config().to_dict())

    def as_display(self) -> str:
        """Текстовое представление настроек (токен маскируется)."""
        data = self.raw()
        lines = []
        for section in sorted(_EDITABLE_SECTIONS):
            if section not in data:
                continue
            lines.append(f"[{section}]")
            for key, value in data[section].items():
                shown = "***" if key in _SECRET_KEYS and value else value
                lines.append(f"  {section}.{key} = {shown}")
        return "\n".join(lines)

    def set(self, dotted_key: str, raw_value: str) -> str:
        """Изменить значение настройки. Возвращает человекочитаемый результат.

        :raises KeyError: если секция/ключ не существуют или недоступны для правки.
        :raises ValueError: если значение нельзя привести к нужному типу.
        """
        import database

        if "." not in dotted_key:
            raise KeyError("Ключ должен быть вида section.key")
        section, key = dotted_key.split(".", 1)
        if section not in _EDITABLE_SECTIONS:
            raise KeyError(f"Секция недоступна для правки: {section}")

        data = self.raw()
        if section not in data or key not in data[section]:
            raise KeyError(f"Нет такого параметра: {dotted_key}")

        coerced = self._coerce(data[section][key], raw_value)
        data[section][key] = coerced
        database.write_json_atomic(self._path, data)

        note = " (требуется перезапуск)" if dotted_key in _RESTART_KEYS else ""
        shown = "***" if key in _SECRET_KEYS else coerced
        return f"✅ {dotted_key} = {shown}{note}"

    @staticmethod
    def _coerce(current: Any, raw_value: str) -> Any:
        """Привести строку к типу текущего значения."""
        if isinstance(current, bool):
            if raw_value.lower() in ("1", "true", "yes", "on", "да"):
                return True
            if raw_value.lower() in ("0", "false", "no", "off", "нет"):
                return False
            raise ValueError(f"Ожидалось true/false, получено {raw_value!r}")
        if isinstance(current, int):
            return int(raw_value)
        if isinstance(current, float):
            return float(raw_value)
        if isinstance(current, list):
            return [x.strip() for x in raw_value.split(",") if x.strip()]
        return raw_value
