"""Проверка реле на Raspberry Pi — «щёлкнуть» им без запуска всей системы.

Запуск на Pi (систему перед этим лучше остановить: sudo systemctl stop gate_system):
    python tools/relay_check.py                 # пин и режим из config.json, 1 импульс
    python tools/relay_check.py --times 3       # три импульса с паузой
    python tools/relay_check.py --pin 27 --low  # другой пин, модуль Low-trigger

Что должно произойти: на каждый импульс реле громко щёлкает и на модуле мигает
светодиод. Если щелчка нет — см. раздел «Подключение реле» в README.md.

ВНИМАНИЕ: если реле уже подключено к блоку ворот, ворота реально откроются.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # корень проекта

from gate.config import Config  # noqa: E402
from gate.hardware.relay import GpioRelay, RelayError  # noqa: E402


def main() -> None:
    cfg = Config.load()
    ap = argparse.ArgumentParser(description="Проверка GPIO-реле")
    ap.add_argument("--pin", type=int, default=cfg.relay.gpio_pin, help="номер GPIO (BCM)")
    ap.add_argument("--low", action="store_true", help="модуль срабатывает от низкого уровня")
    ap.add_argument("--high", action="store_true", help="модуль срабатывает от высокого уровня")
    ap.add_argument("--ms", type=int, default=cfg.relay.pulse_ms, help="длительность импульса, мс")
    ap.add_argument("--times", type=int, default=1, help="сколько импульсов")
    args = ap.parse_args()

    active_high = cfg.relay.active_high
    if args.low:
        active_high = False
    if args.high:
        active_high = True

    print(f"GPIO{args.pin}, {'High' if active_high else 'Low'}-trigger, импульс {args.ms} мс, "
          f"раз: {args.times}")
    try:
        relay = GpioRelay(args.pin, active_high=active_high)
    except RelayError as exc:
        print(f"❌ {exc}")
        sys.exit(1)
    try:
        for i in range(args.times):
            print(f"  импульс {i + 1}…", flush=True)
            relay.pulse(args.ms)
            if i + 1 < args.times:
                time.sleep(2)
    finally:
        relay.close()
    print("✅ Готово. Был щелчок? Если да — реле подключено правильно.")


if __name__ == "__main__":
    main()
