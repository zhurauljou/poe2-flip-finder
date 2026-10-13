"""Обновление программы из релизов GitHub.

Раз в несколько часов сервер спрашивает у GitHub последний релиз. Если его версия новее — страница предлагает
обновиться. Установка скачивает архив релиза (тот же poe2-flip.zip, что собирает tools/Build release zip)
и заменяет только файлы программы из списка; кэш, журнал, токен бота и настройки не трогает.
Новая версия начинает работать после перезапуска программы.
Только стандартная библиотека.
"""
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import zipfile

REPO = os.environ.get("POE2FLIP_UPDATE_REPO", "zhurauljou/poe2-flip-finder")
API = os.environ.get("POE2FLIP_UPDATE_API", "https://api.github.com")
MAX_ZIP = 20 * 1024 * 1024

_lock = threading.Lock()
state = {"latest": None, "notes": "", "asset": None, "page": None, "checked": None, "error": None, "installed": None}


def parse_version(v):
    """'v1.2.3' / '1.2' -> (1, 2, 3) / (1, 2, 0) — для сравнения."""
    nums = [int(x) for x in re.findall(r"\d+", str(v or ""))[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def _get(url, ua, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": accept})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(MAX_ZIP + 1)


def check(ua):
    """Спрашивает у GitHub последний релиз. Ошибки сети не страшны — проверим в следующий раз."""
    try:
        d = json.loads(_get("%s/repos/%s/releases/latest" % (API, REPO), ua).decode("utf-8"))
        assets = [a for a in d.get("assets", []) if str(a.get("name", "")).endswith(".zip")]
        asset = next((a for a in assets if a["name"] == "poe2-flip.zip"), assets[0] if assets else None)
        with _lock:
            state.update(latest=str(d.get("tag_name") or "").lstrip("vV") or None, notes=d.get("body") or "",
                         asset=asset and asset.get("browser_download_url"), page=d.get("html_url"),
                         checked=int(time.time()), error=None)
    except urllib.error.HTTPError as e:
        with _lock:  # 404 — релизов ещё нет
            state.update(checked=int(time.time()), error=None if e.code == 404 else "GitHub: HTTP %d" % e.code)
    except Exception as e:
        with _lock:
            state.update(checked=int(time.time()), error="нет связи с GitHub (%s)" % e.__class__.__name__)


_releases = {"t": 0, "list": [], "error": None}


def releases(ua, n=5):
    """Последние релизы для окна «Что нового» (кэш 10 минут — у GitHub лимит 60 запросов в час без входа)."""
    if time.time() - _releases["t"] > 600 or not _releases["list"]:
        try:
            d = json.loads(_get("%s/repos/%s/releases?per_page=%d" % (API, REPO, n), ua).decode("utf-8"))
            _releases.update(t=time.time(), error=None, list=[
                {"version": str(r.get("tag_name") or "").lstrip("vV"), "notes": r.get("body") or "",
                 "date": (r.get("published_at") or "")[:10], "url": r.get("html_url")}
                for r in d if not r.get("draft") and not r.get("prerelease")])
        except Exception as e:
            _releases.update(t=time.time(), error="нет связи с GitHub (%s)" % e.__class__.__name__)
    return {"releases": _releases["list"], "error": _releases["error"], "page": "https://github.com/%s/releases" % REPO}


def status(current):
    with _lock:
        s = dict(state)
    s["current"] = current
    s["available"] = bool(s["latest"] and s["asset"] and parse_version(s["latest"]) > parse_version(current)
                          and s["installed"] != s["latest"])
    return s


def _app_file(rel):
    """Новые файлы программы (app/имя.py и т.п.) ставим, даже если их нет в списке у текущей версии."""
    name = rel[4:] if rel.startswith("app/") else ""
    return bool(name) and "/" not in name and not name.startswith(".") and name.rsplit(".", 1)[-1] in ("py", "html", "png", "ico")


def install(root, files, current, ua):
    """Скачивает архив релиза и заменяет файлы программы. files — пути относительно корня программы
    («app/server.py», «Start (Windows).bat»). Возвращает, что изменилось."""
    s = status(current)
    if not s["available"]:
        raise RuntimeError("обновлений нет")
    data = _get(s["asset"], ua, accept="application/octet-stream")
    if len(data) > MAX_ZIP:
        raise RuntimeError("архив обновления слишком большой")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise RuntimeError("скачанный файл — не архив")
    # В архиве всё лежит в папке poe2-flip/ — берём только известные файлы программы по их месту в архиве.
    new = {}
    for info in z.infolist():
        rel = info.filename.replace(chr(92), "/").split("/", 1)[-1]
        if (rel in files or _app_file(rel)) and not info.is_dir():
            new[rel] = (z.read(info), info.external_attr >> 16)
    if not any(r.endswith("server.py") for r in new) or not any(r.endswith("index.html") for r in new):
        raise RuntimeError("в архиве нет файлов программы")
    changed = []
    for name, (content, mode) in new.items():
        path = os.path.join(root, *name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "rb") as fh:
                if fh.read() == content:
                    continue  # не трогаем то, что не изменилось (в том числе запущенный сейчас .bat)
        except OSError:
            pass
        tmp = path + ".update"
        with open(tmp, "wb") as fh:
            fh.write(content)
        if mode and os.name != "nt":
            os.chmod(tmp, mode & 0o777)  # Mac: .command остаётся запускаемым
        os.replace(tmp, path)
        changed.append(name)
    with _lock:
        state["installed"] = s["latest"]
    return {"ok": True, "version": s["latest"], "changed": changed}
