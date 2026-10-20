#!/usr/bin/env python3
"""
F1 haber botu: RSS -> Claude (puanlama + özet) -> Telegram
Çalıştırma:  python bot.py --mode run   (normal)
             python bot.py --mode test  (kaynakları test et + örnek özet gönder)
"""
import argparse
import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse

import feedparser
import requests
import trafilatura
import yaml
from anthropic import Anthropic
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "sources.yaml"
STATE_PATH = ROOT / "state" / "seen.json"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, "
              "text/xml, text/html;q=0.9, */*;q=0.8",
}
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
PREFIX = "🚨 | "
GNEWS_LANG = {
    "en": "hl=en-GB&gl=GB&ceid=GB:en",
    "it": "hl=it&gl=IT&ceid=IT:it",
    "de": "hl=de&gl=DE&ceid=DE:de",
    "es": "hl=es&gl=ES&ceid=ES:es",
    "nl": "hl=nl&gl=NL&ceid=NL:nl",
}

# ---------------------------------------------------------------- prompts
TRIAGE_PROMPT = """You are the news editor of a fast Formula 1 breaking-news account.
New headlines come from several sites, some in Italian, German, Spanish or Dutch.

Stories already tracked in the last 24 hours ([posted] = already published by us):
<<STORIES>>

New items:
<<ITEMS>>

For EVERY new item return one JSON object with:
- "i": the item number
- "score": importance 1-10 for an F1 breaking-news account
   10 = huge breaking news: confirmed driver or team principal moves, sackings, title decided,
        serious crashes or injuries
    9 = major official news: contract signings, big penalties or disqualifications,
        official FIA/F1 decisions, legal cases, race or qualifying results
    8 = credible reports from major outlets about the above, important technical or rule changes
    7 = notable statements from key figures on hot topics, significant team news
    5-6 = moderately interesting news
    3-4 = minor quotes, previews, routine updates
    1-2 = filler, opinion pieces, listicles, quizzes, betting, merchandise,
          or NOT about Formula 1 (other series, road cars, other sports)
- "story": the id of the tracked story this item is about (e.g. "S12"), or a new label
  "N1", "N2", ... if it is a new story. Items in this list about the same new story share the same N label.
  Different angles or reactions to the same event belong to the same story.
- "title": for a new story, a short neutral English label of max 12 words; otherwise ""
- "update": true only if the story is marked [posted] AND this item adds a significant new
  development (official confirmation, verdict, new key fact); otherwise false
Return only a JSON array, nothing else."""

WRITE_PROMPT = """Bir Formula 1 haber hesabı için Türkçe paylaşım metni yazıyorsun.

Kaynak site: <<SOURCE>>
Başlık: <<TITLE>>
Sitenin verdiği kısa özet:
<<TEXT>>

Kurallar:
1. Sadece başlıkta ve özette yazanlara tamamen sadık kal. Olmayan bilgi, yorum, tahmin veya
   abartı ekleme. Haber bir iddia ya da başka bir yayına dayanıyorsa bunu belirt
   ("... göre", "iddiaya göre").
2. Haber esas olarak birinin sözleriyse şu biçimi kullan: 🚨 | İsim: "alıntı"
   Alıntının en önemli kısmını anlamını değiştirmeden Türkçeye çevir; düşünceyi yarıda kesme
   (merak uyandırmak için kesik bırakma), uzun da tutma. Sadece metinde geçen sözleri kullan.
3. Tek bir basit bilgiyse en fazla 280 karakter, birden fazla önemli detay varsa en fazla 400
   karakter. "🚨 | " dahil 400'ü asla geçme.
4. Metin tam olarak "🚨 | " ile başlasın. Hashtag, başka emoji, link veya kaynak satırı ekleme.
5. "type": alıntıysa "quote", değilse "news".
6. "status": resmi bir açıklama veya doğrudan ilgili kişinin/takımın/FIA'nın sözüyse
   "official"; medya haberi, iddia veya isimsiz kaynaklara dayanıyorsa "report".
Sadece JSON döndür: {"type": "...", "post_tr": "...", "status": "..."}"""

# ---------------------------------------------------------------- helpers
def now():
    return datetime.now(timezone.utc)


def log(*a):
    print(*a, flush=True)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    st = {}
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    st.setdefault("initialized", False)
    st.setdefault("seen", {})        # haber id -> ilk görülme zamanı
    st.setdefault("stories", {})     # hikaye id -> bilgiler
    st.setdefault("story_seq", 0)
    st.setdefault("feeds", {})       # kaynak adı -> çalışan RSS adresi
    st.setdefault("calendar", [])    # [başlangıç, bitiş, yarış adı]
    st.setdefault("last_alert", "")
    st.pop("sent", None)             # eski sürümden kalan alan
    return st


def save_state(st):
    seen_cut = now() - timedelta(days=14)
    st["seen"] = {k: v for k, v in st["seen"].items() if datetime.fromisoformat(v) > seen_cut}
    story_cut = now() - timedelta(hours=48)
    st["stories"] = {k: v for k, v in st["stories"].items()
                     if datetime.fromisoformat(v["last"]) > story_cut}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def http_get(url, timeout=20):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


def clean_text(s, limit=None):
    t = BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True)
    t = re.sub(r"\s+", " ", t)
    return t[:limit] if limit else t


def norm_link(link):
    p = urlparse(link)
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", "", ""))


# ---------------------------------------------------------------- feeds
def parse_feed(url):
    try:
        r = http_get(url)
    except Exception as e:
        log(f"  feed alınamadı: {url} ({e})")
        return []
    return feedparser.parse(r.content).entries or []


def discover_feeds(site):
    try:
        r = http_get(site)
    except Exception as e:
        log(f"  site açılamadı: {site} ({e})")
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    found = []
    for tag in soup.find_all("link", href=True):
        rel = " ".join(tag.get("rel") or []).lower()
        typ = (tag.get("type") or "").lower()
        if "alternate" in rel and ("rss" in typ or "atom" in typ):
            found.append(urljoin(site, tag["href"]))
    return found


def google_news_feed(src):
    domain = urlparse(src["site"]).netloc
    domain = domain[4:] if domain.startswith("www.") else domain
    query = src.get("gnews_query") or f"site:{domain}"
    lang = GNEWS_LANG.get(src.get("lang", "en"), GNEWS_LANG["en"])
    return f"https://news.google.com/rss/search?q={quote_plus(query + ' when:1d')}&{lang}"


def get_entries(src, state):
    """Sırayla: daha önce çalışan feed -> config'teki feed'ler -> otomatik bulma -> Google News."""
    name = src["name"]
    candidates = []
    cached = state["feeds"].get(name)
    if cached and (cached in (src.get("feeds") or []) or src.get("discover", True)):
        candidates.append(cached)  # ayarlardan çıkarılmış eski bir adresi kullanma
    candidates += [u for u in (src.get("feeds") or []) if u not in candidates]
    for url in candidates:
        entries = parse_feed(url)
        if entries:
            state["feeds"][name] = url
            return entries, url, "ok"
    if src.get("discover", True):
        for url in discover_feeds(src["site"]):
            if url in candidates:
                continue
            entries = parse_feed(url)
            if entries:
                state["feeds"][name] = url
                return entries, url, "discovered"
    url = google_news_feed(src)
    entries = parse_feed(url)
    if entries:
        return entries, url, "google"
    return [], None, "fail"


def entry_time(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def make_item(e, src, priority):
    link = e.get("link") or ""
    title = clean_text(e.get("title"))
    if "news.google.com" in link:  # Google News başlıklarındaki " - Site Adı" ekini temizle
        title = re.sub(r"(^|\s+)-\s+[^-]+$", "", title).strip()
    key = norm_link(link) if link else title.lower()
    return {
        "id": hashlib.sha1(key.encode()).hexdigest()[:16],
        "source": src["name"],
        "handle": src.get("handle", ""),
        "priority": priority,
        "title": title,
        "link": link,
        "summary": clean_text(e.get("summary") or e.get("description"), 1000),
        "time": entry_time(e),
        "image": feed_image(e),
    }


def feed_image(e):
    """RSS kaydındaki kapak görselini bulur (media:content, media:thumbnail veya enclosure)."""
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key) or []:
            if m.get("url"):
                return m["url"]
    for enc in e.get("enclosures") or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    return ""


# F1 dışı serileri yapay zekaya hiç göndermeden elemek için basit kelime filtresi
OTHER_SERIES = ["motogp", "moto2", "moto3", "superbike", "wsbk", "formula e", "formel e", "fórmula e",
                "indycar", "nascar", "wec", "le mans", "imsa", "dtm", "wrc", "rally", "rallye", "dakar",
                "gt world challenge", "elms", "supercars", "extreme e", "karting"]
F1_WORDS = ["f1", "formula 1", "formula one", "formel 1", "formula uno", "fórmula 1", "grand prix",
            "verstappen", "hamilton", "leclerc", "norris", "piastri", "russell", "antonelli",
            "alonso", "stroll", "gasly", "colapinto", "albon", "sainz", "ocon", "bearman", "hulkenberg",
            "hülkenberg", "bortoleto", "lawson", "hadjar", "lindblad", "perez", "pérez", "bottas",
            "ferrari", "mercedes", "red bull", "mclaren", "aston martin", "alpine", "williams", "haas",
            "racing bulls", "sauber", "cadillac", "wolff", "horner", "vasseur", "zak brown", "mekies"]


def looks_non_f1(it):
    text = f' {it["title"]} {it["summary"][:300]} '.lower()
    return (any(k in text for k in OTHER_SERIES)
            and not any(re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", text) for k in F1_WORDS))


def collect(cfg, state, report=None):
    items = {}
    for prio, src in enumerate(cfg["sources"]):
        try:
            entries, url, status = get_entries(src, state)
        except Exception as e:
            entries, url, status = [], None, "fail"
            log(f"  hata: {e}")
        log(f"{src['name']}: {len(entries)} haber [{status}] {url or ''}")
        if report is not None:
            report.append((src["name"], len(entries), status))
        dated = sorted(entries, key=lambda e: entry_time(e) or datetime.min.replace(tzinfo=timezone.utc),
                       reverse=True)
        for e in dated[:40]:
            it = make_item(e, src, prio)
            if it["title"]:
                items.setdefault(it["id"], it)
    return list(items.values())


# ---------------------------------------------------------------- yarış takvimi
def refresh_calendar(state):
    """Yarış takvimini günde bir kez günceller (Jolpica / Ergast verisi)."""
    last = state.get("calendar_updated")
    if last and now() - datetime.fromisoformat(last) < timedelta(hours=24):
        return
    try:
        r = http_get("https://api.jolpi.ca/ergast/f1/current.json")
        races = r.json()["MRData"]["RaceTable"]["Races"]

        def dt(d):
            t = d.get("time", "12:00:00Z").replace("Z", "+00:00")
            return datetime.fromisoformat(f'{d["date"]}T{t}')

        cal = []
        for race in races:
            race_start = dt(race)
            first = dt(race["FirstPractice"]) if "FirstPractice" in race else race_start - timedelta(days=2)
            cal.append([(first - timedelta(hours=6)).isoformat(),
                        (race_start + timedelta(hours=12)).isoformat(), race.get("raceName", "")])
        state["calendar"] = cal
        state["calendar_updated"] = now().isoformat()
        log(f"Takvim güncellendi: {len(cal)} yarış")
    except Exception as e:
        log(f"Takvim alınamadı: {e}")


def race_status(cfg, state):
    mode = str(cfg.get("settings", {}).get("race_weekend", "auto")).lower()
    current = next((c for c in state["calendar"]
                    if datetime.fromisoformat(c[0]) <= now() <= datetime.fromisoformat(c[1])), None)
    upcoming = next((c for c in sorted(state["calendar"])
                     if datetime.fromisoformat(c[0]) > now()), None)
    active = mode == "on" or (mode == "auto" and current is not None)
    return active, current, upcoming


# ---------------------------------------------------------------- Claude
def ask_claude(client, model, prompt, max_tokens, effort="low", thinking=True):
    """effort: modelin ne kadar uğraşacağı (low = ucuz ve hızlı). thinking=False: düşünmeyi kapatır."""
    extra = {"output_config": {"effort": effort}}
    if not thinking:
        extra["thinking"] = {"type": "disabled"}
    resp = client.messages.create(model=model, max_tokens=max_tokens,
                                  messages=[{"role": "user", "content": prompt}], extra_body=extra)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    if not text.strip():
        kinds = [getattr(b, "type", "?") for b in resp.content]
        raise ValueError(f"Claude boş cevap döndü (stop_reason={resp.stop_reason}, bloklar={kinds})")
    return text


def extract_json(text, opener):
    text = re.sub(r"```(?:json)?", "", text)
    closer = "]" if opener == "[" else "}"
    start, end = text.find(opener), text.rfind(closer)
    if start == -1 or end == -1:
        raise ValueError("JSON bulunamadı: " + text[:200])
    return json.loads(text[start:end + 1])


def triage(client, model, items, stories, effort="low"):
    """Her haber için puan, hikaye ve gelişme bilgisini döndürür."""
    recent = sorted(stories.items(), key=lambda kv: kv[1]["last"], reverse=True)[:80]
    story_txt = "\n".join(f'{sid} {"[posted] " if s["posted"] else ""}{s["title"]}'
                           for sid, s in recent) or "(none)"
    results = {}
    for c in range(0, len(items), 50):
        chunk = items[c:c + 50]
        lines = "\n".join(f'{i}. [{it["source"]}] {it["title"]} | {it["summary"][:250]}'
                          for i, it in enumerate(chunk))
        prompt = TRIAGE_PROMPT.replace("<<STORIES>>", story_txt).replace("<<ITEMS>>", lines)
        for d in extract_json(ask_claude(client, model, prompt, 8000, effort=effort), "["):
            i = int(d.get("i", -1))
            if 0 <= i < len(chunk):
                results[chunk[i]["id"]] = {
                    "score": int(d.get("score", 1)),
                    "story": str(d.get("story", "")).strip() or f"N-solo-{i}",
                    "title": (d.get("title") or "").strip(),
                    "update": bool(d.get("update", False)),
                    "chunk": c,
                }
    return results


def assign_stories(state, items, tri):
    """Triage sonuçlarını kalıcı hikayelere bağlar, kaynak zamanlarını kaydeder."""
    stories, new_ids = state["stories"], {}
    for it in items:
        r = tri.get(it["id"])
        if not r:
            continue
        key = r["story"]
        if key in stories:
            sid = key
        else:
            nk = (r["chunk"], key)
            if nk not in new_ids:
                state["story_seq"] += 1
                sid = f"S{state['story_seq']}"
                new_ids[nk] = sid
                stories[sid] = {"title": r["title"] or it["title"], "first": now().isoformat(),
                                "sources": {}, "posted": False, "alerted": False}
            sid = new_ids[nk]
        s = stories[sid]
        t = (it["time"] or now()).isoformat()
        prev = s["sources"].get(it["source"])
        s["sources"][it["source"]] = min(prev, t) if prev else t
        s["last"] = now().isoformat()
        it.update(score=r["score"], story=sid, update=r["update"])


def trending_count(story, window_min):
    cut = now() - timedelta(minutes=window_min)
    return sum(1 for t in story["sources"].values() if datetime.fromisoformat(t) >= cut)


def select(items, state, s):
    """Her hikayeden en iyi haberi seçer: yeni hikayeler, yükselenler ve önemli gelişmeler."""
    min_score, window = s.get("min_score", 3), s.get("trend_window_min", 45)
    trend_min, trend_score = s.get("trend_min_sources", 4), s.get("trend_min_score", 5)
    best = {}
    for it in items:
        if "story" not in it:
            continue
        story = state["stories"][it["story"]]
        trending = trending_count(story, window) >= trend_min
        if story["posted"]:
            ok = it["update"] and it["score"] >= min_score
        else:
            ok = it["score"] >= min_score or (trending and it["score"] >= trend_score)
        if not ok:
            continue
        cur = best.get(it["story"])
        if cur is None or (it["score"], -it["priority"]) > (cur["score"], -cur["priority"]):
            best[it["story"]] = it
    chosen = sorted(best.values(), key=lambda x: (-x["score"], x["priority"]))
    return chosen[: s.get("max_posts_per_run", 15)]


def fix_prefix(s):
    s = (s or "").strip()
    return s if s.startswith(PREFIX) else PREFIX + s.lstrip("🚨| ").strip()


def write_post(client, model, it):
    text = it["summary"] or "(özet yok, sadece başlık var)"
    prompt = (WRITE_PROMPT.replace("<<SOURCE>>", it["source"])
              .replace("<<TITLE>>", it["title"]).replace("<<TEXT>>", text))
    data = extract_json(ask_claude(client, model, prompt, 1500, thinking=False), "{")
    n = len(data.get("post_tr", ""))
    if n > 400:  # bir kez kısaltma iste
        retry = (prompt + f"\n\nÖnceki metin {n} karakterdi, sınırı aşıyor. Önceki cevap:\n"
                 f"{json.dumps(data, ensure_ascii=False)}\nKuralları koruyarak en fazla 400 karaktere "
                 "kısalt. Sadece JSON döndür.")
        data = extract_json(ask_claude(client, model, retry, 1500, thinking=False), "{")
    data["post_tr"] = fix_prefix(data.get("post_tr"))
    return data


# ---------------------------------------------------------------- Telegram
def tg_send(text, image=""):
    """Mesaj gönderir; image verilirse kapak görseli mesajın üstünde büyük önizleme olarak görünür."""
    if not (TG_TOKEN and TG_CHAT):
        log("Telegram ayarları eksik. Mesaj:\n" + text)
        return
    preview = ({"url": image, "prefer_large_media": True, "show_above_text": True}
               if image else {"is_disabled": True})
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                                "link_preview_options": preview}, timeout=20)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            log(f"Telegram hatası: {r.status_code} {r.text[:300]}")
        return


def alert(state, msg, hours=3):
    """Hata uyarılarını en fazla 3 saatte bir gönder."""
    last = state.get("last_alert")
    if last and now() - datetime.fromisoformat(last) < timedelta(hours=hours):
        log("Uyarı (sessiz): " + msg)
        return
    state["last_alert"] = now().isoformat()
    tg_send("⚠️ " + html.escape(msg))


def format_msg(it, p, header=""):
    e = html.escape
    tr = p["post_tr"]
    status = {"official": "✅ <b>Resmi</b>", "report": "🟡 <b>İddia / Haber</b>"}.get(p.get("status"), "")
    if p.get("type") == "quote":
        status = "💬 <b>Demeç</b>"
    msg = (header + (status + "\n" if status else "") +
           f'<pre>{e(tr)}\n\n📰 {e(it["handle"])}</pre>\n'
           f'🔗 <a href="{e(it["link"], quote=True)}">{e(it["source"])}</a>'
           f' · {len(tr)} karakter · önem {it.get("score", "-")}/10')
    if len(tr) > 400:
        msg += "\n⚠️ 400 karakteri aşıyor, kısaltman gerekebilir"
    return msg


def trend_line(story, window):
    names = sorted(story["sources"], key=lambda n: story["sources"][n])
    first = names[0] if names else "?"
    return (f"🔥 <b>YÜKSELEN HABER</b> · son {window} dk'da {trending_count(story, window)} kaynak\n"
            f"İlk veren: {html.escape(first)} · Diğerleri: {html.escape(', '.join(names[1:]))}\n\n")


# ---------------------------------------------------------------- modes
def run(cfg, state):
    s = cfg.get("settings", {})
    model = s.get("model", "claude-haiku-5-5")
    window = s.get("trend_window_min", 45)
    refresh_calendar(state)
    items = collect(cfg, state)
    stamp = now().isoformat()
    state["last_run"] = stamp

    if not state["initialized"]:  # ilk çalışma: mevcut haberleri gönderme, sadece işaretle
        for it in items:
            state["seen"][it["id"]] = stamp
        state["initialized"] = True
        tg_send(f"✅ Bot kuruldu. Mevcut {len(items)} haber işaretlendi; "
                "bundan sonra sadece yeni haberler gelecek.")
        return

    new = [it for it in items if it["id"] not in state["seen"]]
    max_age = timedelta(hours=s.get("max_age_hours", 6))
    fresh = [it for it in new if it["time"] is None or now() - it["time"] <= max_age]
    for it in new:
        state["seen"][it["id"]] = stamp
    skipped = [it for it in fresh if looks_non_f1(it)]
    fresh = [it for it in fresh if not looks_non_f1(it)]
    log(f"Yeni: {len(new)} | taze: {len(fresh)} | F1 dışı diye elenen: {len(skipped)}")
    for it in skipped:
        log(f"  elendi: [{it['source']}] {it['title'][:80]}")
    if not fresh:
        return

    client = Anthropic()
    try:
        tri = triage(client, model, fresh, state["stories"], s.get("triage_effort", "low"))
    except Exception as e:
        for it in fresh:  # kaybolmasın, sonraki çalışmada tekrar denensin
            state["seen"].pop(it["id"], None)
        alert(state, f"Haber puanlama başarısız: {e}")
        return
    assign_stories(state, fresh, tri)

    chosen = select(fresh, state, s)
    log(f"Seçilen: {len(chosen)}")
    trend_min = s.get("trend_min_sources", 4)
    for it in chosen:
        story = state["stories"][it["story"]]
        header = ""
        if trending_count(story, window) >= trend_min and not story["alerted"]:
            header = trend_line(story, window)
            story["alerted"] = True
        elif story["posted"]:
            header = "🔄 <b>GELİŞME</b>\n\n"
        try:
            p = write_post(client, model, it)
            tg_send(format_msg(it, p, header))
        except Exception as e:
            log(f"Özet hatası: {e}")
            tg_send(header + f'⚠️ Özet üretilemedi: <a href="{html.escape(it["link"], quote=True)}">'
                    f'{html.escape(it["title"])}</a>')
        story["posted"] = True

    # Daha önce paylaşılmış ama şimdi yükselen hikayeler için kısa uyarı
    for sid, story in state["stories"].items():
        if story["posted"] and not story["alerted"] and trending_count(story, window) >= trend_min:
            story["alerted"] = True
            tg_send(trend_line(story, window) + f"Paylaştığın haber büyüyor: {html.escape(story['title'])}")


def test(cfg, state):
    refresh_calendar(state)
    report = []
    items = collect(cfg, state, report)
    icon = {"ok": "✅", "discovered": "✅", "google": "🟡", "fail": "❌"}
    note = {"discovered": " (RSS otomatik bulundu)",
            "google": " (kendi RSS'i bulunamadı, Google News yedeği kullanılıyor)",
            "fail": " (haber alınamadı)"}
    lines = ["<b>Kaynak testi</b>"]
    for name, n, status in report:
        lines.append(f"{icon[status]} {html.escape(name)}: {n} haber{note.get(status, '')}")
    active, current, upcoming = race_status(cfg, state)
    if active:
        lines.append(f"\n🏁 Yarış haftasonu modu: AÇIK ({html.escape(current[2] if current else 'elle açıldı')})")
    else:
        nxt = f" · sonraki: {html.escape(upcoming[2])} ({upcoming[0][:10]})" if upcoming else ""
        lines.append(f"\n🏁 Yarış haftasonu modu: kapalı{nxt}")
    tg_send("\n".join(lines))

    dated = sorted([i for i in items if i["time"]], key=lambda x: x["time"], reverse=True)
    if dated:
        it = dated[0]
        model = cfg.get("settings", {}).get("model", "claude-haiku-5-5")
        p = write_post(Anthropic(), model, it)
        tg_send("<b>Örnek özet (en yeni haber):</b>\n\n" + format_msg(it, p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="run", choices=["run", "test"])
    args = ap.parse_args()
    cfg, state = load_config(), load_state()
    try:
        test(cfg, state) if args.mode == "test" else run(cfg, state)
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
