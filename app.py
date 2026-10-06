import json
import math
import os
import sqlite3
import threading
import time
import tkinter as tk
from datetime import datetime, time as dtime
from pathlib import Path
from tkinter import messagebox, ttk

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

try:
    import tkinter.font as tkfont
except Exception:
    tkfont = None

BASE = Path(__file__).resolve().parent
DB = BASE / "stock_assistant.db"
ENV = BASE / ".env"

load_dotenv(ENV)

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "15:35")
BUY_SCORE = int(os.getenv("BUY_SCORE", "3"))
SELL_SCORE = int(os.getenv("SELL_SCORE", "3"))


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS stocks(
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            shares INTEGER NOT NULL DEFAULT 0,
            avg_cost REAL NOT NULL DEFAULT 0,
            buy_target REAL,
            sell_target REAL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_price REAL,
            last_alert TEXT,
            last_alert_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS purchases(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            shares INTEGER NOT NULL,
            price REAL NOT NULL,
            purchased_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            price REAL NOT NULL,
            value REAL,
            profit REAL
        )
    """)
    con.commit()
    return con


def money(v):
    if v is None:
        return "-"
    return f"{v:,.0f}円"


def pct(v):
    return f"{v:+.2f}%"


def calc_average(rows):
    total_shares = sum(int(r["shares"]) for r in rows)
    total_cost = sum(int(r["shares"]) * float(r["price"]) for r in rows)
    return total_shares, (total_cost / total_shares if total_shares else 0)


def add_purchase(code, name, shares, price, buy_target, sell_target):
    con = db()
    now = datetime.now().isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO purchases(code,shares,price,purchased_at) VALUES(?,?,?,?)",
        (code, shares, price, now)
    )
    rows = con.execute(
        "SELECT shares, price FROM purchases WHERE code=?", (code,)
    ).fetchall()
    total_shares, avg = calc_average(rows)

    con.execute("""
        INSERT INTO stocks(code,name,shares,avg_cost,buy_target,sell_target)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(code) DO UPDATE SET
          name=excluded.name,
          shares=excluded.shares,
          avg_cost=excluded.avg_cost,
          buy_target=COALESCE(excluded.buy_target, stocks.buy_target),
          sell_target=COALESCE(excluded.sell_target, stocks.sell_target),
          enabled=1
    """, (code, name, total_shares, avg, buy_target, sell_target))
    con.commit()
    con.close()


def get_stocks():
    con = db()
    rows = con.execute("SELECT * FROM stocks WHERE enabled=1 ORDER BY code").fetchall()
    con.close()
    return rows


def get_purchases(code):
    con = db()
    rows = con.execute(
        "SELECT shares,price,purchased_at FROM purchases WHERE code=? ORDER BY purchased_at",
        (code,)
    ).fetchall()
    con.close()
    return rows


def save_price(code, price):
    con = db()
    row = con.execute("SELECT shares,avg_cost FROM stocks WHERE code=?", (code,)).fetchone()
    if row:
        value = float(price) * row["shares"]
        profit = value - float(row["avg_cost"]) * row["shares"]
        con.execute(
            "UPDATE stocks SET last_price=? WHERE code=?", (price, code)
        )
        con.execute("""
            INSERT INTO snapshots(code,captured_at,price,value,profit)
            VALUES(?,?,?,?,?)
        """, (code, datetime.now().isoformat(timespec="seconds"), price, value, profit))
        con.commit()
    con.close()


class PriceProvider:
    def price(self, code):
        """Yahoo Finance fallback. 日本株は .T を付与。"""
        ticker = code if "." in code else f"{code}.T"
        t = yf.Ticker(ticker)
        try:
            fi = t.fast_info
            p = fi.get("last_price")
            if p and not (isinstance(p, float) and math.isnan(p)):
                return float(p)
        except Exception:
            pass
        hist = t.history(period="1d", interval="1m", auto_adjust=False)
        if hist is None or hist.empty:
            raise RuntimeError(f"{code}: 株価を取得できませんでした")
        return float(hist["Close"].dropna().iloc[-1])

    def history(self, code):
        ticker = code if "." in code else f"{code}.T"
        return yf.Ticker(ticker).history(period="3mo", interval="1d", auto_adjust=False)

    def news(self, code):
        ticker = code if "." in code else f"{code}.T"
        try:
            data = yf.Ticker(ticker).news
            result = []
            for item in data[:5]:
                content = item.get("content", item)
                title = content.get("title") or item.get("title")
                link = content.get("canonicalUrl", {}).get("url") or item.get("link")
                if title:
                    result.append((title, link))
            return result
        except Exception:
            return []


provider = PriceProvider()


def indicators(code):
    h = provider.history(code)
    if h is None or len(h) < 30:
        return None

    close = h["Close"].astype(float)
    volume = h["Volume"].astype(float)

    sma25 = close.rolling(25).mean().iloc[-1]
    sma5 = close.rolling(5).mean().iloc[-1]

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = float((100 - (100 / (1 + rs))).iloc[-1])
    if math.isnan(rsi):
        rsi = 50.0

    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    change = (last / prev - 1) * 100 if prev else 0

    avg_volume = float(volume.rolling(20).mean().iloc[-1])
    today_volume = float(volume.iloc[-1])
    volume_ratio = today_volume / avg_volume if avg_volume else 1

    return {
        "price": last,
        "sma5": float(sma5),
        "sma25": float(sma25),
        "rsi": rsi,
        "change": change,
        "volume_ratio": volume_ratio,
    }


def signal_for(stock, ind):
    if not ind:
        return {"signal": "データ不足", "score": 0, "reasons": []}

    price = ind["price"]
    reasons_buy = []
    reasons_sell = []
    buy = sell = 0

    if stock["buy_target"] is not None and price <= stock["buy_target"]:
        buy += 2
        reasons_buy.append("設定した買い目標価格以下")
    if stock["sell_target"] is not None and price >= stock["sell_target"]:
        sell += 2
        reasons_sell.append("設定した売り目標価格以上")

    if ind["rsi"] <= 30:
        buy += 2
        reasons_buy.append(f"RSI {ind['rsi']:.1f}で売られすぎ傾向")
    elif ind["rsi"] >= 70:
        sell += 2
        reasons_sell.append(f"RSI {ind['rsi']:.1f}で買われすぎ傾向")

    if price < ind["sma25"]:
        buy += 1
        reasons_buy.append("25日移動平均線を下回る")
    elif price > ind["sma25"]:
        sell += 1
        reasons_sell.append("25日移動平均線を上回る")

    if ind["sma5"] > ind["sma25"]:
        buy += 1
        reasons_buy.append("短期移動平均が上向き")
    elif ind["sma5"] < ind["sma25"]:
        sell += 1
        reasons_sell.append("短期移動平均が弱い")

    if ind["volume_ratio"] >= 1.5:
        if ind["change"] > 0:
            buy += 1
            reasons_buy.append("出来高増加＋上昇")
        elif ind["change"] < 0:
            sell += 1
            reasons_sell.append("出来高増加＋下落")

    if buy >= BUY_SCORE and buy > sell:
        return {"signal": "🟢 買い候補", "score": buy, "reasons": reasons_buy}
    if sell >= SELL_SCORE and sell > buy:
        return {"signal": "🔴 売り候補", "score": sell, "reasons": reasons_sell}
    return {"signal": "⚪ 様子見", "score": max(buy, sell), "reasons": (reasons_buy if buy >= sell else reasons_sell)}


def line_send(text):
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    user_id = os.getenv("LINE_USER_ID", "").strip()
    if not token or not user_id:
        return False, "LINE設定がありません"

    url = "https://api.line.me/v2/bot/message/push"
    payload = {"to": user_id, "messages": [{"type": "text", "text": text[:5000]}]}
    r = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    if r.ok:
        return True, "LINE送信成功"
    return False, f"LINE送信失敗: HTTP {r.status_code} {r.text[:300]}"


def desktop_notify(title, message):
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(title, message)
        root.destroy()
    except Exception:
        pass


def should_alert(stock, signal_text):
    old = stock["last_alert"]
    if old == signal_text:
        return False
    return True


def mark_alert(code, signal_text):
    con = db()
    con.execute(
        "UPDATE stocks SET last_alert=?,last_alert_at=? WHERE code=?",
        (signal_text, datetime.now().isoformat(timespec="seconds"), code)
    )
    con.commit()
    con.close()


def build_daily_report():
    stocks = get_stocks()
    if not stocks:
        return "本日の保有株レポート\n登録銘柄がありません。"

    lines = ["📊 本日の投資まとめ", datetime.now().strftime("%Y/%m/%d"), ""]
    total_value = total_cost = 0.0
    details = []

    for s in stocks:
        if s["last_price"] is None:
            continue
        value = s["last_price"] * s["shares"]
        cost = s["avg_cost"] * s["shares"]
        profit = value - cost
        total_value += value
        total_cost += cost
        details.append((s["name"], s["code"], profit, s["last_price"], s["shares"]))

    total_profit = total_value - total_cost
    total_rate = total_profit / total_cost * 100 if total_cost else 0
    lines.append(f"評価額合計：{money(total_value)}")
    lines.append(f"含み損益：{money(total_profit)}（{pct(total_rate)}）")
    lines.append("")

    if details:
        best = max(details, key=lambda x: x[2])
        worst = min(details, key=lambda x: x[2])
        lines.append(f"利益最大：{best[0]} {money(best[2])}")
        lines.append(f"損失最大：{worst[0]} {money(worst[2])}")
        lines.append("")
        for s in stocks:
            if s["last_price"] is None:
                continue
            ind = indicators(s["code"])
            sig = signal_for(s, ind)
            lines.append(f"{sig['signal']} {s['name']}（{s['code']}）")
            if ind:
                lines.append(
                    f"  株価 {money(ind['price'])} / RSI {ind['rsi']:.1f} / 前日比 {pct(ind['change'])}"
                )
                if sig["reasons"]:
                    lines.append("  理由：" + "、".join(sig["reasons"][:3]))
        lines.append("")
        lines.append("※シグナルはルールによる候補判定で、利益を保証するものではありません。")
    else:
        lines.append("現在価格が取得できた銘柄がありません。")

    return "\n".join(lines)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("STOCK ASSISTANT PRO")
        self.geometry("1050x700")
        self.minsize(900, 600)
        self.monitoring = False
        self.last_report_date = None
        self.build_ui()
        self.refresh()

    def build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="STOCK ASSISTANT PRO", font=("Yu Gothic UI", 20, "bold")).pack(side="left")
        self.status = ttk.Label(top, text="停止中")
        self.status.pack(side="right")

        buttons = ttk.Frame(self, padding=(12, 0, 12, 8))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="銘柄追加", command=self.add_dialog).pack(side="left", padx=4)
        ttk.Button(buttons, text="価格更新", command=self.update_prices).pack(side="left", padx=4)
        ttk.Button(buttons, text="今すぐ分析", command=self.analyze).pack(side="left", padx=4)
        ttk.Button(buttons, text="今日のまとめ", command=self.show_report).pack(side="left", padx=4)
        self.monitor_btn = ttk.Button(buttons, text="監視開始", command=self.toggle_monitor)
        self.monitor_btn.pack(side="left", padx=4)

        self.tree = ttk.Treeview(
            self,
            columns=("code", "name", "shares", "avg", "price", "value", "profit", "signal"),
            show="headings",
            height=18
        )
        heads = [
            ("code", "コード", 80), ("name", "銘柄", 180), ("shares", "保有", 70),
            ("avg", "平均取得", 100), ("price", "現在値", 100), ("value", "評価額", 120),
            ("profit", "損益", 110), ("signal", "判定", 140)
        ]
        for c, h, w in heads:
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=12, pady=8)

        bottom = ttk.Frame(self, padding=12)
        bottom.pack(fill="x")
        self.summary = ttk.Label(bottom, text="", justify="left")
        self.summary.pack(side="left")
        self.log = tk.Text(bottom, height=7, wrap="word")
        self.log.pack(side="right", fill="x", expand=True, padx=(20, 0))

    def write_log(self, text):
        self.log.insert("end", datetime.now().strftime("[%H:%M:%S] ") + text + "\n")
        self.log.see("end")

    def refresh(self):
        for x in self.tree.get_children():
            self.tree.delete(x)

        total_value = total_cost = 0
        for s in get_stocks():
            price = s["last_price"]
            value = price * s["shares"] if price is not None else None
            cost = s["avg_cost"] * s["shares"]
            profit = value - cost if value is not None else None
            if value is not None:
                total_value += value
                total_cost += cost

            signal = "—"
            try:
                signal = signal_for(s, indicators(s["code"]))["signal"]
            except Exception:
                pass

            self.tree.insert("", "end", values=(
                s["code"], s["name"], s["shares"], money(s["avg_cost"]),
                money(price), money(value), money(profit), signal
            ))

        p = total_value - total_cost
        r = p / total_cost * 100 if total_cost else 0
        self.summary.config(
            text=f"評価額：{money(total_value)}    投資元本：{money(total_cost)}    "
                 f"含み損益：{money(p)}（{pct(r)}）"
        )

    def add_dialog(self):
        win = tk.Toplevel(self)
        win.title("保有株を追加")
        win.resizable(False, False)
        fields = [
            ("銘柄コード", "7203"),
            ("銘柄名", "トヨタ自動車"),
            ("購入株数", "100"),
            ("購入価格", "2500"),
            ("買い目標価格（任意）", ""),
            ("売り目標価格（任意）", ""),
        ]
        entries = {}
        for i, (label, default) in enumerate(fields):
            ttk.Label(win, text=label).grid(row=i, column=0, padx=10, pady=6, sticky="w")
            e = ttk.Entry(win, width=25)
            e.insert(0, default)
            e.grid(row=i, column=1, padx=10, pady=6)
            entries[label] = e

        def save():
            try:
                code = entries["銘柄コード"].get().strip()
                name = entries["銘柄名"].get().strip()
                shares = int(entries["購入株数"].get())
                price = float(entries["購入価格"].get())
                bt = entries["買い目標価格（任意）"].get().strip()
                st = entries["売り目標価格（任意）"].get().strip()
                buy_target = float(bt) if bt else None
                sell_target = float(st) if st else None
                if not code or not name or shares <= 0 or price <= 0:
                    raise ValueError
                add_purchase(code, name, shares, price, buy_target, sell_target)
                win.destroy()
                self.refresh()
                self.write_log(f"{name}（{code}）を登録しました。")
            except Exception:
                messagebox.showerror("入力エラー", "入力内容を確認してください。")

        ttk.Button(win, text="登録", command=save).grid(
            row=len(fields), column=0, columnspan=2, pady=12
        )

    def update_prices(self):
        self.status.config(text="株価更新中…")
        def work():
            for s in get_stocks():
                try:
                    p = provider.price(s["code"])
                    save_price(s["code"], p)
                    self.after(0, lambda s=s, p=p: self.write_log(f"{s['name']} {money(p)}"))
                except Exception as e:
                    self.after(0, lambda e=e, s=s: self.write_log(f"{s['name']} 更新失敗: {e}"))
            self.after(0, self.refresh)
            self.after(0, lambda: self.status.config(text="更新完了"))
        threading.Thread(target=work, daemon=True).start()

    def analyze(self):
        def work():
            for s in get_stocks():
                try:
                    ind = indicators(s["code"])
                    sig = signal_for(s, ind)
                    self.after(0, lambda s=s, sig=sig: self.write_log(
                        f"{s['name']}：{sig['signal']} / " + "、".join(sig["reasons"][:3])
                    ))
                    if sig["signal"] in ("🟢 買い候補", "🔴 売り候補") and should_alert(s, sig["signal"]):
                        text = (
                            f"{sig['signal']}\n"
                            f"{s['name']}（{s['code']}）\n"
                            f"現在値：{money(ind['price'])}\n"
                            f"RSI：{ind['rsi']:.1f}\n"
                            f"前日比：{pct(ind['change'])}\n"
                            f"理由：{'、'.join(sig['reasons'][:4])}\n\n"
                            "※投資判断を保証するものではありません。"
                        )
                        ok, msg = line_send(text)
                        mark_alert(s["code"], sig["signal"])
                        self.after(0, lambda msg=msg: self.write_log(msg))
                        self.after(0, lambda text=text, sig=sig: desktop_notify(sig["signal"], text))
                except Exception as e:
                    self.after(0, lambda e=e: self.write_log(f"分析失敗: {e}"))
            self.after(0, self.refresh)
        threading.Thread(target=work, daemon=True).start()

    def show_report(self):
        report = build_daily_report()
        win = tk.Toplevel(self)
        win.title("本日の投資まとめ")
        win.geometry("750x600")
        text = tk.Text(win, wrap="word", font=("Yu Gothic UI", 11))
        text.pack(fill="both", expand=True, padx=10, pady=10)
        text.insert("1.0", report)
        text.config(state="disabled")
        ttk.Button(win, text="LINEへ送信", command=lambda: self.send_report(report)).pack(pady=8)

    def send_report(self, report):
        ok, msg = line_send(report)
        self.write_log(msg)
        if ok:
            messagebox.showinfo("通知", "LINEへ送信しました。")
        else:
            messagebox.showwarning("通知", msg)

    def toggle_monitor(self):
        if self.monitoring:
            self.monitoring = False
            self.monitor_btn.config(text="監視開始")
            self.status.config(text="停止中")
            return
        self.monitoring = True
        self.monitor_btn.config(text="監視停止")
        self.status.config(text="監視中")
        threading.Thread(target=self.monitor_loop, daemon=True).start()

    def monitor_loop(self):
        while self.monitoring:
            self.update_prices()
            time.sleep(CHECK_INTERVAL)
            if not self.monitoring:
                break
            self.analyze()
            now = datetime.now()
            report_h, report_m = map(int, DAILY_REPORT_TIME.split(":"))
            if now.hour == report_h and now.minute == report_m and self.last_report_date != now.date():
                self.last_report_date = now.date()
                report = build_daily_report()
                ok, msg = line_send(report)
                self.after(0, lambda msg=msg: self.write_log(msg))
                self.after(0, lambda: desktop_notify("本日の投資まとめ", report[:1500]))

    def on_close(self):
        self.monitoring = False
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
