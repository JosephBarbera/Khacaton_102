"""Импорт событий из CSV/JSON. Идемпотентен: повторная загрузка не создаёт дублей.

Использование:
    python import_events.py data/events_sample.json
    python import_events.py import.csv --actor "модератор Иван"
"""
import argparse
import csv
import json
import sys

import db


def load_records(path):
    if str(path).lower().endswith(".csv"):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("events", [])
    if not isinstance(data, list):
        raise SystemExit("Файл должен содержать список событий (JSON-массив или ключ «events»).")
    return data


def main():
    parser = argparse.ArgumentParser(description="Импорт событий в базу афиши")
    parser.add_argument("file", help="CSV или JSON с событиями")
    parser.add_argument("--actor", default="импорт", help="Кто выполняет импорт (для журнала изменений)")
    args = parser.parse_args()

    db.init_db()
    counts = {"added": 0, "updated": 0, "skipped": 0, "rejected": 0}
    rejected = []

    for i, raw in enumerate(load_records(args.file), 1):
        clean, err = db.normalize_event(raw)
        if err:
            counts["rejected"] += 1
            title = raw.get("title") or "без названия"
            rejected.append("строка {0} («{1}»): {2}".format(i, title, err))
            continue
        result = db.upsert_event(clean, actor=args.actor)
        counts[result] += 1

    print("Импорт завершён: добавлено {added}, обновлено {updated}, "
          "пропущено {skipped}, отклонено {rejected}.".format(**counts))
    for line in rejected[:20]:
        print("  отклонено —", line)
    if len(rejected) > 20:
        print("  … и ещё {0} отклонённых".format(len(rejected) - 20))
    return 1 if counts["rejected"] else 0


if __name__ == "__main__":
    sys.exit(main())
