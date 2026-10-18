#!/usr/bin/env python3
"""Zamanlanmış çalışmada botun bu sefer çalışıp çalışmayacağına karar verir.
Normalde 20 dakikada bir, yarış haftasonunda 5 dakikada bir çalışır. Sadece standart kütüphane kullanır."""
import json
import os
import re
from datetime import datetime, timedelta, timezone

now = datetime.now(timezone.utc)


def finish(run, reason):
    print(reason)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"run={'true' if run else 'false'}\n")
    raise SystemExit(0)


if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
    finish(True, "Elle çalıştırma: her zaman çalışır")

try:
    with open("state/seen.json", encoding="utf-8") as f:
        state = json.load(f)
except Exception:
    finish(True, "Kayıt dosyası yok: çalışır")

with open("sources.yaml", encoding="utf-8") as f:
    cfg = f.read()


def setting(name, default):
    m = re.search(rf"^\s*{name}:\s*([^\s#]+)", cfg, re.M)
    return m.group(1).strip("\"'") if m else default


mode = setting("race_weekend", "auto").lower()
normal = int(setting("normal_interval_min", 20))
race = int(setting("race_interval_min", 5))
in_race = mode == "on" or (mode == "auto" and any(
    datetime.fromisoformat(c[0]) <= now <= datetime.fromisoformat(c[1]) for c in state.get("calendar", [])))
interval = race if in_race else normal

last = state.get("last_run")
if last and now - datetime.fromisoformat(last) < timedelta(minutes=max(interval - 3, 1)):
    finish(False, f"Atlandı (aralık {interval} dk, yarış modu {'açık' if in_race else 'kapalı'})")
finish(True, f"Çalışıyor (aralık {interval} dk, yarış modu {'açık' if in_race else 'kapalı'})")
