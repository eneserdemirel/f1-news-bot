#!/usr/bin/env python3
"""Botun kontrol aralığını dakika olarak yazdırır: normalde normal_interval_min,
yarış haftasonunda race_interval_min. Workflow'daki döngü her turdan sonra bunu çağırır.
Sadece standart kütüphane kullanır."""
import json
import re
from datetime import datetime, timezone

now = datetime.now(timezone.utc)

try:
    with open("sources.yaml", encoding="utf-8") as f:
        cfg = f.read()
except Exception:
    cfg = ""

try:
    with open("state/seen.json", encoding="utf-8") as f:
        calendar = json.load(f).get("calendar", [])
except Exception:
    calendar = []


def setting(name, default):
    m = re.search(rf"^\s*{name}:\s*([^\s#]+)", cfg, re.M)
    return m.group(1).strip("\"'") if m else default


try:
    mode = str(setting("race_weekend", "auto")).lower()
    normal = int(setting("normal_interval_min", 10))
    race = int(setting("race_interval_min", 5))
    in_race = mode == "on" or (mode == "auto" and any(
        datetime.fromisoformat(c[0]) <= now <= datetime.fromisoformat(c[1]) for c in calendar))
    print(max(race if in_race else normal, 1))
except Exception:
    print(10)
