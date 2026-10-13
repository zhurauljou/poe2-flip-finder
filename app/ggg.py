"""Официальный API биржи валют GGG (почасовые сводки, без авторизации).

https://www.pathofexile.com/developer/docs/reference#currency-exchange
Каждый час GGG публикует сводку по всем парам биржи: сколько каждой валюты
сменило владельца и сколько выставлено в заявках. Сводка за час появляется
примерно через 5 минут после его окончания.
"""
import html
import json
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

CX_URL = "https://web.poecdn.com/api/currency-exchange/poe2/%d"
REPOE_URL = "https://repoe-fork.github.io/poe2/base_items.min.json"
HOURS_KEPT = 25
HIST_HOURS = 24

DIVINE = "Metadata/Items/Currency/CurrencyModValues"
EXALTED = "Metadata/Items/Currency/CurrencyAddModToRare"
CHAOS = "Metadata/Items/Currency/CurrencyRerollRare"
CORE = {DIVINE: "divine", EXALTED: "exalted", CHAOS: "chaos"}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)


def load_names(cache_dir, get_json):
    """metadata id -> английское название предмета (из выгрузки игровых данных RePoE)."""
    path = os.path.join(cache_dir, "repoe_names.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 7 * 86400:
        return _read(path)
    data = get_json(REPOE_URL) or {}
    names = {k: v.get("name") for k, v in data.items() if v.get("name")}
    if names:
        _write(path, names)
        return names
    return _read(path) if os.path.exists(path) else {}


GOLD_URL = "https://poe2db.tw/us/Currency_Exchange"


def load_gold_fees(cache_dir, get_text):
    """Название -> комиссия биржи в золоте за 1 штуку, когда этот предмет ты ПОЛУЧАЕШЬ («Хочу»).

    Источник — поле GoldPurchaseFee игровой таблицы CurrencyExchange в отображении poe2db
    (сама RePoE эту таблицу не выгружает). Значения меняются только с патчами — обновляем раз в неделю.
    """
    path = os.path.join(cache_dir, "gold_fees.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 7 * 86400:
        return _read(path)
    fees = {}
    try:
        page = get_text(GOLD_URL) or ""
        for m in re.finditer(r'<a class="[^"]*"[^>]*href="[^"]*">([^<]+)</a><span>(\d+)</span>', page):
            fees.setdefault(html.unescape(m.group(1)).strip(), int(m.group(2)))
    except Exception:
        pass
    if fees:
        _write(path, fees)
        return fees
    return _read(path) if os.path.exists(path) else {}


def display_name(meta, names):
    name = names.get(meta) or meta.rsplit("/", 1)[-1]
    m = re.search(r"GemUncut(\d+)$", meta)
    if m and "Level" not in name:
        name = "%s (Level %s)" % (name, m.group(1))
    return name


def latest_hour():
    """Начало последнего завершённого часа (его сводка уже должна быть опубликована)."""
    return int(time.time()) // 3600 * 3600 - 3600


def sync_hours(cache_dir, get_json, progress):
    """Докачивает недостающие часовые сводки. Возвращает отсортированный (новые первыми) список часов."""
    hdir = os.path.join(cache_dir, "ggg_hours")
    os.makedirs(hdir, exist_ok=True)
    last = latest_hour()
    wanted = [last - i * 3600 for i in range(HOURS_KEPT)]
    missing = [h for h in wanted if not os.path.exists(os.path.join(hdir, "%d.json" % h))]

    def one(h):
        try:
            d = get_json(CX_URL % h)
        except Exception:
            return  # сеть моргнула — докачаем при следующем обновлении
        markets = (d or {}).get("markets") or []
        if not markets:
            return  # час ещё не опубликован — попробуем позже
        compact = {}
        for m in markets:
            a, b = m["market_pair"]
            vt, st = m.get("volume_traded") or {}, m.get("highest_stock") or {}
            compact.setdefault(m["league"], []).append(
                [a, b, vt.get(a, 0), vt.get(b, 0), st.get(a, 0), st.get(b, 0)])
        _write(os.path.join(hdir, "%d.json" % h), compact)

    done = 0
    progress(0, len(missing))
    with ThreadPoolExecutor(max_workers=4) as ex:
        for _ in ex.map(one, missing):
            done += 1
            progress(done, len(missing))

    for fn in os.listdir(hdir):
        if fn.endswith(".json") and int(fn[:-5]) < last - 48 * 3600:
            os.remove(os.path.join(hdir, fn))
    return cached_hours(cache_dir)


def cached_hours(cache_dir):
    """Часы, сводки которых лежат на диске, — последние HOURS_KEPT, новые первыми."""
    hdir = os.path.join(cache_dir, "ggg_hours")
    if not os.path.isdir(hdir):
        return []
    hours = sorted((int(fn[:-5]) for fn in os.listdir(hdir) if fn.endswith(".json")), reverse=True)
    return [h for h in hours if h > hours[0] - HOURS_KEPT * 3600] if hours else []


def ninja_meta(cache_dir, league, get_json, api, categories):
    """Название -> {id, image, category} с poe.ninja — только ради иконок и категорий."""
    safe = "".join(c if c.isalnum() else "_" for c in league)
    path = os.path.join(cache_dir, "ninja_meta_%s.json" % safe)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        return _read(path)
    meta = {}
    for cat in categories:
        try:
            d = get_json("%s/economy/exchange/current/overview?league=%s&type=%s"
                         % (api, urllib.parse.quote(league, safe=""), cat))
        except Exception:
            continue
        for it in (d or {}).get("items", []):
            meta.setdefault(it["name"], {"id": it["id"], "image": it.get("image"), "category": cat})
    if meta:
        _write(path, meta)
    elif os.path.exists(path):
        meta = _read(path)
    return meta


def _sum(hour_files, hours, league):
    """(a, b) -> [объём a, объём b] за указанные часы; плюс остатки в заявках за самый свежий час."""
    acc, stock = {}, {}
    for i, h in enumerate(hours):
        for a, b, va, vb, sa, sb in hour_files[h].get(league, []):
            if a > b:
                a, b, va, vb, sa, sb = b, a, vb, va, sb, sa
            s = acc.setdefault((a, b), [0, 0])
            s[0] += va
            s[1] += vb
            if i == 0:
                stock[(a, b)] = (sa, sb)
    return acc, stock


def _values(day):
    """Цена каждой валюты в Divine по суточным объёмам: сначала прямые пары с Divine, потом через Exalted/Chaos."""
    val = {DIVINE: 1.0}
    pairs = sorted(day.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))
    for allowed in ({DIVINE}, {EXALTED, CHAOS}, None):
        changed = True
        while changed:
            changed = False
            for (a, b), (va, vb) in pairs:
                if va <= 0 or vb <= 0 or (a in val) == (b in val):
                    continue
                known, other = (a, b) if a in val else (b, a)
                if allowed is not None and known not in allowed:
                    continue
                vk, vo = (va, vb) if known == a else (vb, va)
                val[other] = val[known] * vk / vo
                changed = True
    return val


def build_market(cache_dir, league, window, hours, names, meta, gold):
    hdir = os.path.join(cache_dir, "ggg_hours")
    hour_files = {h: _read(os.path.join(hdir, "%d.json" % h)) for h in hours}
    win_hours, day_hours = hours[:window], hours[:24]
    win, stock = _sum(hour_files, win_hours, league)
    day, _ = _sum(hour_files, day_hours, league)
    val = _values(day)

    # Почасовой курс каждой пары за последние HIST_HOURS часов (старые первыми) — для мини-графиков.
    hist_hours = list(reversed(hours[:HIST_HOURS]))
    per_hour = [_sum(hour_files, [h], league)[0] for h in hist_hours]

    def history(a, b):
        out = []
        for acc in per_hour:
            va, vb = acc.get((a, b), (0, 0))
            out.append(float("%.6g" % (vb / va)) if va > 0 and vb > 0 else None)
        return out

    ids = {}

    def item_id(m):
        if m not in ids:
            ids[m] = CORE.get(m) or (meta.get(display_name(m, names)) or {}).get("id") or m.split("/Items/", 1)[-1]
        return ids[m]

    def div_volume(a, b, va, vb, nhours):
        parts = [x for x in (va * val[a] if a in val else None, vb * val[b] if b in val else None) if x is not None]
        return sum(parts) / len(parts) * 24 / max(1, nhours) if parts else 0

    items, pairs = {}, []
    for (a, b), (va, vb) in win.items():
        if va <= 0 or vb <= 0:
            continue
        da, db = day.get((a, b), (0, 0))
        sa, sb = stock.get((a, b), (0, 0))
        pairs.append({
            "from": item_id(a), "to": item_id(b), "rate": vb / va,
            "prev": db / da if da > 0 and db > 0 else None,
            "volume": round(div_volume(a, b, va, vb, len(win_hours)), 3),
            "vol7": round(div_volume(a, b, da, db, len(day_hours)), 3),
            "stockFrom": sa, "stockTo": sb, "hist": history(a, b),
        })
        for m in (a, b):
            if item_id(m) not in items:
                name = display_name(m, names)
                nm = meta.get(name) or {}
                items[item_id(m)] = {"id": item_id(m), "name": name, "image": nm.get("image"),
                                     "category": nm.get("category") or m.split("/")[2],
                                     "value": val.get(m), "gold": gold.get(name)}
    return {
        "source": "ggg", "league": league, "window": len(win_hours),
        "fetchedAt": (hours[0] + 3600) if hours else time.time(),
        "periodStart": win_hours[-1] if win_hours else None,
        "periodEnd": (hours[0] + 3600) if hours else None,
        "core": sorted(c for c in CORE.values() if c in items),
        "items": items, "pairs": pairs,
    }
