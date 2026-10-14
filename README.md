# PoE 2 Flip Finder

**English** · [Русский](README.ru.md) · [中文](README.zh.md)

Finds profitable currency exchange paths on the **Path of Exile 2** currency exchange (Alva).

The program reads GGG's official hourly data on real exchange trades and looks for chains of trades
that make a profit — for example `Divine → Chaos → X → Divine`. It runs on your computer, in your browser.

## Download

**[⬇ Latest version — poe2-flip.zip](https://github.com/zhurauljou/poe2-flip-finder/releases/latest)**

Unzip it and follow `How to start.txt`. In short — there's just one file to run:

- **Windows:** you need [Python 3.8+](https://www.python.org/downloads/) (tick “Add python.exe to PATH” during setup), then run `Start (Windows).bat`
  (after the first start a **PoE 2 Flip Finder** shortcut with a Divine Orb icon appears next to it — use it from then on).
- **Mac:** right-click `Start (Mac).command` → “Open”.

The program finds updates on its own: when a new version is out, an “Update” button appears at the top.
To check manually — “Check for updates” at the bottom of the filters panel.

## Features

- **Cycles and exchange paths** with profit, liquidity, time and gold fee.
- **“Holds”** — how many hours in a row a path has already been profitable, and a **score** tested on history:
  on top are the paths that most often stay profitable in the next hour too.
- **Per-pair noise margin** — noisy pairs get a bigger margin, stable ones a smaller one.
- **Steps panel**: what to give and what to get, rate, 24 h trends; you can enter the rate from the game and how much you actually got.
- **“At Alva” mode** — big, one step per screen (handy on a phone next to the game).
- **Trade journal** — your real spread per pair.
- **Notifications** on the page and in **Telegram** via the shared bot [@PoeFlipperBot](https://t.me/PoeFlipperBot) (or your own bot, for advanced users).
- **Pairs** — a reference of all pairs with your start currency: rate, liquidity, orders, 24 h trend, with search.
- **Presets** — “Safe / Fast / Profit” in one click; changed filters are marked and can be reset.
- **From a phone**: the 📱 button — turn on home Wi-Fi access and scan the QR code.
- Light/dark theme, Русский / English / 中文 / Українська / Беларуская.

## Data and privacy

Everything runs locally. Rates come from GGG's official API, names and icons from poe.ninja and RePoE.
The trade journal and the Telegram bot token are stored only on your machine (`%APPDATA%\poe2-flip` on Windows,
`~/.config/poe2-flip` on Mac) and are never sent anywhere.

## Important

These are **average rates of real trades per hour**, not the live order book. Always check the rate in game before trading.
No guarantee of profit. This project is not affiliated with Grinding Gear Games.
