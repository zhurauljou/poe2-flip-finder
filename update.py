"""Обновление программы из релизов GitHub.

Раз в несколько часов сервер спрашивает у GitHub последний релиз. Если его версия новее — страница предлагает
обновиться. Установка скачивает архив релиза (тот же poe2-flip.zip, что собирает «Собрать архив для передачи»)
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


def status(current):
    with _lock:
        s = dict(state)
    s["current"] = current
    s["available"] = bool(s["latest"] and s["asset"] and parse_version(s["latest"]) > parse_version(current)
                          and s["installed"] != s["latest"])
    return s


def install(here, files, current, ua):
    """Скачивает архив релиза и заменяет файлы программы. Возвращает, что изменилось."""
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
    # В архиве файлы лежат в папке poe2-flip/ — берём только известные файлы программы, без путей из архива.
    new = {}
    for info in z.infolist():
        name = info.filename.replace("\\", "/").split("/")[-1]
        if name in files and not info.is_dir():
            new[name] = (z.read(info), info.external_attr >> 16)
    if "server.py" not in new or "index.html" not in new:
        raise RuntimeError("в архиве нет файлов программы")
    changed = []
    for name, (content, mode) in new.items():
        path = os.path.join(here, name)
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
