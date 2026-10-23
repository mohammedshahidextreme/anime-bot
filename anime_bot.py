#!/usr/bin/env python3
"""
Anime update bot for a Telegram channel.

Har naye episode / naye anime par channel me ek post jaati hai:
poster + title + language + rating + category (genres).

Data source: AniList GraphQL API (free, API key nahi chahiye).
Koi video / episode file upload nahi hoti - sirf updates.

Commands:
  python anime_bot.py selftest   # offline format test (network nahi chahiye)
  python anime_bot.py hello      # channel me ek test message bhejta hai
  python anime_bot.py once       # ek baar check karke nayi posts bhejta hai
  python anime_bot.py run        # hamesha chalta rahe, har POLL_MINUTES par check
"""
import datetime
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
STATE = os.path.join(DATA, "state.json")
ANILIST = "https://graphql.anilist.co"

QUERY = """
query ($start: Int, $end: Int, $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    airingSchedules(airingAt_greater: $start, airingAt_lesser: $end, sort: TIME) {
      id
      airingAt
      episode
      media {
        id
        siteUrl
        isAdult
        format
        episodes
        averageScore
        countryOfOrigin
        genres
        title { romaji english native }
        coverImage { extraLarge large }
      }
    }
  }
}
"""


# ---------- setup ----------
def load_env():
    path = os.path.join(BASE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v:
                os.environ.setdefault(k.strip(), v)


def env(name, default=""):
    # khali value (jaise GitHub me set na kiya variable) ko bhi default maano
    return os.environ.get(name) or default


# ---------- http ----------
def http_json(url, payload, timeout=30):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "anime-channel-bot/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except Exception:
            return {"ok": False, "error_code": e.code, "description": body[:200]}
    except Exception as e:  # network down, timeout, etc.
        return {"ok": False, "error_code": 0, "description": str(e)[:200]}


def tg(method, payload):
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN .env me set nahi hai.")
    url = f"https://api.telegram.org/bot{token}/{method}"
    res = {"ok": False}
    for _ in range(3):
        res = http_json(url, payload)
        if res.get("ok"):
            return res
        if res.get("error_code") == 429:  # Telegram rate limit
            wait = (res.get("parameters") or {}).get("retry_after", 5)
            time.sleep(wait + 1)
            continue
        return res
    return res


# ---------- post format ----------
def tag(genre):
    return "#" + re.sub(r"[^A-Za-z0-9]", "", genre)


def build_caption(item):
    m = item["media"]
    t = m.get("title") or {}
    title = t.get("english") or t.get("romaji") or t.get("native") or "Unknown"
    ep = item["episode"]
    total = m.get("episodes")

    head = "🆕 <b>NEW ANIME</b>" if ep == 1 else "📺 <b>NEW EPISODE</b>"
    ep_line = f"🎞 Episode {ep}" + (f" / {total}" if total else "")

    score = m.get("averageScore")
    rating = f"{score / 10:.1f}/10" if score else "N/A"

    genres = m.get("genres") or []
    genre_text = ", ".join(genres) if genres else "N/A"
    tags = " ".join(tag(g) for g in genres)

    lines = [
        head,
        "",
        f"<b>{html.escape(title)}</b>",
        ep_line,
        f"🗣 Language: {html.escape(env('ANIME_LANGUAGE', 'Japanese (Sub)'))}",
        f"⭐ Rating: {rating}",
        f"🎭 Category: {html.escape(genre_text)}",
    ]
    if tags:
        lines += ["", tags]
    caption = "\n".join(lines)
    return caption[:1024]  # Telegram photo caption limit


def post_item(item, caption=None):
    channel = env("TELEGRAM_CHANNEL")
    if not channel:
        sys.exit("TELEGRAM_CHANNEL .env me set nahi hai (jaise @mychannel).")
    caption = caption or build_caption(item)
    markup = {"inline_keyboard": [[{"text": "Website", "url": "https://sugoix.shop/"}]]}
    cover = item["media"].get("coverImage") or {}
    poster = cover.get("extraLarge") or cover.get("large")

    res = {"ok": False}
    if poster:
        res = tg("sendPhoto", {
            "chat_id": channel, "photo": poster,
            "caption": caption, "parse_mode": "HTML",
            "reply_markup": markup,
        })
        if not res.get("ok"):
            print("[tg] photo fail:", res.get("description"))
    if not res.get("ok"):  # fallback: text-only post
        res = tg("sendMessage", {
            "chat_id": channel, "text": caption, "parse_mode": "HTML",
            "reply_markup": markup,
        })
    return bool(res.get("ok")), res


# ---------- Hindi dub schedule (India release) ----------
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SPEC_RE = re.compile(
    r"^(weekly\s+|daily\s+)?(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}))?(?:\s+ep(\d+))?"
    r"(?:\s+per(\d+))?(?:\s+x(\d+))?(?:\s+tot(\d+))?(?:\s+sn(\d+))?(?:\s+lang=(\S+))?(?:\s+dub=(\S+))?$", re.I)
META = {}  # anime naam -> {"total": N, "season": N, "langs": [..]}

SEARCH_QUERY = """
query ($q: String) {
  Page(perPage: 5) {
    media(search: $q, type: ANIME, sort: SEARCH_MATCH) {
      id siteUrl isAdult episodes averageScore genres
      title { romaji english native }
      coverImage { extraLarge large }
    }
  }
}
"""


def _words(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _read_lines():
    try:
        with open(os.path.join(BASE, "hindi_list.txt"), encoding="utf-8") as f:
            return [ln.strip() for ln in f]
    except FileNotFoundError:
        return []


def load_hindi_list():
    """hindi_list.txt ki har line: [weekly|daily] YYYY-MM-DD [HH:MM] [epN] [perN] [xN] | Anime naam"""
    entries = []
    for ln in _read_lines():
        if not ln or ln.startswith("#") or ln.startswith("@") or "|" not in ln:
            continue
        spec, name = [x.strip() for x in ln.split("|", 1)]
        m = SPEC_RE.match(spec)
        if not m or not name:
            print("[warn] list ki line samajh nahi aayi:", ln)
            continue
        try:
            hh, mm = (m.group(3) or "0:0").split(":")
            start = datetime.datetime.strptime(m.group(2), "%Y-%m-%d").replace(
                hour=int(hh), minute=int(mm), tzinfo=IST)
        except ValueError:
            print("[warn] date/time galat:", ln)
            continue
        kind = (m.group(1) or "").strip().lower()
        step = 7 if kind == "weekly" else 1 if kind == "daily" else 0
        ep0 = int(m.group(4)) if m.group(4) else None
        per = max(1, int(m.group(5))) if m.group(5) else 1
        count = int(m.group(6)) if m.group(6) else (None if step else 1)
        entries.append((name, start, step, count, ep0, per))
        META[name] = {
            "total": int(m.group(7)) if m.group(7) else None,
            "season": int(m.group(8)) if m.group(8) else None,
            "langs": [x for x in (m.group(9) or "").split(",") if x],
            "dub": (m.group(10) or "").replace("_", " "),
        }
    return entries


def hindi_due(entries, now, window_hours=48):
    """Jin releases ka time aa chuka hai (aur 48 ghante se purana nahi)."""
    due = []
    for name, start, step, count, ep0, per in entries:
        n = 0
        while True:
            when = start + datetime.timedelta(days=step * n)
            if when > now or (count is not None and n >= count):
                break
            if (now - when).total_seconds() <= window_hours * 3600:
                due.append((f"{name}|{when:%Y-%m-%d}", name,
                            (ep0 + n * per, ep0 + n * per + per - 1) if ep0 else None))
            n += 1
            if not step:
                break
    return due


def find_media(name):
    res = http_json(ANILIST, {"query": SEARCH_QUERY, "variables": {"q": name}})
    try:
        for m in res["data"]["Page"]["media"]:
            if not m.get("isAdult"):
                return m
    except Exception:
        pass
    return None


ROMAN = {"II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}


def season_label(*names):
    """Naam se 'Season 4' / 'Stage 1' nikalta hai (nahi mila to None)."""
    for n in names:
        n = n or ""
        m = re.search(r"\bseason\s*(\d+)", n, re.I) or re.search(r"\b(\d+)(?:st|nd|rd|th)\s+season", n, re.I)
        if m:
            return f"Season {m.group(1)}"
        m = re.search(r"\b(\d+)(?:st|nd|rd|th)\s+stage", n, re.I)
        if m:
            return f"Stage {m.group(1)}"
        m = re.search(r"\bpart\s*(\d+)", n, re.I)
        if m:
            return f"Part {m.group(1)}"
        m = re.search(r"\b(II|III|IV|V|VI)\s*$", n.strip())
        if m:
            return f"Season {ROMAN[m.group(1)]}"
    return None


def build_hindi_caption(name, media, ep=None, meta=None):
    media = media or {}
    meta = meta or {}
    t = media.get("title") or {}
    title = t.get("english") or t.get("romaji") or name
    genres = media.get("genres") or []
    # Season: agar AniList title me nahi hai to "- Season 4" jod do
    shown = title
    if not season_label(title):
        lab = season_label(name) or (f"Season {meta['season']}" if meta.get("season") else None)
        if lab:
            shown = f"{title} - {lab}"
    langs = "-".join(meta["langs"]) if meta.get("langs") else \
        "-".join(x.strip() for x in env("ANIME_LANGUAGE", "Hindi").split(",") if x.strip())
    sub = env("ANIME_SUBTITLE", "English")

    if ep:
        ep_text = f"EPISODE {ep[0]}" if ep[0] == ep[1] else f"EPISODES {ep[0]}-{ep[1]}"
        head = f"\U0001F525\u2728 <b><i>{ep_text} HINDI ADDED!</i></b> \u2728\U0001F525"
    else:
        head = "\U0001F1EE\U0001F1F3 <b>NEW HINDI DUB</b>"

    lines = [head, "", "\U0001F3AC <b>Title:</b>", f"<i>{html.escape(shown)}</i>"]
    if genres:
        lines.append("\U0001F5E3 <b>Genre:</b> " + html.escape(" \u2022 ".join(genres)))
    score = media.get("averageScore")
    lines.append(f"\u2B50 <b>Rating:</b> {score / 10:.1f}/10" if score else "\u2B50 <b>Rating:</b> N/A")
    lines.append(f"\U0001F50A <b>Audio:</b> <i>{html.escape(langs)}</i>")
    lines.append(f"\U0001F4AC <b>Subtitle:</b> {html.escape(sub)}")
    if meta.get("dub"):
        lines.append(f"\U0001F399 <b>Dub By:</b> <i>{html.escape(meta['dub'])}</i>")
    tags = " ".join(tag(g) for g in genres)
    if tags:
        lines += ["", tags]
    return "\n".join(lines)[:1024]


# ---------- Daily schedule post ----------
def ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def releases_on(entries, day):
    out = []
    for name, start, step, count, ep0, per in entries:
        n = 0
        while True:
            when = start + datetime.timedelta(days=step * n)
            if when.date() > day or (count is not None and n >= count):
                break
            if when.date() == day:
                out.append((when, name,
                            (ep0 + n * per, ep0 + n * per + per - 1) if ep0 else None,
                            step, per))
            n += 1
            if not step:
                break
    return sorted(out, key=lambda x: x[0])


MEDIA_CACHE = {}


def cached_media(name):
    if name not in MEDIA_CACHE:
        MEDIA_CACHE[name] = find_media(name) or {}
        time.sleep(1)
    return MEDIA_CACHE[name]


def build_schedule_texts(day, rel):
    head = "\U0001F4C5 <b>" + f"{day:%A}".upper() + f" {ordinal(day.day)} {day:%B}</b>"
    blocks = []
    for when, name, ep, step, per in rel:
        meta = META.get(name, {})
        media = cached_media(name)
        label = season_label(name) or (f"Season {meta['season']}" if meta.get("season") else None)
        shown = html.escape(name) + (f" ({label})" if label and label.lower() not in name.lower() else "")
        lines = [f"\U0001F3AC <b>{shown}</b>", f"   \U0001F550 {when:%I:%M %p} IST"]
        genres = media.get("genres") or []
        if genres:
            lines.append("   \U0001F3AD " + html.escape(", ".join(genres)))
        if meta.get("langs"):
            lines.append("   \U0001F399 " + " ".join("#" + re.sub(r"\W", "", x) for x in meta["langs"]))
        if ep:
            total = meta.get("total") or media.get("episodes")
            if ep[0] > 1:  # ab tak aaya hua (latest) episode
                lines.append(f"   \U0001F4FA Episode {ep[0] - 1}" + (f" / {total}" if total else ""))
            lines.append("   \u23ED Next: " + (f"Episode {ep[0]}" if ep[0] == ep[1] else f"Episodes {ep[0]}-{ep[1]}"))
        kind = "Weekly" if step == 7 else "Daily" if step == 1 else "Once"
        lines.append(f"   \U0001F4CA {kind} | {per} ep(s)/release")
        blocks.append("\n".join(lines))
    foot = "\u2501" * 12 + "\n\U0001F4FA Daily Anime Schedule\n\U0001F310 Watch Animes Here: sugoix.shop"
    texts, cur = [], head
    for b in blocks:
        if len(cur) + len(b) + 20 > 3800:
            texts.append(cur)
            cur = head
        cur += "\n" + "\u2500" * 12 + "\n" + b
    texts.append(cur + "\n\n" + foot)
    return texts


def daily_schedule(state, now, entries):
    hour = int(env("SCHEDULE_HOUR", "8"))
    today = now.date()
    if now.hour < hour or state.get("schedule_date") == str(today):
        return True
    rel = releases_on(entries, today)
    ok = True
    if rel:
        channel = env("TELEGRAM_CHANNEL")
        for text in build_schedule_texts(today, rel):
            res = tg("sendMessage", {"chat_id": channel, "text": text, "parse_mode": "HTML",
                                     "disable_web_page_preview": True})
            if not res.get("ok"):
                print("[error] schedule post:", res.get("description"))
                ok = False
            time.sleep(2)
    if ok:
        state["schedule_date"] = str(today)
        print(f"[schedule] {len(rel)} release aaj ({today})")
    return ok


# ---------- Auto: websites se Hindi dub schedule padhna ----------
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
DAYS = {d: i for i, d in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}
DATE_RE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2}), (\d{4})")
SCHED_RE = re.compile(
    r"\b(?P<day>Mon|Tue|Wed|Thu|Fri|Sat|Sun|Daily)[a-z]*\b\s*(?:\(\d+\s*Eps?\.?\)\s*)?"
    r"[\u2022\u00b7\-]?\s*(?:\d{1,2}:\d{2}\s*/\s*)?(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ap>AM|PM)", re.I)


def load_hindi_sources():
    """hindi_list.txt ki '@source URL' aur '@dryrun' lines."""
    lines = _read_lines()
    sources = [ln.split(None, 1)[1].strip() for ln in lines
               if ln.lower().startswith("@source ") and len(ln.split(None, 1)) == 2]
    dry = any(ln.lower().startswith("@dryrun") for ln in lines)
    skips = [_words(ln.split(None, 1)[1]) for ln in lines
             if ln.lower().startswith("@skip ") and len(ln.split(None, 1)) == 2]
    return sources, dry, skips


def fetch_page(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (anime-channel-bot)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        print("[auto] page nahi khuli:", url, str(e)[:100])
        return ""


def _cell(c):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()


def _clean_name(n):
    n = re.sub(r"\(also in[^)]*\)", "", n, flags=re.I)
    n = re.sub(r"\{[^}]*\}", "", n)
    return re.sub(r"\s+", " ", n).strip(" -\u2013\u2022*")


def parse_auto_rows(page):
    """Table ki wo rows jo abhi chal rahi/aane wali hain (premiere date + din/time)."""
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        cells = [_cell(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
        if len(cells) < 3:
            continue
        text = " | ".join(cells)
        if re.search(r"\b(finished|removed|batch|ended)\b", text, re.I):
            continue
        dates = DATE_RE.findall(text)
        sm = SCHED_RE.search(text)
        if len(dates) != 1 or not sm:  # ya date nahi, ya khatam ho chuka (2 date)
            continue
        name = None
        for c in cells:
            if re.fullmatch(r"[\d\W]*", c) or DATE_RE.search(c) or SCHED_RE.search(c):
                continue
            if re.fullmatch(r"(airing|tba)", c, re.I) or re.search(r"poster\s*$", c, re.I):
                continue
            if re.fullmatch(r"(hindi|tamil|telugu|and|[, &])+", c, re.I):
                continue
            name = _clean_name(c)
            break
        if not name or re.search(r"\bEp\.?\s*\d+\s*-\s*\d+", name, re.I):
            continue  # batch / episode range wali row
        mon, day, year = dates[0]
        dk = sm.group("day").lower()[:3]
        step = 1 if dk == "dai" else 7
        hh = int(sm.group("h")) % 12 + (12 if sm.group("ap").upper() == "PM" else 0)
        start = datetime.datetime(int(year), MONTHS[mon.lower()], int(day), hh,
                                  int(sm.group("m")), tzinfo=IST)
        if step == 7 and start.weekday() != DAYS[dk]:
            print(f"[auto] din match nahi hua, chhoda: {name}")
            continue
        em = re.search(r"\((\d+)\s*Eps?\b", text, re.I)
        n_eps = int(em.group(1)) if em else None
        per = n_eps if (step == 1 and n_eps) else 1      # daily (2 Eps.) = roz 2 episode
        count = n_eps if (step == 7 and n_eps) else None  # weekly (10 Eps.) = kul 10
        ep0 = None if re.search(r"cour[\s-]*[2-9]|part[\s-]*[2-9]", name, re.I) else 1
        out.append((name, start, step, count, ep0, per))
    return out


def collect_entries():
    entries = load_hindi_list()
    sources, dry, skips = load_hindi_sources()
    if not sources:
        return entries
    auto, seen = [], set()
    for url in sources:
        for e in parse_auto_rows(fetch_page(url)):
            k = (e[0].lower(), e[1])
            if k in seen or any(sk <= _words(e[0]) for sk in skips):
                continue
            seen.add(k)
            auto.append(e)
    print(f"[auto] {len(sources)} page se {len(auto)} anime mile" + (" (DRYRUN)" if dry else ""))
    if dry:
        for n, st, step, c, ep, per in auto:
            print(f"   - {n[:48]} | {'daily' if step == 1 else st.strftime('%a')} {st:%H:%M}"
                  f" se {st:%d %b}, ep{ep or '-'}" + (f" x{c}" if c else "") + (f" per{per}" if per > 1 else ""))
        return entries  # dryrun: auto wale post nahi honge
    return entries + auto


def hindi_once():
    state = load_state()
    now = datetime.datetime.now(IST)
    window = float(env("HINDI_WINDOW_HOURS", "48"))
    posted = list(state.get("hindi_posted", []))
    entries = collect_entries()
    due = [d for d in hindi_due(entries, now, window) if d[0] not in posted]
    print(f"[hindi] {len(due)} post due")
    failed = not daily_schedule(state, now, entries)
    for key, name, ep in due:
        media = find_media(name) or {}
        ok, res = post_item({"media": media}, build_hindi_caption(name, media, ep, META.get(name)))
        if ok:
            posted.append(key)
            print(f"[post] {name}")
        else:
            failed = True
            print(f"[error] {name}: {res.get('description')}")
        time.sleep(3)
    state["hindi_posted"] = posted[-2000:]
    save_state(state)
    return not failed


# ---------- AniList ----------
def fetch_schedule(start, end):
    items, page = [], 1
    while True:
        res = http_json(ANILIST, {
            "query": QUERY,
            "variables": {"start": start, "end": end, "page": page},
        })
        if "data" not in res or not res["data"]:
            raise RuntimeError(f"AniList error: {str(res)[:200]}")
        data = res["data"]["Page"]
        items += data["airingSchedules"]
        if not data["pageInfo"]["hasNextPage"] or page >= 10:
            break
        page += 1
        time.sleep(1)
    # adult content skip
    return [i for i in items if i.get("media") and not i["media"].get("isAdult")]


# ---------- state ----------
def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(DATA, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


# ---------- main logic ----------
def once():
    if env("HINDI_ONLY", "1") == "1":  # India me Hindi dub release par post
        return hindi_once()
    state = load_state()
    now = int(time.time())
    last = state.get("last_check")
    if last is None:
        backfill = float(env("BACKFILL_HOURS", "0"))
        last = now - int(backfill * 3600)
        if backfill == 0:
            print("[info] pehli run: ab se aage ke episodes post honge.")

    start = max(last - 300, now - 86400)  # 5 min overlap, max 24 ghante peeche
    try:
        items = fetch_schedule(start, now)
    except RuntimeError as e:  # AniList me temporary dikkat: agli baar phir koshish
        print("[error]", e)
        return None  # exit code 0: bas agli baar dobara try hoga
    posted = set(state.get("posted", []))
    new = sorted((i for i in items if i["id"] not in posted),
                 key=lambda i: i["airingAt"])
    print(f"[anilist] {len(items)} aired, {len(new)} nayi")

    failed = False
    for item in new:
        ok, res = post_item(item)
        title = (item["media"]["title"].get("english")
                 or item["media"]["title"].get("romaji"))
        if ok:
            posted.add(item["id"])
            print(f"[post] {title} ep {item['episode']}")
        else:
            failed = True
            print(f"[error] {title} ep {item['episode']}: {res.get('description')}")
        time.sleep(3)  # Telegram flood limit se bachne ke liye

    if not failed:
        state["last_check"] = now
    state["posted"] = list(posted)[-2000:]
    save_state(state)
    return not failed  # True = sab theek


def run():
    minutes = float(env("POLL_MINUTES", "15"))
    print(f"[run] har {minutes:g} minute me check hoga. Rokne ke liye Ctrl+C.")
    while True:
        try:
            once()
        except Exception as e:
            print("[error]", e)
        time.sleep(minutes * 60)


def hello():
    res = tg("sendMessage", {
        "chat_id": env("TELEGRAM_CHANNEL"),
        "text": "✅ Anime bot connected. Ab yahan updates aayenge.",
    })
    print("OK, channel me message gaya." if res.get("ok")
          else f"Fail: {res.get('description')}")


def selftest():
    sample = {
        "id": 1, "airingAt": 0, "episode": 1,
        "media": {
            "episodes": 12, "averageScore": 82,
            "genres": ["Action", "Slice of Life", "Sci-Fi"],
            "title": {"english": "Tom & Jerry <Test>", "romaji": "x", "native": "y"},
            "coverImage": {"extraLarge": "https://example.com/p.jpg"},
        },
    }
    c1 = build_caption(sample)
    assert "NEW ANIME" in c1 and "Episode 1 / 12" in c1
    assert "8.2/10" in c1 and "#SliceofLife" in c1 and "#SciFi" in c1
    assert "Tom &amp; Jerry &lt;Test&gt;" in c1  # HTML escaped

    sample["episode"] = 5
    sample["media"].update({"episodes": None, "averageScore": None, "genres": []})
    c2 = build_caption(sample)
    assert "NEW EPISODE" in c2 and "Episode 5" in c2 and "/" not in c2.split("Episode 5")[1].split("\n")[0]
    assert "N/A" in c2

    sample["media"]["genres"] = ["Action"] * 300
    assert len(build_caption(sample)) <= 1024

    print("--- nayi anime post ---")
    sample["episode"] = 1
    sample["media"].update({"episodes": 12, "averageScore": 82,
                            "genres": ["Action", "Romance", "Drama"]})
    print(build_caption(sample))
    print("\nselftest OK")


if __name__ == "__main__":
    load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "once":
        # exit code 1 = post nahi ja saki (GitHub Actions tab me laal dikhega)
        sys.exit(0 if once() is not False else 1)
    {"selftest": selftest, "hello": hello, "run": run}.get(
        cmd, lambda: print(__doc__))()
