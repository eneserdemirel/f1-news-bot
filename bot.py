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
from urllib.parse import urljoin, urlparse, urlunparse

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

# ---------------------------------------------------------------- prompts
TRIAGE_PROMPT = """You are the news editor of a fast Formula 1 breaking-news account.
Below are new headlines from several sites (some may be in Italian or Dutch).

Stories already posted in the last 48 hours:
<<RECENT>>

New items:
<<ITEMS>>

For EVERY new item return one JSON object with:
- "i": the item number
- "score": importance 1-5 for an F1 breaking-news account
   5 = major breaking news: confirmed driver or team principal moves, contracts, sackings,
       big penalties or disqualifications, serious crashes or injuries, legal cases,
       official FIA/F1 decisions, race or qualifying results
   4 = significant: credible reports about the above, important technical or rule changes,
       notable statements from key figures on hot topics
   3 = moderately interesting
   2 = minor quotes, previews, routine updates
   1 = filler, opinion pieces, listicles, quizzes, betting, merchandise,
       or NOT about Formula 1 (other series, road cars)
- "group": a number; items about the same story get the same group number
- "already_posted": true if it is the same story as one already posted with no significant new development
Return only a JSON array, nothing else."""

WRITE_PROMPT = """You write ready-to-post Formula 1 news updates for a social media account.

Source site: <<SOURCE>>
Headline: <<TITLE>>
Article text:
<article>
<<TEXT>>
</article>

Rules:
1. Stay strictly faithful to the article. Do not add facts, background, speculation, opinions
   or hype that are not in the article. If the article reports a rumour, keep it framed as a
   report ("reportedly", "according to ...").
2. Choose the post type:
   - "quote": the story is mainly someone's statement or interview. Format:
     🚨 | Name: "quote"
     Copy the most important part of the quote word for word from the article. Do not reword it
     and do not merge separate quotes. Keep it to one or two complete sentences: never cut it
     mid-thought to create suspense (no clickbait), and do not make it long. If needed, add very
     short context before the colon, e.g. 🚨 | Wolff on Antonelli's lawsuit: "..."
     Only use quotes that appear in the article. If the quote is not in English, translate it
     faithfully and keep it in quotation marks.
   - "news": everything else, written as an English summary.
3. Length: a single simple fact -> max 280 characters. Several important details -> max 400
   characters. Never exceed 400. The count includes "🚨 | ".
4. Start exactly with "🚨 | ". No hashtags, no other emojis, no links, no source line.
5. Also produce:
   - "post_tr": a faithful Turkish translation of the post, also starting with "🚨 | "
   - "title_en": a punchy 1-2 word English image title (3 words only if natural English needs it)
   - "title_tr": the Turkish equivalent of the title
   - "original_source": if the article says the news first came from another outlet
     (e.g. "via Corriere dello Sport"), that outlet's name; otherwise ""
Return only JSON:
{"type": "...", "post_en": "...", "post_tr": "...", "title_en": "...", "title_tr": "...", "original_source": "..."}"""


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
    st.setdefault("seen", {})    # haber id -> ilk görülme zamanı
    st.setdefault("sent", [])    # son 48 saatte gönderilen hikayeler
    st.setdefault("feeds", {})   # kaynak adı -> çalışan RSS adresi
    st.setdefault("last_alert", "")
    return st


def save_state(st):
    seen_cut = now() - timedelta(days=14)
    st["seen"] = {k: v for k, v in st["seen"].items() if datetime.fromisoformat(v) > seen_cut}
    sent_cut = now() - timedelta(hours=48)
    st["sent"] = [s for s in st["sent"] if datetime.fromisoformat(s["time"]) > sent_cut]
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


def google_news_feed(site):
    domain = urlparse(site).netloc
    domain = domain[4:] if domain.startswith("www.") else domain
    return f"https://news.google.com/rss/search?q=site:{domain}+when:1d&hl=en-GB&gl=GB&ceid=GB:en"


def get_entries(src, state):
    """Sırayla: daha önce çalışan feed -> config'teki feed'ler -> otomatik bulma -> Google News."""
    name = src["name"]
    candidates = []
    if state["feeds"].get(name):
        candidates.append(state["feeds"][name])
    candidates += [u for u in (src.get("feeds") or []) if u not in candidates]
    for url in candidates:
        entries = parse_feed(url)
        if entries:
            state["feeds"][name] = url
            return entries, url, "ok"
    for url in discover_feeds(src["site"]):
        if url in candidates:
            continue
        entries = parse_feed(url)
        if entries:
            state["feeds"][name] = url
            return entries, url, "discovered"
    url = google_news_feed(src["site"])
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
    key = norm_link(link) if link else title.lower()
    return {
        "id": hashlib.sha1(key.encode()).hexdigest()[:16],
        "source": src["name"],
        "handle": src.get("handle", ""),
        "priority": priority,
        "title": title,
        "link": link,
        "summary": clean_text(e.get("summary") or e.get("description"), 400),
        "time": entry_time(e),
    }


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
        for e in entries[:40]:
            it = make_item(e, src, prio)
            if it["title"]:
                items.setdefault(it["id"], it)
    return list(items.values())


# ---------------------------------------------------------------- Claude
def ask_claude(client, model, prompt, max_tokens):
    resp = client.messages.create(model=model, max_tokens=max_tokens,
                                  messages=[{"role": "user", "content": prompt}])
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


def triage(client, model, items, recent_titles):
    results = {}
    recent = "\n".join(f"- {t}" for t in recent_titles[-80:]) or "(none)"
    for c in range(0, len(items), 50):
        chunk = items[c:c + 50]
        lines = "\n".join(f'{i}. [{it["source"]}] {it["title"]} | {it["summary"][:250]}'
                          for i, it in enumerate(chunk))
        prompt = TRIAGE_PROMPT.replace("<<RECENT>>", recent).replace("<<ITEMS>>", lines)
        for d in extract_json(ask_claude(client, model, prompt, 8000), "["):
            i = int(d.get("i", -1))
            if 0 <= i < len(chunk):
                results[chunk[i]["id"]] = {
                    "score": int(d.get("score", 1)),
                    "group": f'{c}-{d.get("group", i)}',
                    "posted": bool(d.get("already_posted", False)),
                }
    return results


def select(items, tri, min_score, max_posts):
    """Her hikayeden tek haber: en yüksek puanlı, eşitse listede üstte olan kaynak."""
    best = {}
    for it in items:
        r = tri.get(it["id"])
        if not r or r["posted"] or r["score"] < min_score:
            continue
        it["score"] = r["score"]
        cur = best.get(r["group"])
        if cur is None or (it["score"], -it["priority"]) > (cur["score"], -cur["priority"]):
            best[r["group"]] = it
    chosen = sorted(best.values(), key=lambda x: (-x["score"], x["priority"]))
    return chosen[:max_posts]


def article_text(it):
    txt = ""
    try:
        r = http_get(it["link"])
        txt = trafilatura.extract(r.text, include_comments=False, include_tables=False) or ""
    except Exception as e:
        log(f"  makale okunamadı: {e}")
    if len(txt) < 300:  # okunamadıysa RSS özetiyle devam
        txt = (it["summary"] + "\n" + txt).strip()
    return txt[:7000]


def fix_prefix(s):
    s = (s or "").strip()
    return s if s.startswith(PREFIX) else PREFIX + s.lstrip("🚨| ").strip()


def write_post(client, model, it, text):
    prompt = (WRITE_PROMPT.replace("<<SOURCE>>", it["source"])
              .replace("<<TITLE>>", it["title"]).replace("<<TEXT>>", text))
    data = extract_json(ask_claude(client, model, prompt, 4000), "{")
    n = len(data.get("post_en", ""))
    if n > 400:  # bir kez kısaltma iste
        retry = (prompt + f"\n\nYour previous post_en was {n} characters, over the limit. "
                 f"Previous answer:\n{json.dumps(data, ensure_ascii=False)}\n"
                 "Rewrite it so post_en is at most 400 characters (280 if it is a single simple "
                 "fact), keeping every rule. Return only JSON.")
        data = extract_json(ask_claude(client, model, retry, 4000), "{")
    data["post_en"] = fix_prefix(data.get("post_en"))
    data["post_tr"] = fix_prefix(data.get("post_tr"))
    return data


# ---------------------------------------------------------------- Telegram
def tg_send(text):
    if not (TG_TOKEN and TG_CHAT):
        log("Telegram ayarları eksik. Mesaj:\n" + text)
        return
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=20)
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


def format_msg(it, p):
    e = html.escape
    handle = e(it["handle"])
    en, tr = p["post_en"], p["post_tr"]
    msg = (f'🖼 Görsel başlığı: <code>{e(p.get("title_en", ""))}</code> · '
           f'<code>{e(p.get("title_tr", ""))}</code>\n\n'
           f'<pre>{e(en)}\n\n📰 {handle}</pre>\n\n'
           f'🇹🇷\n<pre>{e(tr)}\n\n📰 {handle}</pre>\n\n'
           f'🔗 <a href="{e(it["link"], quote=True)}">{e(it["source"])}</a>'
           f' · {len(en)} karakter · önem {it.get("score", "-")}/5')
    if p.get("original_source"):
        msg += f'\n↪️ Haberin ilk kaynağı: {e(p["original_source"])}'
    if len(en) > 400:
        msg += "\n⚠️ 400 karakteri aşıyor, kısaltman gerekebilir"
    return msg


# ---------------------------------------------------------------- modes
def run(cfg, state):
    s = cfg.get("settings", {})
    model = s.get("model", "claude-haiku-5-5")
    items = collect(cfg, state)
    stamp = now().isoformat()

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
    log(f"Yeni: {len(new)} | taze: {len(fresh)}")
    if not fresh:
        return

    client = Anthropic()
    try:
        tri = triage(client, model, fresh, [x["title"] for x in state["sent"]])
    except Exception as e:
        for it in fresh:  # kaybolmasın, sonraki çalışmada tekrar denensin
            state["seen"].pop(it["id"], None)
        alert(state, f"Haber puanlama başarısız: {e}")
        return

    chosen = select(fresh, tri, s.get("min_score", 4), s.get("max_posts_per_run", 8))
    log(f"Seçilen: {len(chosen)}")
    for it in chosen:
        try:
            p = write_post(client, model, it, article_text(it))
            tg_send(format_msg(it, p))
            state["sent"].append({"title": f'{p.get("title_en", "")}: {it["title"]}', "time": stamp})
        except Exception as e:
            log(f"Özet hatası: {e}")
            tg_send(f'⚠️ Özet üretilemedi: <a href="{html.escape(it["link"], quote=True)}">'
                    f'{html.escape(it["title"])}</a>')
            state["sent"].append({"title": it["title"], "time": stamp})


def test(cfg, state):
    report = []
    items = collect(cfg, state, report)
    icon = {"ok": "✅", "discovered": "✅", "google": "🟡", "fail": "❌"}
    note = {"discovered": " (RSS otomatik bulundu)",
            "google": " (kendi RSS'i bulunamadı, Google News yedeği kullanılıyor)",
            "fail": " (haber alınamadı)"}
    lines = ["<b>Kaynak testi</b>"]
    for name, n, status in report:
        lines.append(f"{icon[status]} {html.escape(name)}: {n} haber{note.get(status, '')}")
    tg_send("\n".join(lines))

    dated = sorted([i for i in items if i["time"]], key=lambda x: x["time"], reverse=True)
    if dated:
        it = dated[0]
        model = cfg.get("settings", {}).get("model", "claude-haiku-5-5")
        p = write_post(Anthropic(), model, it, article_text(it))
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
