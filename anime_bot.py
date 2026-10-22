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


def post_item(item):
    channel = env("TELEGRAM_CHANNEL")
    if not channel:
        sys.exit("TELEGRAM_CHANNEL .env me set nahi hai (jaise @mychannel).")
    caption = build_caption(item)
    cover = item["media"].get("coverImage") or {}
    poster = cover.get("extraLarge") or cover.get("large")

    res = {"ok": False}
    if poster:
        res = tg("sendPhoto", {
            "chat_id": channel, "photo": poster,
            "caption": caption, "parse_mode": "HTML",
        })
        if not res.get("ok"):
            print("[tg] photo fail:", res.get("description"))
    if not res.get("ok"):  # fallback: text-only post
        res = tg("sendMessage", {
            "chat_id": channel, "text": caption, "parse_mode": "HTML",
        })
    return bool(res.get("ok")), res


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
