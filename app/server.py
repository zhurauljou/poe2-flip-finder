#!/usr/bin/env python3
"""PoE 2 Currency Flip Finder — локальный сервер.

Тянет курсы биржи валют Path of Exile 2 (почасовые сводки GGG или суточные
данные poe.ninja), кэширует их на диск
и отдаёт браузерному интерфейсу (index.html), который ищет выгодные циклы обмена.
Только стандартная библиотека Python 3.8+.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ggg
import notify
import update

HERE = os.path.dirname(os.path.abspath(__file__))
# Корень папки программы — там запускаторы и инструкция; сама программа лежит в app/.
# (Версия 1.0 лежала прямо в корне — тогда корень и есть папка программы.)
ROOT = os.path.dirname(HERE) if os.path.basename(HERE) == "app" else HERE
CACHE_DIR = os.path.join(HERE, "cache")


def _data_dir():
    """Личные данные (токен бота, подписчики, журнал сделок) — в профиле пользователя, а не в папке программы:
    так они не уедут в OneDrive вместе с рабочим столом и не попадут в архив для передачи."""
    if os.environ.get("POE2FLIP_DATA"):
        return os.environ["POE2FLIP_DATA"]
    if os.name == "nt" and os.environ.get("APPDATA"):
        return os.path.join(os.environ["APPDATA"], "poe2-flip")
    return os.path.join(os.path.expanduser("~"), ".config", "poe2-flip")


DATA_DIR = _data_dir()
PRIVATE_FILES = ("journal.json", "notify.json", "subscribers.json")


def migrate_private():
    """Старые версии хранили личные файлы в cache/ — переносим."""
    import shutil
    for name in PRIVATE_FILES:
        old, new = os.path.join(CACHE_DIR, name), os.path.join(DATA_DIR, name)
        if os.path.exists(old) and not os.path.exists(new):
            os.makedirs(DATA_DIR, exist_ok=True)
            shutil.move(old, new)
PORT = int(os.environ.get("POE2FLIP_PORT", "8765"))
# 127.0.0.1 — только этот компьютер; 0.0.0.0 — ещё и телефон/планшет в той же Wi-Fi сети.
# Переключается кнопкой 📱 на странице без перезапуска; выбор запоминается в настройках (app.json).
LOCAL, LAN = "127.0.0.1", "0.0.0.0"

VERSION = "1.0.5"
API = "https://poe.ninja/poe2/api"
UA = "poe2-flip-finder/%s (personal desktop tool)" % VERSION

# Категории биржи на poe.ninja. Пустые/несуществующие просто пропускаются.
CATEGORIES = [
    "Currency", "Fragments", "Runes", "Essences", "Ritual", "Expedition",
    "Delirium", "Breach", "Abyss", "SoulCores", "UncutGems",
    "LineageSupportGems", "Idols", "Verisium",
]

state_lock = threading.Lock()
state = {"running": False, "progress": 0, "total": 0, "message": "", "error": None}


def http_get_json(url, retries=4):
    delay = 2.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            if e.code == 404:
                return None
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


def http_get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (" + UA + ")"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def q(s):
    return urllib.parse.quote(s, safe="")


def cache_path(league):
    """Кэш данных poe.ninja по лиге."""
    safe = "".join(c if c.isalnum() else "_" for c in league)
    return os.path.join(CACHE_DIR, "market_%s.json" % safe)


def set_state(**kw):
    with state_lock:
        state.update(kw)


def fetch_market(league, min_volume):
    """Собирает все пары обмена по лиге и сохраняет в кэш."""
    try:
        set_state(running=True, progress=0, total=0, error=None, message="Загружаю списки категорий…")
        items = {}      # id -> {name, image, category, detailsId, volume}
        jobs = []       # (category, detailsId)
        core_ids = set()
        for cat in CATEGORIES:
            data = http_get_json("%s/economy/exchange/current/overview?league=%s&type=%s" % (API, q(league), cat))
            if not data or not data.get("lines"):
                continue
            core = data.get("core", {})
            for it in core.get("items", []):
                core_ids.add(it["id"])
                items.setdefault(it["id"], dict(it, volume=None))
            vol = {ln["id"]: ln.get("volumePrimaryValue") or 0 for ln in data["lines"]}
            for it in data.get("items", []):
                v = vol.get(it["id"], 0)
                items[it["id"]] = dict(it, volume=v)
                if v >= min_volume or it["id"] in core_ids:
                    jobs.append((cat, it["detailsId"]))
        # Divine — основная валюта, в lines её нет; берём её пары отдельно.
        for it in list(items.values()):
            if it["id"] in core_ids and ("Currency", it["detailsId"]) not in jobs:
                jobs.append(("Currency", it["detailsId"]))

        set_state(total=len(jobs), message="Загружаю пары обмена…")
        pairs = {}  # "a|b" (a<b) -> {from, to, rate(to per 1 from), volume}
        done = 0

        def one(job):
            cat, did = job
            return http_get_json("%s/economy/exchange/current/details?league=%s&type=%s&id=%s"
                                 % (API, q(league), cat, q(did)))

        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(one, j) for j in jobs]
            for f in as_completed(futs):
                done += 1
                set_state(progress=done)
                try:
                    d = f.result()
                except Exception:
                    continue
                if not d or "item" not in d:
                    continue
                src = d["item"]["id"]
                for p in d.get("pairs", []):
                    rate = p.get("rate") or 0
                    if rate <= 0:
                        continue
                    dst = p["id"]
                    a, b = sorted([src, dst])
                    key = a + "|" + b
                    if key in pairs:
                        continue
                    hist = p.get("history") or []
                    prev = hist[1].get("rate") if len(hist) > 1 else None
                    week = [h.get("volumePrimaryValue") or 0 for h in hist[:7]]
                    vol7 = sum(week) / len(week) if week else None
                    pairs[key] = {"from": src, "to": dst, "rate": rate, "prev": prev,
                                  "volume": p.get("volumePrimaryValue") or 0, "vol7": vol7}

        market = {
            "league": league,
            "fetchedAt": time.time(),
            "minVolume": min_volume,
            "core": sorted(core_ids),
            "items": items,
            "pairs": list(pairs.values()),
        }
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = cache_path(league) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(market, fh, ensure_ascii=False)
        os.replace(tmp, cache_path(league))
        set_state(running=False, message="Готово: %d валют, %d пар" % (len(items), len(pairs)))
    except Exception as e:
        set_state(running=False, error=str(e), message="Ошибка загрузки")


def fetch_ggg(league):
    """Докачивает почасовые сводки GGG, справочник названий и иконки."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        set_state(running=True, progress=0, total=0, error=None, message="Загружаю справочник предметов…")
        ggg.load_names(CACHE_DIR, http_get_json)
        ggg.ninja_meta(CACHE_DIR, league, http_get_json, API, CATEGORIES)
        set_state(message="Загружаю комиссии в золоте…")
        ggg.load_gold_fees(CACHE_DIR, http_get_text)
        set_state(message="Загружаю часовые сводки GGG…")
        hours = ggg.sync_hours(CACHE_DIR, http_get_json, lambda d, t: set_state(progress=d, total=t))
        if not hours:
            raise RuntimeError("GGG не вернул ни одной часовой сводки")
        set_state(running=False, message="Готово: сводок за %d ч" % len(hours))
    except Exception as e:
        set_state(running=False, error=str(e), message="Ошибка загрузки")


_ggg_memo = {}
last_league = {"name": None}  # лига, которую последней смотрела страница, — её сервер и обновляет сам


def ggg_market(league, window):
    hours = ggg.cached_hours(CACHE_DIR)
    if not hours:
        return None
    key = (league, window, tuple(hours))
    if key not in _ggg_memo:
        names = ggg.load_names(CACHE_DIR, http_get_json)
        meta = ggg.ninja_meta(CACHE_DIR, league, http_get_json, API, CATEGORIES)
        gold = ggg.load_gold_fees(CACHE_DIR, http_get_text)
        _ggg_memo.clear()
        _ggg_memo[key] = json.dumps(ggg.build_market(CACHE_DIR, league, window, hours, names, meta, gold),
                                    ensure_ascii=False).encode("utf-8")
    return _ggg_memo[key]


# ---------- фоновое обновление и Telegram ----------
# Раз в 5 минут сервер сам докачивает почасовые сводки GGG (даже без открытой вкладки) и, если включён Telegram,
# ищет новые пути по настройкам, которые прислала страница.
AUTO_EVERY = 300


def link_base():
    """Ссылка «Открыть в программе»: с телефона работает, если включён доступ с телефона."""
    host = "127.0.0.1"
    if net["host"] != LOCAL:
        ips = lan_ips()
        host = ips[0] if ips else host
    return "http://%s:%d/" % (host, PORT)


_leagues = {"t": 0, "list": []}


def leagues_list():
    """Лиги с биржей (список poe.ninja), кэш на час."""
    if not _leagues["list"] or time.time() - _leagues["t"] > 3600:
        try:
            d = http_get_json("%s/data/index-state" % API) or {}
            names = [l["name"] for l in d.get("economyLeagues", [])]
            if names:
                _leagues.update(t=time.time(), list=names)
        except Exception:
            pass
    return _leagues["list"]


def default_league():
    st = notify.load_cfg(DATA_DIR).get("settings") or {}
    return st.get("league") or last_league["name"] or (leagues_list() or [None])[0]


_parsed = {}


def market_dict(league, window):
    """Рынок GGG как словарь — для бота (подписчики могут смотреть разные лиги)."""
    hours = ggg.cached_hours(CACHE_DIR)
    if not hours or not league:
        return None
    key = (league, window, hours[0], len(hours))
    if key not in _parsed:
        if len(_parsed) > 8:
            _parsed.clear()
        body = ggg_market(league, max(1, min(24, window)))
        _parsed[key] = json.loads(body) if body else None
    return _parsed[key]


def update_loop():
    """Проверка обновлений: вскоре после запуска и дальше раз в 6 часов."""
    time.sleep(10)
    while True:
        update.check(UA)
        time.sleep(6 * 3600)


def background():
    last = 0
    while True:
        time.sleep(20)
        try:
            league = default_league()
            if not league or time.time() - last < AUTO_EVERY:
                continue
            last = time.time()
            with state_lock:
                busy = state["running"]
                if not busy:
                    state["running"] = True
            if not busy:
                fetch_ggg(league)  # сводки GGG общие для всех лиг — хватит одной загрузки
            notify.check_all(DATA_DIR, market_dict, link_base(), league)
        except Exception as e:
            notify.status["last_error"] = str(e)


# ---------- журнал сделок ----------
# Хранится на сервере, а не в браузере: общий для компьютера и телефона и не пропадает при очистке браузера.
JOURNAL = os.path.join(DATA_DIR, "journal.json")
JOURNAL_MAX = 5000  # самые старые записи сверх этого отбрасываются
journal_lock = threading.Lock()


def journal_read():
    try:
        with open(JOURNAL, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def journal_change(op):
    """op: {"op": "add", "trade": {...}} или {"op": "delete", "id": "..."}; возвращает журнал после изменения."""
    with journal_lock:
        items = journal_read()
        if op.get("op") == "add" and isinstance(op.get("trade"), dict) and isinstance(op["trade"].get("id"), str):
            items = [t for t in items if t.get("id") != op["trade"]["id"]] + [op["trade"]]
            items = items[-JOURNAL_MAX:]
        elif op.get("op") == "delete":
            items = [t for t in items if t.get("id") != op.get("id")]
        else:
            raise ValueError("bad journal op")
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = JOURNAL + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(items, fh, ensure_ascii=False)
        os.replace(tmp, JOURNAL)
        return items


class Server(ThreadingHTTPServer):
    # На Windows SO_REUSEADDR позволяет второй копии занять тот же порт рядом с первой — тогда запросы
    # продолжают уходить в старый процесс. Без него повторный запуск честно упадёт в «уже работает».
    allow_reuse_address = os.name != "nt"


# --- Онлайн PoE 2 в Steam: сейчас (Steam Web API) и пик за 24 часа (SteamCharts, иначе наши замеры).
# Игроки отдельного клиента GGG сюда не попадают.
STEAM_APPID = 2694490
PL_CURRENT_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid=%d" % STEAM_APPID
PL_CHARTS_URL = "https://steamcharts.com/app/%d" % STEAM_APPID
PL_CURRENT_TTL = 300
PL_PEAK_TTL = 1800
PL_DAY = 86400

_pl_lock = threading.Lock()
_pl_s = {"now": None, "now_t": 0, "peak": None, "peak_t": 0, "samples": None, "error": None}


def _pl_get(url, ua):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def _pl_load(cache_dir):
    if _pl_s["samples"] is None:
        try:
            with open(os.path.join(cache_dir, "players.json"), encoding="utf-8") as f:
                _pl_s["samples"] = [(int(t), int(n)) for t, n in json.load(f)]
        except (OSError, ValueError, TypeError):
            _pl_s["samples"] = []


def _pl_save(cache_dir):
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = os.path.join(cache_dir, "players.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_pl_s["samples"], f)
        os.replace(tmp, os.path.join(cache_dir, "players.json"))
    except OSError:
        pass


def _pl_charts_peak(html):
    # <span class="num">27078</span><br>24-hour peak
    m = re.search(r'class="num">\s*([\d,]+)\s*</span>\s*<br>\s*24-hour peak', html)
    return int(m.group(1).replace(",", "")) if m else None


def players_status(cache_dir, ua):
    with _pl_lock:
        _pl_load(cache_dir)
        now = time.time()
        if now - _pl_s["now_t"] > PL_CURRENT_TTL:
            _pl_s["now_t"] = now
            try:
                n = json.loads(_pl_get(PL_CURRENT_URL, ua))["response"]["player_count"]
                _pl_s.update(now=int(n), error=None)
                _pl_s["samples"] = [x for x in _pl_s["samples"] if now - x[0] < PL_DAY] + [(int(now), int(n))]
                _pl_save(cache_dir)
            except Exception as e:
                _pl_s["error"] = "нет связи со Steam (%s)" % e.__class__.__name__
        if now - _pl_s["peak_t"] > PL_PEAK_TTL:
            _pl_s["peak_t"] = now
            try:
                _pl_s["peak"] = _pl_charts_peak(_pl_get(PL_CHARTS_URL, "Mozilla/5.0 (" + ua + ")"))
            except Exception:
                _pl_s["peak"] = None
        own = [n for t, n in _pl_s["samples"] if now - t < PL_DAY]
        peak = max([p for p in [_pl_s["peak"], _pl_s["now"]] + own if p is not None], default=None)
        # «за сутки» честно, только если пик из SteamCharts или наших замеров хватает почти на сутки
        full = _pl_s["peak"] is not None or (bool(_pl_s["samples"]) and now - _pl_s["samples"][0][0] > PL_DAY - 3600)
        return {"now": _pl_s["now"], "peak24": peak, "peakFull": full, "error": _pl_s["error"],
                "url": "https://steamdb.info/app/%d/charts/" % STEAM_APPID}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = dict(urllib.parse.parse_qsl(url.query))
        if url.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # Браузер телефона иначе показывает старую страницу из кэша после обновления программы.
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif url.path in ("/icon.png", "/icon.ico", "/favicon.ico"):
            name = "icon.png" if url.path == "/icon.png" else "icon.ico"
            try:
                with open(os.path.join(HERE, name), "rb") as fh:
                    body = fh.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if name.endswith(".png") else "image/x-icon")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/leagues":
            names = leagues_list()
            if names:
                self.send_json(names)
            else:
                self.send_json({"error": "poe.ninja недоступен"}, 502)
        elif url.path == "/api/refresh":
            league = qs.get("league", "Standard")
            if qs.get("source") == "ninja":
                target, args = fetch_market, (league, float(qs.get("minVolume", "1")))
            else:
                target, args = fetch_ggg, (league,)
            with state_lock:
                busy = state["running"]
                if not busy:
                    state["running"] = True
            if not busy:
                threading.Thread(target=target, args=args, daemon=True).start()
            self.send_json({"started": not busy})
        elif url.path == "/api/journal":
            with journal_lock:
                self.send_json(journal_read())
        elif url.path == "/api/players":
            self.send_json(players_status(CACHE_DIR, UA))
        elif url.path == "/api/releases":
            self.send_json(update.releases(UA))
        elif url.path == "/api/update":
            self.send_json(update.status(VERSION))
        elif url.path == "/api/lan":
            self.send_json(lan_status())
        elif url.path == "/api/notify":
            self.send_json(notify.public_status(DATA_DIR))
        elif url.path == "/api/status":
            with state_lock:
                self.send_json(dict(state))
        elif url.path == "/api/market":
            league = qs.get("league", "Standard")
            if qs.get("source") != "ninja":
                last_league["name"] = league
            if qs.get("source") == "ninja":
                p = cache_path(league)
                if not os.path.exists(p):
                    self.send_json(None)
                    return
                with open(p, encoding="utf-8") as fh:
                    market = json.load(fh)
                gold = ggg.load_gold_fees(CACHE_DIR, http_get_text)
                for it in market["items"].values():
                    it["gold"] = gold.get(it.get("name"))
                body = json.dumps(market, ensure_ascii=False).encode("utf-8")
            else:
                try:
                    body = ggg_market(league, max(1, min(24, int(qs.get("window", "3")))))
                except Exception as e:
                    self.send_json({"error": str(e)}, 500)
                    return
                if body is None:
                    self.send_json(None)
                    return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/journal", "/api/notify", "/api/update", "/api/lan"):
            self.send_error(404)
            return
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= 256 * 1024:
            self.send_json({"error": "bad size"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(size).decode("utf-8"))
            if path == "/api/journal":
                self.send_json(journal_change(body))
            elif path == "/api/lan":
                self.send_json(set_lan(bool(body.get("enabled"))))
            elif path == "/api/update":
                if body.get("op") == "install":
                    self.send_json(update.install(ROOT, PACK_FILES, VERSION, UA))
                else:
                    update.check(UA)
                    self.send_json(update.status(VERSION))
            else:
                self.send_json(notify.handle(DATA_DIR, body))
        except (ValueError, AttributeError, RuntimeError) as e:
            self.send_json({"error": str(e)}, 400)


# ---------- доступ с телефона ----------
def app_settings():
    try:
        with open(os.path.join(DATA_DIR, "app.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_app_settings(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "app.json"), "w", encoding="utf-8") as fh:
        json.dump(d, fh)


net = {"host": LOCAL, "srv": None, "rebind": False, "error": None}


def lan_status():
    on = net["host"] != LOCAL
    return {"enabled": on, "port": PORT, "urls": ["http://%s:%d/" % (ip, PORT) for ip in lan_ips()] if on else [],
            "error": net["error"]}


def set_lan(on):
    """Включить/выключить доступ с телефона: сервер переоткрывается на другом адресе, программа не перезапускается."""
    want = LAN if on else LOCAL
    d = app_settings()
    d["lan"] = bool(on)
    save_app_settings(d)
    if want != net["host"] and net["srv"]:
        net.update(host=want, rebind=True, error=None)
        threading.Timer(0.3, net["srv"].shutdown).start()  # ответ на запрос успеет уйти
    st = lan_status()
    st.update(enabled=on, urls=["http://%s:%d/" % (ip, PORT) for ip in lan_ips()] if on else [])
    return st


def bind(host):
    """Открыть сервер на адресе; сразу после закрытия прежнего порт может быть занят — пробуем несколько раз."""
    err = None
    for _ in range(40):
        try:
            return Server((host, PORT), Handler)
        except OSError as e:
            err = e
            time.sleep(0.25)
    raise err


def print_lan():
    ips = lan_ips() or ["<IP компьютера>"]
    print()
    print("  Доступ с телефона включён. На телефоне (в той же Wi-Fi сети) откройте:")
    for ip in ips:
        print("    http://%s:%d/" % (ip, PORT))
    if len(ips) > 1:
        print("  (если адресов несколько — нужен тот, что от Wi-Fi роутера, обычно 192.168.x.x)")
    print("  Не открывается — разрешите Python в брандмауэре Windows и отключите VPN")
    print("  или включите в нём доступ к локальной сети.")
    print()


def lan_ips():
    """Адреса этого компьютера в домашней сети (192.168.*, 10.*, 172.16-31.*), домашний Wi-Fi обычно первым.
    Адреса VPN тоже попадают сюда — поэтому печатаем все, а не угадываем один."""
    import socket
    try:
        found = {ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        return []
    def private(ip):
        a, b = (int(x) for x in ip.split(".")[:2])
        return a == 192 and b == 168 or a == 10 or a == 172 and 16 <= b <= 31
    return sorted((ip for ip in found if private(ip)), key=lambda ip: (not ip.startswith("192.168."), ip))


# Что кладётся в архив для людей (и в обновление): запускаторы и инструкция в корне, программа — в app/.
# Без кэша и личных файлов (они и так не в папке программы). Пути — относительно корня.
PACK_FILES = ["Start (Windows).bat", "Start (Mac).command", "How to start.txt",
              "app/index.html", "app/server.py", "app/ggg.py", "app/notify.py", "app/update.py",
              "app/icon.ico", "app/icon.png"]


def pack():
    import zipfile
    out = os.path.join(ROOT, "poe2-flip.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in PACK_FILES:
            path = os.path.join(ROOT, name)
            if not os.path.exists(path):
                raise SystemExit("Нет файла %s — архив не собран" % name)
            info = zipfile.ZipInfo("poe2-flip/" + name, time.localtime(os.path.getmtime(path))[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if name.endswith(".command") else 0o644) << 16  # Mac: запускаемый файл
            with open(path, "rb") as fh:
                z.writestr(info, fh.read())
    print("Готово: %s (версия %s)" % (out, VERSION))
    print("В архиве только файлы программы — без кэша, журнала, токена бота и подписчиков.")


# Устаревшие файлы прошлых версий — пути от корня программы (можно и «app/…»). Обновление только добавляет и
# заменяет файлы, поэтому всё переименованное или удалённое надо дописать сюда — тогда оно уберётся при запуске.
# Первые строки — раскладка 1.0, где всё лежало в корне.
OBSOLETE = ["index.html", "server.py", "ggg.py", "notify.py", "update.py",
                  "Запустить (Windows).bat", "Запустить (Mac).command", "Как запустить.txt",
                  "Запустить для телефона (Windows).bat", "Запустить для телефона (Mac).command",
                  "Собрать архив для передачи (Windows).bat", "Собрать архив для передачи (Mac).command"]


def tidy_old_layout():
    if ROOT == HERE:
        return
    import shutil
    for rel in OBSOLETE:
        if rel in PACK_FILES:  # на всякий случай: никогда не удаляем файлы текущей версии
            continue
        try:
            os.remove(os.path.join(ROOT, *rel.split("/")))
        except OSError:
            pass
    shutil.rmtree(os.path.join(ROOT, "__pycache__"), ignore_errors=True)
    old_cache = os.path.join(ROOT, "cache")
    if not os.path.isdir(old_cache):
        return
    for name in PRIVATE_FILES:  # личное (если старая версия держала его в cache) — в папку личных данных
        src, dst = os.path.join(old_cache, name), os.path.join(DATA_DIR, name)
        if os.path.exists(src) and not os.path.exists(dst):
            os.makedirs(DATA_DIR, exist_ok=True)
            shutil.move(src, dst)
    if not os.path.exists(CACHE_DIR):
        shutil.move(old_cache, CACHE_DIR)  # скачанные курсы переезжают вместе с программой
    else:
        shutil.rmtree(old_cache, ignore_errors=True)  # в app/cache уже есть свои — старые скачаются заново при нужде


# ---------- ярлык с иконкой (Windows) ----------
# У .bat нельзя задать свою иконку — Windows всегда рисует терминал. Поэтому рядом создаём ярлык
# «PoE 2 Flip Finder» с иконкой Divine Orb. Ярлык хранит полный путь, поэтому его не кладут в архив,
# а создают здесь — и пересоздают, если папку перенесли.
SHORTCUT = "PoE 2 Flip Finder.lnk"


def ensure_shortcut():
    if os.name != "nt" or ROOT == HERE:
        return
    bat, ico, lnk = os.path.join(ROOT, "Start (Windows).bat"), os.path.join(HERE, "icon.ico"), os.path.join(ROOT, SHORTCUT)
    if not (os.path.exists(bat) and os.path.exists(ico)):
        return
    st = app_settings()
    if os.path.exists(lnk) and st.get("shortcut_root") == ROOT:
        return
    import subprocess
    # Пути — через переменные окружения: так не мешают пробелы и кириллица в пути.
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:PF_LNK);"
          "$s.TargetPath=$env:PF_BAT;$s.WorkingDirectory=$env:PF_ROOT;$s.IconLocation=$env:PF_ICO+',0';"
          "$s.Description='PoE 2 Flip Finder';$s.Save()")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=60, capture_output=True,
                       env=dict(os.environ, PF_LNK=lnk, PF_BAT=bat, PF_ROOT=ROOT, PF_ICO=ico),
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return
    if os.path.exists(lnk):
        st["shortcut_root"] = ROOT
        save_app_settings(st)


def main():
    if "--pack" in sys.argv:
        pack()
        return
    if ROOT == HERE and os.path.exists(os.path.join(HERE, "app", "server.py")):
        # Папка ещё в раскладке 1.0, а новая версия уже лежит в app/ — запускаем её (она же уберёт старые файлы).
        import subprocess
        sys.exit(subprocess.call([sys.executable, os.path.join(HERE, "app", "server.py")] + sys.argv[1:]))
    url = "http://127.0.0.1:%d/" % PORT
    host = os.environ.get("POE2FLIP_HOST") or (LAN if app_settings().get("lan") else LOCAL)
    try:
        srv = Server((host, PORT), Handler)
    except OSError:
        # Уже запущен — просто открываем окно.
        print("Сервер уже работает, открываю %s" % url)
        webbrowser.open(url)
        return
    net["host"] = host
    print("PoE 2 Flip Finder %s запущен: %s" % (VERSION, url))
    print("Закройте это окно, чтобы остановить.")
    tidy_old_layout()
    migrate_private()
    threading.Thread(target=ensure_shortcut, daemon=True).start()
    threading.Thread(target=background, daemon=True).start()
    threading.Thread(target=update_loop, daemon=True).start()
    threading.Thread(target=notify.poll_loop, daemon=True,
                     args=(DATA_DIR, {"leagues": leagues_list, "default_league": default_league, "market": market_dict})).start()
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    while True:
        net["srv"] = srv
        if net["host"] != LOCAL:
            print_lan()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            break
        srv.server_close()
        if not net["rebind"]:
            break
        net["rebind"] = False
        try:
            srv = bind(net["host"])
            if net["host"] == LOCAL:
                print("Доступ с телефона выключен.")
        except OSError as e:
            net.update(error=str(e), host=LOCAL)
            srv = bind(LOCAL)


if __name__ == "__main__":
    main()
