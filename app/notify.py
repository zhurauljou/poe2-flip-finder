"""Telegram-бот: уведомления о новых выгодных путях — для владельца и для подписчиков.

Работает на сервере, без открытой вкладки. Владелец подключает себя кодом со страницы (/link 123456), его условия —
фильтры страницы. Остальные пишут боту /start и настраивают свои условия кнопками прямо в чате.
Поиск путей — перенос логики страницы (index.html: buildGraph / findPaths / evalCycles / pathStreak) для циклов
и путей без «Целых чисел» и без фильтров по золоту.

Токен, подписчики и их настройки лежат в папке данных пользователя (не в папке программы).
Только стандартная библиотека.
"""
import html
import http.client
import json
import math
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

NOISE_FALLBACK = 0.03   # запас для пары почти без истории (как NOISE.fallback на странице)
REPEAT_AFTER = 7200     # путь, пропавший больше чем на 2 ч, при возвращении считается новым
MAX_IN_MESSAGE = 5
MAX_SUBSCRIBERS = 500

_lock = threading.RLock()
status = {"last_check": None, "last_sent": None, "last_error": None, "found": None}

# Условия подписчика по умолчанию; остальное (запас, объём, …) — как у страницы по умолчанию.
SUB_DEFAULT = {"base": "divine", "capital": "10", "maxLen": "3", "notifyPct": "5", "notifyStreak": "2", "window": "3",
               "fee": "1", "noise": "1", "minVol": "100", "minProfit": "1", "maxProfit": "30", "share": "25", "overhead": "2"}
CAPITALS = [5, 10, 25, 50, 100, 250]
PCTS = [2, 5, 10, 15]
STREAKS = [0, 1, 2, 4, 8]


# ---------- хранение ----------
def _path(data_dir, name):
    return os.path.join(data_dir, name)


def _read(data_dir, name, default):
    try:
        with open(_path(data_dir, name), encoding="utf-8") as fh:
            v = json.load(fh)
        return v if isinstance(v, type(default)) else default
    except (OSError, ValueError):
        return default


def _write(data_dir, name, obj):
    os.makedirs(data_dir, exist_ok=True)
    tmp = _path(data_dir, name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, _path(data_dir, name))


def load_cfg(data_dir):
    return _read(data_dir, "notify.json", {})


def save_cfg(data_dir, cfg):
    _write(data_dir, "notify.json", cfg)


def load_subs(data_dir):
    return _read(data_dir, "subscribers.json", {})


def save_subs(data_dir, subs):
    _write(data_dir, "subscribers.json", subs)


def public_status(data_dir):
    """Состояние для страницы — без токена."""
    cfg, subs = load_cfg(data_dir), load_subs(data_dir)
    subs.pop(str(cfg.get("chat_id")), None)
    return {"configured": bool(cfg.get("token")), "bot": cfg.get("bot"), "linked": bool(cfg.get("chat_id")),
            "enabled": bool(cfg.get("enabled")), "link_code": None if cfg.get("chat_id") else cfg.get("link_code"),
            "open": bool(cfg.get("open")), "subscribers": sum(1 for s in subs.values() if s.get("active")),
            "subscribers_total": len(subs), **status}


# ---------- Telegram ----------
def tg_call(token, method, params=None, timeout=20):
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (token, method),
                                 data=json.dumps(params or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode("utf-8"))
        except ValueError:
            raise RuntimeError("Telegram: HTTP %d" % e.code)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        # Обрыв соединения, сброс VPN, таймаут чтения, битый ответ — всё это «нет связи», а не падение программы.
        raise RuntimeError("нет связи с Telegram (%s)" % (getattr(e, "reason", None) or e.__class__.__name__))
    if not d.get("ok"):
        desc = d.get("description", "ошибка")
        if d.get("error_code") in (401, 404):
            desc = ("неверный токен бота — скопируй его из @BotFather целиком" if method == "getMe"
                    else "токен бота не действует (отозван или неверный) — отключи Telegram и подключи заново")
        err = RuntimeError("Telegram: %s" % desc)
        err.code = d.get("error_code")
        raise err
    return d["result"]


def handle(data_dir, op):
    """Действия со страницы владельца: token / settings / open / test / disconnect."""
    with _lock:
        cfg = load_cfg(data_dir)
        kind = op.get("op")
        if kind == "token":
            token = str(op.get("token") or "").strip()
            me = tg_call(token, "getMe")  # проверяем токен
            if me.get("username") != cfg.get("bot"):
                save_subs(data_dir, {})  # другой бот — старые подписчики к нему не относятся
            # Владелец подтверждает себя кодом: так чужой человек, написавший боту первым, не станет владельцем.
            cfg = {"token": token, "bot": me.get("username"), "settings": cfg.get("settings"), "enabled": False,
                   "open": cfg.get("open", True), "link_code": "%06d" % random.randrange(10 ** 6)}
        elif kind == "settings":
            if isinstance(op.get("settings"), dict):
                cfg["settings"] = op["settings"]
            if "enabled" in op:
                cfg["enabled"] = bool(op["enabled"])
            _watch.pop("owner", None)  # новые условия — сначала молча запоминаем текущую выдачу
        elif kind == "open":
            cfg["open"] = bool(op.get("open"))
        elif kind == "test":
            if not cfg.get("chat_id"):
                raise RuntimeError("Telegram ещё не подключён")
            tg_call(cfg["token"], "sendMessage", {"chat_id": cfg["chat_id"], "text": T(_owner_lang(cfg))["test"]})
        elif kind == "disconnect":
            cfg = {"settings": cfg.get("settings"), "bot": cfg.get("bot"), "open": cfg.get("open", True)}
        else:
            raise ValueError("bad notify op")
        save_cfg(data_dir, cfg)
    return public_status(data_dir)


# ---------- тексты ----------
TEXTS = {
    "ru": {"head": "🟢 Новый путь", "holds": "держится {n}", "liq": "ликвидность {v} div", "more": "…и ещё {n}",
           "open": "Открыть в программе", "test": "🔔 Тестовое уведомление PoE 2 Flip Finder — всё работает.",
           "h": "ч", "min": "мин", "d": "дн",
           "linked": "✅ Ты подключён как владелец бота. Условия уведомлений берутся с твоей страницы программы.",
           "owner": "Ты владелец: условия берутся со страницы программы (фильтры, «Прибыль от», «Держится от»). Команды: /top — лучшие пути сейчас, /stop — пауза, /start — включить.",
           "ownerOn": "✅ Уведомления включены. Пришлю новые пути по условиям со страницы программы. /top — лучшие сейчас, /stop — пауза.",
           "welcome": "👋 Привет! Я присылаю новые выгодные пути обмена валюты в Path of Exile 2 — по курсам реальных сделок (почасовые данные GGG).\n\nНастрой условия кнопками ниже. Команды: /top — лучшие пути сейчас, /settings — настройки, /stop — пауза.\n\n⚠️ Это средние курсы, а не стакан: перед обменом сверяй курс у Альвы.",
           "closed": "Бот сейчас закрыт для новых подписчиков.",
           "full": "Слишком много подписчиков — попробуй позже.",
           "stopped": "⏸ Уведомления на паузе. /start — включить снова.",
           "menu": "⚙️ <b>Настройки</b>\nЛига: <b>{league}</b>\nСтартовая: <b>{base}</b> · капитал <b>{cap}</b>\nОбменов: до <b>{maxLen}</b>\nПрибыль от <b>{pct}%</b> · держится от <b>{streak} ч</b>\n{state}",
           "on": "✅ Присылаю новые пути", "off": "⏸ Пауза",
           "bLeague": "Лига", "bCap": "Капитал", "bPct": "Прибыль от", "bStreak": "Держится", "bLen": "Обменов",
           "bEdit": "✏️", "bPause": "⏸ Пауза", "bResume": "▶ Включить", "bTop": "🔝 Лучшие сейчас",
           "nStop": "⏸ Остановить уведомления", "nResume": "▶ Включить снова", "nMenu": "⚙️ Настройки",
           "kTop": "🔝 Лучшие сейчас", "kMenu": "⚙️ Настройки", "kPause": "⏸ Пауза", "kResume": "▶ Включить",
           "subOn": "✅ Уведомления включены.",
           "ownerView": "⚙️ <b>Твои условия</b> — со страницы программы, меняются там\nЛига: <b>{league}</b> · окно курса {window} ч\n{base} → {target} · капитал <b>{cap}</b> · обменов до {maxLen}\nЗапас: {noise}, минимум {fee}% · мин. объём {minVol} div/сут\nПрибыль в выдаче: {pmin}–{pmax}%\nУведомлять: прибыль от <b>{pct}%</b> · держится от <b>{streak} ч</b>\nИсключено: {ex}\n{state}",
           "nzOff": "один для всех", "nzSoft": "по шуму, мягко", "nzNorm": "по шуму, обычно", "nzStrict": "по шуму, строго",
           "cmd_top": "Лучшие пути сейчас", "cmd_settings": "Настройки условий", "cmd_stop": "Поставить уведомления на паузу",
           "cmd_start": "Включить уведомления", "cmd_help": "Что умеет бот",
           "cbStopped": "Уведомления на паузе", "cbResumed": "Уведомления снова включены",
           "askCap": "Отправь число — капитал в стартовой валюте, например 37.", "badNum": "Нужно число больше нуля, например 37.",
           "topHead": "🔝 Лучшие пути сейчас ({league}, капитал {cap}):", "topNone": "Сейчас подходящих путей нет.",
           "noData": "Данных по этой лиге пока нет — попробуй через пару минут.",
           "help": "Команды: /settings — настройки, /top — лучшие пути сейчас, /stop — пауза, /start — включить."},
    "en": {"head": "🟢 New path", "holds": "holds {n}", "liq": "liquidity {v} div", "more": "…and {n} more",
           "open": "Open in the app", "test": "🔔 PoE 2 Flip Finder test notification — it works.",
           "h": "h", "min": "min", "d": "d",
           "linked": "✅ You're connected as the bot owner. Alert conditions come from your program page.",
           "owner": "You're the owner: conditions come from the program page (filters, “Profit from”, “Holds at least”). Commands: /top — best paths now, /stop — pause, /start — resume.",
           "ownerOn": "✅ Notifications on. I'll send new paths using the conditions from the program page. /top — best now, /stop — pause.",
           "welcome": "👋 Hi! I send new profitable currency exchange paths in Path of Exile 2 — based on real trade rates (GGG hourly data).\n\nSet your conditions with the buttons below. Commands: /top — best paths now, /settings — settings, /stop — pause.\n\n⚠️ These are average rates, not the order book: check the rate at Alva before trading.",
           "closed": "The bot is closed to new subscribers right now.",
           "full": "Too many subscribers — try later.",
           "stopped": "⏸ Notifications paused. /start — turn back on.",
           "menu": "⚙️ <b>Settings</b>\nLeague: <b>{league}</b>\nStart: <b>{base}</b> · capital <b>{cap}</b>\nTrades: up to <b>{maxLen}</b>\nProfit from <b>{pct}%</b> · holds at least <b>{streak} h</b>\n{state}",
           "on": "✅ Sending new paths", "off": "⏸ Paused",
           "bLeague": "League", "bCap": "Capital", "bPct": "Profit from", "bStreak": "Holds", "bLen": "Trades",
           "bEdit": "✏️", "bPause": "⏸ Pause", "bResume": "▶ Resume", "bTop": "🔝 Best now",
           "nStop": "⏸ Stop notifications", "nResume": "▶ Turn back on", "nMenu": "⚙️ Settings",
           "kTop": "🔝 Best now", "kMenu": "⚙️ Settings", "kPause": "⏸ Pause", "kResume": "▶ Resume",
           "subOn": "✅ Notifications on.",
           "ownerView": "⚙️ <b>Your conditions</b> — from the program page, change them there\nLeague: <b>{league}</b> · rate window {window} h\n{base} → {target} · capital <b>{cap}</b> · up to {maxLen} trades\nMargin: {noise}, minimum {fee}% · min volume {minVol} div/day\nProfit in results: {pmin}–{pmax}%\nNotify: profit from <b>{pct}%</b> · holds at least <b>{streak} h</b>\nExcluded: {ex}\n{state}",
           "nzOff": "same for all", "nzSoft": "by noise, soft", "nzNorm": "by noise, normal", "nzStrict": "by noise, strict",
           "cmd_top": "Best paths right now", "cmd_settings": "Alert conditions", "cmd_stop": "Pause notifications",
           "cmd_start": "Turn notifications on", "cmd_help": "What the bot does",
           "cbStopped": "Notifications paused", "cbResumed": "Notifications are back on",
           "askCap": "Send a number — capital in the start currency, e.g. 37.", "badNum": "Need a number above zero, e.g. 37.",
           "topHead": "🔝 Best paths now ({league}, capital {cap}):", "topNone": "No matching paths right now.",
           "noData": "No data for this league yet — try again in a couple of minutes.",
           "help": "Commands: /settings — settings, /top — best paths now, /stop — pause, /start — resume."},
    "zh": {"head": "🟢 新路径", "holds": "持续 {n}", "liq": "流动性 {v} div", "more": "…还有 {n} 条",
           "open": "在程序中打开", "test": "🔔 PoE 2 Flip Finder 测试通知——一切正常。",
           "h": "小时", "min": "分钟", "d": "天",
           "linked": "✅ 你已作为机器人所有者连接。提醒条件来自你的程序页面。",
           "owner": "你是所有者：条件来自程序页面（筛选、“最低利润”、“至少持续”）。命令：/top — 当前最佳路径，/stop — 暂停，/start — 开启。",
           "ownerOn": "✅ 提醒已开启。将按程序页面上的条件推送新路径。/top — 当前最佳，/stop — 暂停。",
           "welcome": "👋 你好！我会推送《流放之路 2》中新的盈利货币兑换路径——基于真实成交汇率（GGG 每小时数据）。\n\n用下面的按钮设置条件。命令：/top — 当前最佳路径，/settings — 设置，/stop — 暂停。\n\n⚠️ 这些是平均汇率而非挂单：兑换前请在阿尔瓦处核对汇率。",
           "closed": "机器人目前不接受新订阅。",
           "full": "订阅者太多——请稍后再试。",
           "stopped": "⏸ 提醒已暂停。/start — 重新开启。",
           "menu": "⚙️ <b>设置</b>\n赛季：<b>{league}</b>\n起始货币：<b>{base}</b> · 本金 <b>{cap}</b>\n兑换次数：最多 <b>{maxLen}</b>\n利润至少 <b>{pct}%</b> · 至少持续 <b>{streak} 小时</b>\n{state}",
           "on": "✅ 正在推送新路径", "off": "⏸ 已暂停",
           "bLeague": "赛季", "bCap": "本金", "bPct": "最低利润", "bStreak": "持续", "bLen": "兑换",
           "bEdit": "✏️", "bPause": "⏸ 暂停", "bResume": "▶ 开启", "bTop": "🔝 当前最佳",
           "nStop": "⏸ 停止提醒", "nResume": "▶ 重新开启", "nMenu": "⚙️ 设置",
           "kTop": "🔝 当前最佳", "kMenu": "⚙️ 设置", "kPause": "⏸ 暂停", "kResume": "▶ 开启",
           "subOn": "✅ 提醒已开启。",
           "ownerView": "⚙️ <b>你的条件</b>——来自程序页面，请在那里修改\n赛季：<b>{league}</b> · 汇率窗口 {window} 小时\n{base} → {target} · 本金 <b>{cap}</b> · 最多 {maxLen} 次兑换\n余量：{noise}，最低 {fee}% · 最低成交量 {minVol} div/天\n结果中的利润：{pmin}–{pmax}%\n提醒：利润至少 <b>{pct}%</b> · 至少持续 <b>{streak} 小时</b>\n已排除：{ex}\n{state}",
           "nzOff": "统一余量", "nzSoft": "按波动，宽松", "nzNorm": "按波动，标准", "nzStrict": "按波动，严格",
           "cmd_top": "当前最佳路径", "cmd_settings": "提醒条件", "cmd_stop": "暂停提醒",
           "cmd_start": "开启提醒", "cmd_help": "机器人能做什么",
           "cbStopped": "提醒已暂停", "cbResumed": "提醒已重新开启",
           "askCap": "请发送一个数字——以起始货币计的本金，例如 37。", "badNum": "需要大于零的数字，例如 37。",
           "topHead": "🔝 当前最佳路径（{league}，本金 {cap}）：", "topNone": "目前没有符合条件的路径。",
           "noData": "该赛季暂无数据——请过几分钟再试。",
           "help": "命令：/settings — 设置，/top — 当前最佳路径，/stop — 暂停，/start — 开启。"},
    "uk": {"head": "🟢 Новий шлях", "holds": "тримається {n}", "liq": "ліквідність {v} div", "more": "…і ще {n}",
           "open": "Відкрити в програмі", "test": "🔔 Тестове сповіщення PoE 2 Flip Finder — усе працює.",
           "h": "год", "min": "хв", "d": "дн",
           "linked": "✅ Ти підключений як власник бота. Умови сповіщень беруться з твоєї сторінки програми.",
           "owner": "Ти власник: умови беруться зі сторінки програми (фільтри, «Прибуток від», «Тримається від»). Команди: /top — найкращі шляхи зараз, /stop — пауза, /start — увімкнути.",
           "ownerOn": "✅ Сповіщення увімкнено. Надсилатиму нові шляхи за умовами зі сторінки програми. /top — найкращі зараз, /stop — пауза.",
           "welcome": "👋 Привіт! Я надсилаю нові вигідні шляхи обміну валюти в Path of Exile 2 — за курсами реальних угод (погодинні дані GGG).\n\nНалаштуй умови кнопками нижче. Команди: /top — найкращі шляхи зараз, /settings — налаштування, /stop — пауза.\n\n⚠️ Це середні курси, а не стакан: перед обміном звіряй курс в Альви.",
           "closed": "Бот зараз закритий для нових підписників.",
           "full": "Забагато підписників — спробуй пізніше.",
           "stopped": "⏸ Сповіщення на паузі. /start — увімкнути знову.",
           "menu": "⚙️ <b>Налаштування</b>\nЛіга: <b>{league}</b>\nСтартова: <b>{base}</b> · капітал <b>{cap}</b>\nОбмінів: до <b>{maxLen}</b>\nПрибуток від <b>{pct}%</b> · тримається від <b>{streak} год</b>\n{state}",
           "on": "✅ Надсилаю нові шляхи", "off": "⏸ Пауза",
           "bLeague": "Ліга", "bCap": "Капітал", "bPct": "Прибуток від", "bStreak": "Тримається", "bLen": "Обмінів",
           "bEdit": "✏️", "bPause": "⏸ Пауза", "bResume": "▶ Увімкнути", "bTop": "🔝 Найкращі зараз",
           "nStop": "⏸ Зупинити сповіщення", "nResume": "▶ Увімкнути знову", "nMenu": "⚙️ Налаштування",
           "kTop": "🔝 Найкращі зараз", "kMenu": "⚙️ Налаштування", "kPause": "⏸ Пауза", "kResume": "▶ Увімкнути",
           "subOn": "✅ Сповіщення увімкнено.",
           "ownerView": "⚙️ <b>Твої умови</b> — зі сторінки програми, змінюються там\nЛіга: <b>{league}</b> · вікно курсу {window} год\n{base} → {target} · капітал <b>{cap}</b> · обмінів до {maxLen}\nЗапас: {noise}, мінімум {fee}% · мін. обсяг {minVol} div/добу\nПрибуток у видачі: {pmin}–{pmax}%\nСповіщати: прибуток від <b>{pct}%</b> · тримається від <b>{streak} год</b>\nВиключено: {ex}\n{state}",
           "nzOff": "один для всіх", "nzSoft": "за шумом, м'яко", "nzNorm": "за шумом, звичайно", "nzStrict": "за шумом, суворо",
           "cmd_top": "Найкращі шляхи зараз", "cmd_settings": "Налаштування умов", "cmd_stop": "Поставити сповіщення на паузу",
           "cmd_start": "Увімкнути сповіщення", "cmd_help": "Що вміє бот",
           "cbStopped": "Сповіщення на паузі", "cbResumed": "Сповіщення знову увімкнено",
           "askCap": "Надішли число — капітал у стартовій валюті, наприклад 37.", "badNum": "Потрібне число, більше за нуль, наприклад 37.",
           "topHead": "🔝 Найкращі шляхи зараз ({league}, капітал {cap}):", "topNone": "Зараз відповідних шляхів немає.",
           "noData": "Даних для цієї ліги поки немає — спробуй за кілька хвилин.",
           "help": "Команди: /settings — налаштування, /top — найкращі шляхи зараз, /stop — пауза, /start — увімкнути."},
    "be": {"head": "🟢 Новы шлях", "holds": "трымаецца {n}", "liq": "ліквіднасць {v} div", "more": "…і яшчэ {n}",
           "open": "Адкрыць у праграме", "test": "🔔 Тэставае апавяшчэнне PoE 2 Flip Finder — усё працуе.",
           "h": "гадз", "min": "хв", "d": "дз",
           "linked": "✅ Ты падключаны як уладальнік бота. Умовы апавяшчэнняў бяруцца з тваёй старонкі праграмы.",
           "owner": "Ты ўладальнік: умовы бяруцца са старонкі праграмы (фільтры, «Прыбытак ад», «Трымаецца ад»). Каманды: /top — найлепшыя шляхі зараз, /stop — паўза, /start — уключыць.",
           "ownerOn": "✅ Апавяшчэнні ўключаны. Дашлю новыя шляхі па ўмовах са старонкі праграмы. /top — найлепшыя зараз, /stop — паўза.",
           "welcome": "👋 Прывітанне! Я дасылаю новыя выгадныя шляхі абмену валюты ў Path of Exile 2 — па курсах рэальных здзелак (пагадзінныя даныя GGG).\n\nНаладзь умовы кнопкамі ніжэй. Каманды: /top — найлепшыя шляхі зараз, /settings — налады, /stop — паўза.\n\n⚠️ Гэта сярэднія курсы, а не жывыя заяўкі: перад абменам звярай курс у Альвы.",
           "closed": "Бот зараз зачынены для новых падпісчыкаў.",
           "full": "Занадта шмат падпісчыкаў — паспрабуй пазней.",
           "stopped": "⏸ Апавяшчэнні на паўзе. /start — уключыць зноў.",
           "menu": "⚙️ <b>Налады</b>\nЛіга: <b>{league}</b>\nСтартавая: <b>{base}</b> · капітал <b>{cap}</b>\nАбменаў: да <b>{maxLen}</b>\nПрыбытак ад <b>{pct}%</b> · трымаецца ад <b>{streak} гадз</b>\n{state}",
           "on": "✅ Дасылаю новыя шляхі", "off": "⏸ Паўза",
           "bLeague": "Ліга", "bCap": "Капітал", "bPct": "Прыбытак ад", "bStreak": "Трымаецца", "bLen": "Абменаў",
           "bEdit": "✏️", "bPause": "⏸ Паўза", "bResume": "▶ Уключыць", "bTop": "🔝 Найлепшыя зараз",
           "nStop": "⏸ Спыніць апавяшчэнні", "nResume": "▶ Уключыць зноў", "nMenu": "⚙️ Налады",
           "kTop": "🔝 Найлепшыя зараз", "kMenu": "⚙️ Налады", "kPause": "⏸ Паўза", "kResume": "▶ Уключыць",
           "subOn": "✅ Апавяшчэнні ўключаны.",
           "ownerView": "⚙️ <b>Твае ўмовы</b> — са старонкі праграмы, змяняюцца там\nЛіга: <b>{league}</b> · акно курсу {window} гадз\n{base} → {target} · капітал <b>{cap}</b> · абменаў да {maxLen}\nЗапас: {noise}, мінімум {fee}% · мін. аб'ём {minVol} div/суткі\nПрыбытак у выдачы: {pmin}–{pmax}%\nАпавяшчаць: прыбытак ад <b>{pct}%</b> · трымаецца ад <b>{streak} гадз</b>\nВыключана: {ex}\n{state}",
           "nzOff": "адзін для ўсіх", "nzSoft": "па шуме, мякка", "nzNorm": "па шуме, звычайна", "nzStrict": "па шуме, строга",
           "cmd_top": "Найлепшыя шляхі зараз", "cmd_settings": "Налады ўмоў", "cmd_stop": "Паставіць апавяшчэнні на паўзу",
           "cmd_start": "Уключыць апавяшчэнні", "cmd_help": "Што ўмее бот",
           "cbStopped": "Апавяшчэнні на паўзе", "cbResumed": "Апавяшчэнні зноў уключаны",
           "askCap": "Адпраў лік — капітал у стартавай валюце, напрыклад 37.", "badNum": "Патрэбны лік больш за нуль, напрыклад 37.",
           "topHead": "🔝 Найлепшыя шляхі зараз ({league}, капітал {cap}):", "topNone": "Зараз прыдатных шляхоў няма.",
           "noData": "Даных па гэтай лізе пакуль няма — паспрабуй праз пару хвілін.",
           "help": "Каманды: /settings — налады, /top — найлепшыя шляхі зараз, /stop — паўза, /start — уключыць."},
}


def T(lang):
    return TEXTS.get(lang) or TEXTS["ru"]


def _owner_lang(cfg):
    return ((cfg.get("settings") or {}).get("lang") or "ru")


def _tg_lang(code):
    code = (code or "").lower()
    return code[:2] if code[:2] in ("uk", "be") else "ru" if code[:2] in ("ru", "kk") else "zh" if code.startswith("zh") else "en"


# ---------- поиск путей (перенос со страницы) ----------
def _num(v, default):
    """Как `+v || default` в JS: пусто, 0 и мусор дают значение по умолчанию."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f and f == f else default


def pair_noise(hist):
    if not hist:
        return None
    d = sorted(abs(math.log(b / a)) for a, b in zip(hist, hist[1:]) if a and b and a > 0 and b > 0)
    return d[len(d) >> 1] if len(d) >= 4 else None


def pair_keep(hist, fee, mult):
    base = fee / 100
    if not mult or not hist:
        return 1 - base
    n = pair_noise(hist)
    return 1 - min(0.5, max(base, NOISE_FALLBACK if n is None else mult * n))


def find_paths(market, st):
    """Пути, которые страница показала бы с этими настройками (без «Целых чисел» и фильтров по золоту)."""
    items, pairs, core = market["items"], market["pairs"], market["core"]
    base = st.get("base") or "divine"
    target = st.get("target") or base
    is_cycle = base == target
    max_len = int(_num(st.get("maxLen"), 3))
    fee = _num(st.get("fee"), 0)
    mult = _num(st.get("noise"), 0)
    min_vol = _num(st.get("minVol"), 0)
    min_pct = _num(st.get("minProfit"), 0) / 100
    cap = _num(st.get("capital"), 1)
    max_hours = _num(st.get("maxHours"), math.inf)
    share = min(100, max(1, _num(st.get("share"), 25))) / 100
    overhead = _num(st.get("overhead"), 0) / 60
    min_streak = _num(st.get("minStreak"), 0)
    ex_items, ex_cats = set(st.get("exItems") or []), set(st.get("exCats") or [])
    keep_ids = set(core) | {base, target}

    def excluded(i):
        return i not in keep_ids and (i in ex_items or (items.get(i) or {}).get("category") in ex_cats)

    def div_value(i):
        if i == "divine":
            return 1.0
        for p in pairs:
            if p["from"] == i and p["to"] == "divine":
                return p["rate"]
            if p["to"] == i and p["from"] == "divine":
                return 1 / p["rate"]
        return math.nan

    # граф: два направления на каждую пару
    adj, direct = {}, {}
    for p in pairs:
        if p["volume"] < min_vol or excluded(p["from"]) or excluded(p["to"]):
            continue
        keep, hist, vol7 = pair_keep(p.get("hist"), fee, mult), p.get("hist"), p.get("vol7")
        flow = min(p["volume"], vol7) if vol7 else p["volume"]
        for a, b, rate, h in ((p["from"], p["to"], p["rate"], hist),
                              (p["to"], p["from"], 1 / p["rate"], [1 / x if x and x > 0 else None for x in hist] if hist else None)):
            e = {"to": b, "rate": rate, "vol": p["volume"], "keep": keep, "hist": h, "flow": flow}
            adj.setdefault(a, []).append(e)
            direct[(a, b)] = e

    if is_cycle:
        bench = 1.0
    else:
        bench = None
        for p in pairs:
            if p["from"] == base and p["to"] == target:
                bench = p["rate"] * pair_keep(p.get("hist"), fee, mult)
                break
            if p["to"] == base and p["from"] == target:
                bench = pair_keep(p.get("hist"), fee, mult) / p["rate"]
                break
        if bench is None:
            bench = div_value(base) / div_value(target) * pair_keep(None, fee, mult)
    lo = bench * (1 + min_pct)
    hi = bench * (1 + _num(st.get("maxProfit"), 1e9) / 100)

    def redundant(nodes, edges):
        n = len(edges)
        if n < (4 if is_cycle else 3):
            return False
        for i in range(1, n):
            a, b = nodes[i - 1], nodes[i + 1]
            if a == b or (a, b) not in direct:
                continue
            e = direct[(a, b)]
            via = edges[i - 1]["rate"] * edges[i - 1]["keep"] * edges[i]["rate"] * edges[i]["keep"]
            if e["rate"] * e["keep"] >= via:
                return True
        return False

    found = []
    path, edges, used = [base], [], {base}
    steps = [0]

    def dfs(node, amt, depth):
        steps[0] += 1
        if steps[0] > 5e6:
            return
        for e in adj.get(node, []):
            nxt = amt * e["rate"] * e["keep"]
            if e["to"] == target:
                if depth >= 2 and lo <= nxt <= hi:
                    all_e = edges + [e]
                    nodes = path + [target]
                    if not redundant(nodes, all_e):
                        found.append((nodes, all_e))
                continue
            if depth >= max_len or e["to"] in used:
                continue
            used.add(e["to"]); path.append(e["to"]); edges.append(e)
            dfs(e["to"], nxt, depth + 1)
            used.discard(e["to"]); path.pop(); edges.pop()

    dfs(base, 1.0, 1)

    # прямой обмен по часам — для «держится» у путей с другой конечной
    dh, dk = None, 1.0
    if not is_cycle:
        for p in pairs:
            if {p["from"], p["to"]} == {base, target} and p.get("hist"):
                dh = p["hist"] if p["from"] == base else [1 / x if x and x > 0 else None for x in p["hist"]]
                dk = pair_keep(p["hist"], fee, mult)
                break

    def streak(all_e):
        H = len(all_e[0]["hist"]) if all_e[0]["hist"] else 0
        if not H or (not is_cycle and not dh):
            return None
        n = 0
        for h in range(H - 1, -1, -1):
            x = 1.0
            for e in all_e:
                r = e["hist"][h] if e["hist"] else None
                if not (r and r > 0):
                    return n
                x *= r * e["keep"]
            b = 1.0 if is_cycle else (dh[h] * dk if dh[h] and dh[h] > 0 else None)
            if b is None or x / b - 1 < min_pct:
                return n
            n += 1
        return n

    cap_div = cap * div_value(base)
    out = []
    for nodes, all_e in found:
        amounts, a = [cap], cap
        for e in all_e:
            a *= e["rate"] * e["keep"]
            amounts.append(a)
        if any(x < 1 for x in amounts):
            continue
        hours = sum(cap_div / (e["flow"] / 24 * share) + overhead for e in all_e)
        if hours > max_hours:
            continue
        s = streak(all_e)
        if min_streak and not (s is not None and s >= min_streak):
            continue
        out.append({"id": ">".join(nodes), "nodes": nodes, "pct": a / (cap * bench) - 1, "out": a, "start": cap,
                    "streak": s, "hist_len": len(all_e[0]["hist"]) if all_e[0]["hist"] else 0,
                    "min_vol": min(e["vol"] for e in all_e), "hours": hours})
    return out


# Шанс продержаться ещё час по «держится» и рейтинг — как на странице (holdChance / rating в index.html).
def hold_chance(streak):
    if streak is None:
        return 0.66
    return 0.43 if streak == 0 else 0.63 if streak <= 2 else 0.73 if streak <= 5 else 0.80 if streak <= 11 else 0.91


def rating(c):
    return c["pct"] * hold_chance(c["streak"]) * math.log1p(c["min_vol"])


def _matching(paths, st):
    want_pct = _num(st.get("notifyPct"), 0) / 100
    want_streak = _num(st.get("notifyStreak"), 0)
    return [c for c in paths if c["pct"] >= want_pct and (not want_streak or (c["streak"] or 0) >= want_streak)]


# ---------- сообщения ----------
def _dur(h, L):
    if h != h or h == math.inf:
        return "—"
    if h < 1:
        return "~%d %s" % (max(1, round(h * 60)), L["min"])
    if h < 48:
        return "~%s %s" % (("%.1f" % h) if h < 10 else round(h), L["h"])
    return "~%d %s" % (round(h / 24), L["d"])


def _path_text(c, market, L, link_base, head=True):
    items = market["items"]
    name = lambda i: html.escape((items.get(i) or {}).get("name") or i)  # noqa: E731
    s = c["streak"]
    holds = "" if s is None else " · " + L["holds"].format(
        n="%s+ %s" % (c["hist_len"], L["h"]) if s >= c["hist_len"] else "%d %s" % (s, L["h"]))
    line = "%s<b>+%.1f%%</b>%s\n%s\n%s · %s" % (
        L["head"] + " " if head else "", c["pct"] * 100, holds, " → ".join(name(i) for i in c["nodes"]),
        L["liq"].format(v=("{:,.0f}".format(c["min_vol"]).replace(",", " "))), _dur(c["hours"], L))
    if link_base:
        line += '\n<a href="%s#open=%s">%s</a>' % (link_base, urllib.parse.quote(c["id"], safe=""), L["open"])
    return line


def format_message(paths, market, lang, link_base):
    L = T(lang)
    parts = [_path_text(c, market, L, link_base) for c in paths[:MAX_IN_MESSAGE]]
    if len(paths) > MAX_IN_MESSAGE:
        parts.append(L["more"].format(n=len(paths) - MAX_IN_MESSAGE))
    return "\n\n".join(parts)


# Кнопки под каждым уведомлением: остановить (и вернуть), у подписчиков ещё «Настройки».
def notif_kb(lang, active, owner):
    L = T(lang)
    if not active:
        return {"inline_keyboard": [[{"text": L["nResume"], "callback_data": "nr"}]]}
    row = [{"text": L["nStop"], "callback_data": "ns"}]
    if not owner:
        row.append({"text": L["nMenu"], "callback_data": "nm"})
    return {"inline_keyboard": [row]}


# ---------- проверка и рассылка ----------
_watch = {}  # кто ("owner" или id чата) -> {"key": условия, "known": {путь: когда последний раз подходил}}


def _fresh(who, key, match):
    """Новые пути для получателя. Первая проверка при новых условиях — молча запоминаем выдачу."""
    w = _watch.get(who)
    silent = not w or w["key"] != key
    if silent:
        w = _watch[who] = {"key": key, "known": {}}
    now, fresh = time.time(), []
    for c in match:
        last = w["known"].get(c["id"])
        if not silent and (last is None or now - last > REPEAT_AFTER):
            fresh.append(c)
        w["known"][c["id"]] = now
    return sorted(fresh, key=rating, reverse=True)


def sub_settings(sub, cfg, default_league):
    st = dict(SUB_DEFAULT)
    st.update({k: v for k, v in (sub.get("settings") or {}).items() if v not in (None, "")})
    st["league"] = st.get("league") or (cfg.get("settings") or {}).get("league") or default_league
    return st


def check_all(data_dir, get_market, link_base, default_league):
    """Раз в 5 минут: новые пути владельцу (условия со страницы) и каждому активному подписчику (свои условия)."""
    cfg = load_cfg(data_dir)
    token = cfg.get("token")
    if not token:
        return
    jobs = []  # (кто, чат, условия, язык, ссылка)
    if cfg.get("enabled") and cfg.get("chat_id"):
        jobs.append(("owner", cfg["chat_id"], cfg.get("settings") or {}, _owner_lang(cfg), link_base))
    subs = load_subs(data_dir)
    owner = str(cfg.get("chat_id"))
    if owner in subs:  # подписался до того, как стал владельцем, — иначе получал бы всё дважды
        with _lock:
            subs = load_subs(data_dir); subs.pop(owner, None); save_subs(data_dir, subs)
    for cid, sub in subs.items():
        if sub.get("active"):
            jobs.append((cid, int(cid), sub_settings(sub, cfg, default_league), sub.get("lang", "ru"), None))
    searched, gone, errors, total = {}, [], [], 0
    for who, chat, st, lang, link in jobs:
        market = get_market(st.get("league"), int(_num(st.get("window"), 3)))
        if not market:
            continue
        skey = json.dumps({k: v for k, v in st.items() if k not in ("notifyPct", "notifyStreak", "lang")}, sort_keys=True)
        if skey not in searched:  # одинаковые условия — один поиск на всех
            searched[skey] = find_paths(market, st)
        match = _matching(searched[skey], st)
        if who == "owner":
            total = len(match)
        fresh = _fresh(who, json.dumps([st.get("league"), st], sort_keys=True), match)
        if not fresh:
            continue
        try:
            tg_call(token, "sendMessage", {"chat_id": chat, "text": format_message(fresh, market, lang, link),
                                           "parse_mode": "HTML", "disable_web_page_preview": True,
                                           "reply_markup": notif_kb(lang, True, who == "owner")})
            status["last_sent"] = int(time.time())
        except RuntimeError as e:
            if getattr(e, "code", None) == 403 and who != "owner":
                gone.append(who)  # подписчик заблокировал бота
            else:
                errors.append(str(e))
        time.sleep(0.05)  # Telegram: не больше ~30 сообщений в секунду
    if gone:
        with _lock:
            subs = load_subs(data_dir)
            for who in gone:
                if who in subs:
                    subs[who]["active"] = False
            save_subs(data_dir, subs)
    status.update(last_check=int(time.time()), found=total, last_error=errors[-1] if errors else None)


# ---------- бот: сообщения и кнопки ----------
def _menu(sub, st, lang, leagues):
    L = T(lang)
    names = {"chaos": "Chaos", "divine": "Divine", "exalted": "Exalted"}
    text = L["menu"].format(league=html.escape(st["league"] or "—"), base=names.get(st["base"], st["base"]),
                            cap=html.escape(str(st["capital"])), maxLen=st["maxLen"], pct=st["notifyPct"],
                            streak=st["notifyStreak"], state=L["on"] if sub.get("active") else L["off"])
    mark = lambda on, s: ("✓ " + s) if on else s  # noqa: E731
    b = lambda text, data: {"text": text, "callback_data": data}  # noqa: E731
    kb = [
        [b("%s: %s ›" % (L["bLeague"], st["league"] or "—"), "lg")] if len(leagues) > 1 else [],
        [b(mark(st["base"] == k, v), "b:" + k) for k, v in names.items()],
        [b(L["bCap"], "noop")] + [b(mark(_num(st["capital"], 0) == c, str(c)), "c:%d" % c) for c in CAPITALS[:3]],
        [b(mark(_num(st["capital"], 0) == c, str(c)), "c:%d" % c) for c in CAPITALS[3:]] + [b(L["bEdit"], "c:edit")],
        [b(L["bPct"], "noop")] + [b(mark(_num(st["notifyPct"], 0) == p, "%d%%" % p), "p:%d" % p) for p in PCTS],
        [b(L["bStreak"], "noop")] + [b(mark(_num(st["notifyStreak"], 0) == s, "%d" % s), "s:%d" % s) for s in STREAKS],
        [b(L["bLen"], "noop")] + [b(mark(int(_num(st["maxLen"], 3)) == m, str(m)), "m:%d" % m) for m in (3, 4)],
        [b(mark(lang == k, v), "lang:" + k) for k, v in (("ru", "RU"), ("en", "EN"), ("zh", "中文"), ("uk", "УКР"), ("be", "БЕЛ"))],
        [b(L["bPause"] if sub.get("active") else L["bResume"], "toggle"), b(L["bTop"], "top")],
    ]
    return text, {"inline_keyboard": [row for row in kb if row]}


def _owner_view(cfg, default_league):
    """Условия владельца (со страницы программы) — только просмотр, меняются на странице."""
    lang = _owner_lang(cfg)
    L = T(lang)
    st = cfg.get("settings") or {}
    v = lambda k, d="—": st.get(k) if st.get(k) not in (None, "") else d  # noqa: E731
    names = {"chaos": "Chaos", "divine": "Divine", "exalted": "Exalted"}
    noise = {"0": L["nzOff"], "0.5": L["nzSoft"], "1": L["nzNorm"], "1.5": L["nzStrict"]}.get(str(v("noise", "1")), str(v("noise")))
    text = L["ownerView"].format(
        league=html.escape(v("league", default_league or "—")), window=v("window", "3"),
        base=names.get(v("base", "divine"), v("base")), target=names.get(v("target", "") or v("base", "divine"), v("target")),
        cap=html.escape(str(v("capital", "1"))), maxLen=v("maxLen", "3"), noise=noise, fee=v("fee", "0"),
        minVol=v("minVol", "0"), pmin=v("minProfit", "0"), pmax=v("maxProfit", "∞"),
        pct=v("notifyPct", "0"), streak=v("notifyStreak", "0"),
        ex=len(st.get("exCats") or []) + len(st.get("exItems") or []),
        state=L["on"] if cfg.get("enabled") else L["off"])
    kb = {"inline_keyboard": [[{"text": L["bPause"] if cfg.get("enabled") else L["bResume"], "callback_data": "toggle"},
                               {"text": L["bTop"], "callback_data": "top"}]]}
    return text, kb


def _top_text(st, lang, get_market):
    L = T(lang)
    market = get_market(st["league"], int(_num(st.get("window"), 3)))
    if not market:
        return L["noData"]
    best = sorted(_matching(find_paths(market, st), st), key=rating, reverse=True)[:MAX_IN_MESSAGE]
    head = L["topHead"].format(league=html.escape(st["league"]), cap=html.escape(str(st["capital"])))
    return head + "\n\n" + ("\n\n".join(_path_text(c, market, L, None, head=False) for c in best) if best else L["topNone"])


# Закреплённые кнопки под полем ввода — одинаковые для всех, чтобы не устаревали после паузы/включения.
def reply_kb(lang):
    L = T(lang)
    return {"keyboard": [[{"text": L["kTop"]}, {"text": L["kMenu"]}], [{"text": L["kPause"]}, {"text": L["kResume"]}]],
            "resize_keyboard": True, "is_persistent": True}


# Нажатие закреплённой кнопки приходит обычным текстом — сопоставляем с командой на любом языке.
BUTTONS = {T(l)[k]: c for l in TEXTS for k, c in (("kTop", "/top"), ("kMenu", "/settings"), ("kPause", "/stop"), ("kResume", "/start"))}


def register_commands(token):
    """Подсказки при вводе «/» и кнопка «Меню» рядом с полем ввода — на языке пользователя."""
    for lang in TEXTS:
        cmds = [{"command": c, "description": T(lang)["cmd_" + c]} for c in ("top", "settings", "stop", "start", "help")]
        params = {"commands": cmds} if lang == "en" else {"commands": cmds, "language_code": lang}
        tg_call(token, "setMyCommands", params)
    tg_call(token, "setChatMenuButton", {"menu_button": {"type": "commands"}})


def _set_kb(token, chat, message_id, kb):
    try:
        tg_call(token, "editMessageReplyMarkup", {"chat_id": chat, "message_id": message_id, "reply_markup": kb})
    except RuntimeError:
        pass  # сообщение слишком старое или уже с этими кнопками


def on_update(data_dir, u, ctx):
    """Одно обновление от Telegram. ctx: leagues(), default_league(), market(league, window)."""
    cfg = load_cfg(data_dir)
    token = cfg.get("token")
    if not token:
        return
    send = lambda chat, text, kb=None: tg_call(token, "sendMessage", dict(  # noqa: E731
        {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, **({"reply_markup": kb} if kb else {})))
    cb = u.get("callback_query")
    msg = u.get("message") or (cb or {}).get("message")
    if not msg or msg.get("chat", {}).get("type") != "private":
        return
    chat = msg["chat"]["id"]
    user = (cb or u.get("message") or {}).get("from") or {}
    text = (u.get("message") or {}).get("text") or ""
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else BUTTONS.get(text.strip(), "")

    # владелец подтверждает себя кодом со страницы
    if cmd == "/link":
        code = (text.split() + [""])[1]
        with _lock:
            cfg = load_cfg(data_dir)
            if cfg.get("link_code") and code == cfg["link_code"]:
                cfg.update(chat_id=chat, enabled=True, link_code=None)
                save_cfg(data_dir, cfg)
                subs = load_subs(data_dir)
                if subs.pop(str(chat), None) is not None:  # владелец получает уведомления по странице, не как подписчик
                    save_subs(data_dir, subs)
                send(chat, T(_owner_lang(cfg))["linked"], reply_kb(_owner_lang(cfg)))
                _watch.pop("owner", None)
        return
    if chat == cfg.get("chat_id"):
        L = T(_owner_lang(cfg))
        if cb:
            data, note = cb.get("data"), ""
            if data in ("ns", "nr"):
                with _lock:
                    cfg = load_cfg(data_dir); cfg["enabled"] = data == "nr"; save_cfg(data_dir, cfg)
                if data == "nr":
                    _watch.pop("owner", None)
                note = L["cbStopped"] if data == "ns" else L["cbResumed"]
                _set_kb(token, chat, msg["message_id"], notif_kb(_owner_lang(cfg), data == "nr", True))
            elif data == "toggle":
                with _lock:
                    cfg = load_cfg(data_dir); cfg["enabled"] = not cfg.get("enabled"); save_cfg(data_dir, cfg)
                _watch.pop("owner", None)
                note = L["cbResumed"] if cfg["enabled"] else L["cbStopped"]
                txt, kb = _owner_view(cfg, ctx["default_league"]())
                try:
                    tg_call(token, "editMessageText", {"chat_id": chat, "message_id": msg["message_id"], "text": txt,
                                                       "parse_mode": "HTML", "reply_markup": kb})
                except RuntimeError:
                    pass
            elif data == "top":
                st = dict(SUB_DEFAULT)
                st.update({k: v for k, v in (cfg.get("settings") or {}).items() if v not in (None, "")})
                st["league"] = st.get("league") or ctx["default_league"]()
                send(chat, _top_text(st, _owner_lang(cfg), ctx["market"]))
            tg_call(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": note})
            return
        if cmd == "/top":
            st = dict(SUB_DEFAULT)
            st.update({k: v for k, v in (cfg.get("settings") or {}).items() if v not in (None, "")})
            st["league"] = st.get("league") or ctx["default_league"]()
            send(chat, _top_text(st, _owner_lang(cfg), ctx["market"]))
        elif cmd == "/stop":
            with _lock:
                cfg = load_cfg(data_dir); cfg["enabled"] = False; save_cfg(data_dir, cfg)
            send(chat, L["stopped"], reply_kb(_owner_lang(cfg)))
        elif cmd == "/start":
            with _lock:
                cfg = load_cfg(data_dir); cfg["enabled"] = True; save_cfg(data_dir, cfg)
            _watch.pop("owner", None)
            send(chat, L["ownerOn"], reply_kb(_owner_lang(cfg)))
        elif cmd == "/settings":
            send(chat, *_owner_view(cfg, ctx["default_league"]()))
        else:
            send(chat, L["owner"], reply_kb(_owner_lang(cfg)))
        return

    # подписчики
    key = str(chat)
    with _lock:
        subs = load_subs(data_dir)
        sub = subs.get(key)
        if sub is None:
            lang = _tg_lang(user.get("language_code"))
            if cmd != "/start":
                if not cb:
                    send(chat, T(lang)["help"] if cfg.get("open", True) else T(lang)["closed"])
                return
            if not cfg.get("open", True):
                send(chat, T(lang)["closed"])
                return
            if len(subs) >= MAX_SUBSCRIBERS:
                send(chat, T(lang)["full"])
                return
            sub = subs[key] = {"name": user.get("username") or user.get("first_name") or "", "lang": lang,
                               "active": True, "joined": int(time.time()), "settings": {}}
            save_subs(data_dir, subs)
            st = sub_settings(sub, cfg, ctx["default_league"]())
            send(chat, T(lang)["welcome"], reply_kb(lang))
            send(chat, *_menu(sub, st, lang, ctx["leagues"]()))
            return
        lang = sub.get("lang", "ru")
        L = T(lang)
        st = sub_settings(sub, cfg, ctx["default_league"]())
        changed = False
        if cb:
            data = cb.get("data") or ""
            kind, _, val = data.partition(":")
            if data in ("ns", "nr", "nm"):  # кнопки под уведомлением
                note = ""
                if data == "nm":
                    send(chat, *_menu(sub, st, lang, ctx["leagues"]()))
                else:
                    sub["active"] = data == "nr"
                    save_subs(data_dir, subs)
                    _watch.pop(key, None)
                    note = L["cbStopped"] if data == "ns" else L["cbResumed"]
                    _set_kb(token, chat, msg["message_id"], notif_kb(lang, sub["active"], False))
                tg_call(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": note})
                return
            if kind == "lg":
                leagues = ctx["leagues"]() or [st["league"]]
                i = leagues.index(st["league"]) if st["league"] in leagues else -1
                sub["settings"]["league"] = leagues[(i + 1) % len(leagues)]
                changed = True
            elif kind == "b" and val in ("chaos", "divine", "exalted"):
                sub["settings"]["base"] = val; changed = True
            elif kind == "c" and val == "edit":
                sub["await"] = "capital"
                send(chat, L["askCap"])
            elif kind == "c":
                sub["settings"]["capital"] = val; changed = True
            elif kind == "p":
                sub["settings"]["notifyPct"] = val; changed = True
            elif kind == "s":
                sub["settings"]["notifyStreak"] = val; changed = True
            elif kind == "m" and val in ("3", "4"):
                sub["settings"]["maxLen"] = val; changed = True
            elif kind == "lang" and val in TEXTS:
                sub["lang"] = lang = val; L = T(lang); changed = True
            elif kind == "toggle":
                sub["active"] = not sub.get("active"); changed = True
            elif kind == "top":
                send(chat, _top_text(st, lang, ctx["market"]))
            save_subs(data_dir, subs)
            tg_call(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
            if changed:
                _watch.pop(key, None)
                st = sub_settings(sub, cfg, ctx["default_league"]())
                txt, kb = _menu(sub, st, lang, ctx["leagues"]())
                try:
                    tg_call(token, "editMessageText", {"chat_id": chat, "message_id": msg["message_id"], "text": txt,
                                                       "parse_mode": "HTML", "reply_markup": kb})
                except RuntimeError:
                    pass  # «message is not modified» и т.п.
            return
        if sub.get("await") == "capital" and not cmd:
            v = _num(text.replace(",", ".").strip(), 0)
            if v <= 0 or v > 1e6:
                send(chat, L["badNum"])
                return
            sub["settings"]["capital"] = ("%g" % v)
            sub.pop("await", None)
            save_subs(data_dir, subs)
            _watch.pop(key, None)
            send(chat, *_menu(sub, sub_settings(sub, cfg, ctx["default_league"]()), lang, ctx["leagues"]()))
            return
        if cmd:
            sub.pop("await", None)
        if cmd == "/start":
            sub["active"] = True
            save_subs(data_dir, subs)
            _watch.pop(key, None)
            send(chat, L["subOn"], reply_kb(lang))
            send(chat, *_menu(sub, st, lang, ctx["leagues"]()))
        elif cmd == "/stop":
            sub["active"] = False
            save_subs(data_dir, subs)
            send(chat, L["stopped"], reply_kb(lang))
        elif cmd == "/settings":
            send(chat, *_menu(sub, st, lang, ctx["leagues"]()))
        elif cmd == "/top":
            send(chat, _top_text(st, lang, ctx["market"]))
        else:
            send(chat, L["help"], reply_kb(lang))


def poll_loop(data_dir, ctx):
    """Читает сообщения боту (long polling). Без токена — ждёт. Не падает: любой сбой — пауза и новая попытка."""
    while True:
        try:
            _poll(data_dir, ctx)
        except Exception as e:  # страховка: поток чтения сообщений не должен умирать до перезапуска программы
            status["last_error"] = "bot: %s" % e
            time.sleep(15)


def _poll(data_dir, ctx):
    offset, token_seen, fails = None, None, 0
    while True:
        token = load_cfg(data_dir).get("token")
        if not token:
            time.sleep(5)
            continue
        if token != token_seen:
            offset, token_seen = None, token
            try:
                register_commands(token)
            except RuntimeError as e:
                status["last_error"] = str(e)
        try:
            updates = tg_call(token, "getUpdates", {"timeout": 25, "offset": offset,
                                                    "allowed_updates": ["message", "callback_query"]}, timeout=40)
        except RuntimeError as e:
            status["last_error"] = str(e)
            fails += 1
            time.sleep(min(60, 5 * fails))  # сеть пропала — пробуем реже, пока не вернётся
            continue
        if fails:
            fails = 0
            status["last_error"] = None  # связь вернулась
        for u in updates:
            offset = u["update_id"] + 1
            try:
                on_update(data_dir, u, ctx)
            except Exception as e:  # одно кривое сообщение не должно ронять бота
                status["last_error"] = "bot: %s" % e
