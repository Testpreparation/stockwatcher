import os
import json
import sqlite3
import threading
import time
from datetime import datetime, date
from urllib.parse import quote
from urllib.request import Request, urlopen

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

try:
    import yfinance as yf
except Exception:
    yf = None

try:
    import requests
except Exception:
    requests = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


DB_FILE = "stocks.db"


def normalize_ticker(code: str) -> str:
    """
    日本株コードをYahoo Finance形式に変換する。
    7203 → 7203.T
    7203.T → 7203.T
    """
    code = str(code).strip().upper()

    if code.isdigit() and len(code) in (4, 5):
        return f"{code}.T"

    if code.endswith(".T"):
        return code

    return code


class PriceProvider:
    """
    株価取得クラス。

    1. yfinance
    2. Yahoo Finance Chart API
    の順番で取得する。
    """

    def __init__(self):

        if yf is not None:

            try:
                yf.config.network.retries = 2
            except Exception:
                pass

            try:
                yf.config.locale.lang = "ja-JP"
                yf.config.locale.region = "JP"
            except Exception:
                pass

    def _from_yfinance(self, ticker):

        if yf is None:
            return None

        history = yf.Ticker(ticker).history(
            period="3mo",
            interval="1d",
            auto_adjust=False,
            actions=False,
        )

        if history is None or history.empty:
            return None

        closes = []
        volumes = []

        for value in history["Close"].tolist():

            if value is not None:

                try:
                    closes.append(float(value))
                except (TypeError, ValueError):
                    pass

        if "Volume" in history.columns:

            for value in history["Volume"].tolist():

                if value is not None:

                    try:
                        volumes.append(float(value))
                    except (TypeError, ValueError):
                        pass

        if not closes:
            return None

        return {
            "price": closes[-1],
            "previous_close": (
                closes[-2]
                if len(closes) >= 2
                else closes[-1]
            ),
            "closes": closes,
            "volume": (
                volumes[-1]
                if volumes
                else 0.0
            ),
            "source": "yfinance",
        }

    def _from_yahoo_chart(self, ticker):

        encoded = quote(ticker, safe="")

        urls = [

            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{encoded}?range=3mo&interval=1d"
            f"&events=history&includeAdjustedClose=true",

            f"https://query2.finance.yahoo.com/v8/finance/chart/"
            f"{encoded}?range=3mo&interval=1d"
            f"&events=history&includeAdjustedClose=true",
        ]

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/154.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        }

        errors = []

        for url in urls:

            try:

                request = Request(
                    url,
                    headers=headers
                )

                with urlopen(
                    request,
                    timeout=15
                ) as response:

                    payload = json.loads(
                        response.read().decode("utf-8")
                    )

                result = (
                    payload
                    .get("chart", {})
                    .get("result")
                )

                if not result:
                    continue

                quote_data = (
                    result[0]
                    .get("indicators", {})
                    .get("quote", [{}])[0]
                )

                closes = [
                    float(v)
                    for v in quote_data.get("close", [])
                    if v is not None
                ]

                volumes = [
                    float(v)
                    for v in quote_data.get("volume", [])
                    if v is not None
                ]

                if not closes:
                    continue

                return {
                    "price": closes[-1],

                    "previous_close": (
                        closes[-2]
                        if len(closes) >= 2
                        else closes[-1]
                    ),

                    "closes": closes,

                    "volume": (
                        volumes[-1]
                        if volumes
                        else 0.0
                    ),

                    "source": "Yahoo Finance Chart API",
                }

            except Exception as exc:

                errors.append(str(exc))

        if errors:
            raise RuntimeError(
                " / ".join(errors)
            )

        return None

    def get_price(self, code):

        ticker = normalize_ticker(code)

        errors = []

        # -------------------------
        # ① yfinance
        # -------------------------

        try:

            data = self._from_yfinance(ticker)

            if data:
                return data

        except Exception as exc:

            errors.append(
                f"yfinance: {exc}"
            )

        # -------------------------
        # ② Yahoo Finance API
        # -------------------------

        try:

            data = self._from_yahoo_chart(ticker)

            if data:
                return data

        except Exception as exc:

            errors.append(
                f"Yahoo API: {exc}"
            )

        detail = (
            " / ".join(errors)
            if errors
            else "価格データが空でした"
        )

        raise RuntimeError(
            f"{ticker} の株価を取得できませんでした。"
            f"{detail}"
        )


class StockDB:

    def __init__(self, path=DB_FILE):

        self.conn = sqlite3.connect(
            path,
            check_same_thread=False
        )

        self.lock = threading.Lock()

        self._create_tables()

    def _create_tables(self):

        with self.lock:

            cur = self.conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS stocks (

                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    code TEXT UNIQUE NOT NULL,

                    name TEXT NOT NULL,

                    target_buy REAL DEFAULT 0,

                    target_sell REAL DEFAULT 0,

                    current_price REAL DEFAULT 0,

                    previous_close REAL DEFAULT 0,

                    volume REAL DEFAULT 0,

                    updated_at TEXT

                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS purchases (

                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    stock_id INTEGER NOT NULL,

                    shares INTEGER NOT NULL,

                    price REAL NOT NULL,

                    purchased_at TEXT NOT NULL,

                    FOREIGN KEY(stock_id)
                    REFERENCES stocks(id)

                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS alerts (

                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    code TEXT NOT NULL,

                    alert_type TEXT NOT NULL,

                    alert_key TEXT NOT NULL,

                    created_at TEXT NOT NULL,

                    UNIQUE(
                        code,
                        alert_type,
                        alert_key
                    )

                )
            """)

            self.conn.commit()

    def add_stock(self, code, name):

        with self.lock:

            self.conn.execute(
                """
                INSERT OR IGNORE INTO stocks
                (
                    code,
                    name,
                    updated_at
                )
                VALUES (?, ?, ?)
                """,
                (
                    code,
                    name,
                    datetime.now().isoformat(
                        timespec="seconds"
                    )
                )
            )

            self.conn.commit()

    def add_purchase(
        self,
        code,
        shares,
        price
    ):

        with self.lock:

            cur = self.conn.cursor()

            cur.execute(
                "SELECT id FROM stocks WHERE code=?",
                (code,)
            )

            row = cur.fetchone()

            if not row:

                raise ValueError(
                    "先に銘柄を登録してください。"
                )

            cur.execute(
                """
                INSERT INTO purchases
                (
                    stock_id,
                    shares,
                    price,
                    purchased_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    row[0],
                    shares,
                    price,
                    datetime.now().isoformat(
                        timespec="seconds"
                    )
                )
            )

            self.conn.commit()

    def update_price(
        self,
        code,
        data
    ):

        with self.lock:

            self.conn.execute(
                """
                UPDATE stocks

                SET
                    current_price=?,
                    previous_close=?,
                    volume=?,
                    updated_at=?

                WHERE code=?
                """,
                (
                    data["price"],
                    data["previous_close"],
                    data["volume"],
                    datetime.now().isoformat(
                        timespec="seconds"
                    ),
                    code
                )
            )

            self.conn.commit()

    def set_targets(
        self,
        code,
        buy_price,
        sell_price
    ):

        with self.lock:

            self.conn.execute(
                """
                UPDATE stocks

                SET
                    target_buy=?,
                    target_sell=?

                WHERE code=?
                """,
                (
                    buy_price,
                    sell_price,
                    code
                )
            )

            self.conn.commit()

    def get_stocks(self):

        with self.lock:

            cur = self.conn.cursor()

            cur.execute("""
                SELECT

                    s.id,
                    s.code,
                    s.name,
                    s.target_buy,
                    s.target_sell,
                    s.current_price,
                    s.previous_close,
                    s.volume,
                    s.updated_at,

                    COALESCE(
                        SUM(p.shares),
                        0
                    ),

                    COALESCE(
                        SUM(
                            p.shares * p.price
                        ),
                        0
                    )

                FROM stocks s

                LEFT JOIN purchases p
                ON p.stock_id = s.id

                GROUP BY s.id

                ORDER BY s.code
            """)

            rows = cur.fetchall()

        result = []

        for row in rows:

            (
                stock_id,
                code,
                name,
                target_buy,
                target_sell,
                current_price,
                previous_close,
                volume,
                updated_at,
                shares,
                total_cost
            ) = row

            average = (
                total_cost / shares
                if shares
                else 0
            )

            value = (
                current_price * shares
                if current_price
                else 0
            )

            profit = (
                value - total_cost
                if shares
                else 0
            )

            rate = (
                profit / total_cost * 100
                if total_cost
                else 0
            )

            result.append({

                "id": stock_id,

                "code": code,

                "name": name,

                "target_buy": (
                    target_buy or 0
                ),

                "target_sell": (
                    target_sell or 0
                ),

                "current_price": (
                    current_price or 0
                ),

                "previous_close": (
                    previous_close or 0
                ),

                "volume": (
                    volume or 0
                ),

                "updated_at": (
                    updated_at or ""
                ),

                "shares": shares,

                "total_cost": total_cost,

                "average": average,

                "value": value,

                "profit": profit,

                "rate": rate,
            })

        return result

    def has_alert(
        self,
        code,
        alert_type,
        alert_key
    ):

        with self.lock:

            row = self.conn.execute(
                """
                SELECT 1

                FROM alerts

                WHERE
                    code=?
                    AND alert_type=?
                    AND alert_key=?
                """,
                (
                    code,
                    alert_type,
                    alert_key
                )
            ).fetchone()

            return row is not None

    def add_alert(
        self,
        code,
        alert_type,
        alert_key
    ):

        with self.lock:

            self.conn.execute(
                """
                INSERT OR IGNORE INTO alerts
                (
                    code,
                    alert_type,
                    alert_key,
                    created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    code,
                    alert_type,
                    alert_key,
                    datetime.now().isoformat(
                        timespec="seconds"
                    )
                )
            )

            self.conn.commit()


class StockWatcherApp:

    def __init__(self, root):

        self.root = root

        self.root.title(
            "Stock Watcher"
        )

        self.root.geometry(
            "1180x700"
        )

        self.root.minsize(
            1000,
            600
        )

        self.db = StockDB()

        self.provider = PriceProvider()

        self.monitoring = False

        self.monitor_thread = None

        self.line_token = os.getenv(
            "LINE_CHANNEL_ACCESS_TOKEN",
            ""
        ).strip()

        self.line_user_id = os.getenv(
            "LINE_USER_ID",
            ""
        ).strip()

        self._build_ui()

        self.refresh_table()

    def _build_ui(self):

        top = ttk.Frame(
            self.root,
            padding=10
        )

        top.pack(fill="x")

        ttk.Label(
            top,
            text="銘柄コード"
        ).grid(
            row=0,
            column=0,
            padx=5,
            pady=5
        )

        self.code_var = tk.StringVar()

        ttk.Entry(
            top,
            textvariable=self.code_var,
            width=12
        ).grid(
            row=0,
            column=1,
            padx=5
        )

        ttk.Label(
            top,
            text="銘柄名"
        ).grid(
            row=0,
            column=2,
            padx=5
        )

        self.name_var = tk.StringVar()

        ttk.Entry(
            top,
            textvariable=self.name_var,
            width=18
        ).grid(
            row=0,
            column=3,
            padx=5
        )

        ttk.Button(
            top,
            text="銘柄登録",
            command=self.register_stock
        ).grid(
            row=0,
            column=4,
            padx=8
        )

        ttk.Button(
            top,
            text="株価更新",
            command=self.update_prices_async
        ).grid(
            row=0,
            column=5,
            padx=8
        )

        ttk.Button(
            top,
            text="目標価格設定",
            command=self.set_target
        ).grid(
            row=0,
            column=6,
            padx=8
        )

        ttk.Button(
            top,
            text="日次レポート",
            command=self.show_daily_report
        ).grid(
            row=0,
            column=7,
            padx=8
        )

        purchase = ttk.LabelFrame(
            self.root,
            text="買付登録",
            padding=10
        )

        purchase.pack(
            fill="x",
            padx=10,
            pady=(0, 10)
        )

        ttk.Label(
            purchase,
            text="銘柄コード"
        ).grid(
            row=0,
            column=0,
            padx=5
        )

        self.purchase_code_var = tk.StringVar()

        ttk.Entry(
            purchase,
            textvariable=self.purchase_code_var,
            width=12
        ).grid(
            row=0,
            column=1,
            padx=5
        )

        ttk.Label(
            purchase,
            text="株数"
        ).grid(
            row=0,
            column=2,
            padx=5
        )

        self.shares_var = tk.StringVar()

        ttk.Entry(
            purchase,
            textvariable=self.shares_var,
            width=10
        ).grid(
            row=0,
            column=3,
            padx=5
        )

        ttk.Label(
            purchase,
            text="購入価格"
        ).grid(
            row=0,
            column=4,
            padx=5
        )

        self.purchase_price_var = tk.StringVar()

        ttk.Entry(
            purchase,
            textvariable=self.purchase_price_var,
            width=12
        ).grid(
            row=0,
            column=5,
            padx=5
        )

        ttk.Button(
            purchase,
            text="買付登録",
            command=self.register_purchase
        ).grid(
            row=0,
            column=6,
            padx=8
        )

        controls = ttk.Frame(
            self.root,
            padding=(10, 0, 10, 10)
        )

        controls.pack(
            fill="x"
        )

        self.monitor_var = tk.StringVar(
            value="自動監視：停止中"
        )

        ttk.Label(
            controls,
            textvariable=self.monitor_var
        ).pack(
            side="left"
        )

        ttk.Button(
            controls,
            text="自動監視開始/停止",
            command=self.toggle_monitoring
        ).pack(
            side="left",
            padx=15
        )

        ttk.Button(
            controls,
            text="LINEテスト",
            command=self.test_line
        ).pack(
            side="left"
        )

        self.status_var = tk.StringVar(
            value="準備完了"
        )

        ttk.Label(
            controls,
            textvariable=self.status_var
        ).pack(
            side="right"
        )

        columns = (
            "code",
            "name",
            "shares",
            "average",
            "current",
            "value",
            "profit",
            "rate",
            "updated"
        )

        frame = ttk.Frame(
            self.root,
            padding=10
        )

        frame.pack(
            fill="both",
            expand=True
        )

        self.tree = ttk.Treeview(
            frame,
            columns=columns,
            show="headings"
        )

        headings = {

            "code": "コード",

            "name": "銘柄名",

            "shares": "保有株数",

            "average": "平均取得価格",

            "current": "現在価格",

            "value": "評価額",

            "profit": "損益",

            "rate": "損益率",

            "updated": "更新時刻",
        }

        widths = {

            "code": 90,

            "name": 150,

            "shares": 90,

            "average": 110,

            "current": 110,

            "value": 120,

            "profit": 110,

            "rate": 90,

            "updated": 160,
        }

        for col in columns:

            self.tree.heading(
                col,
                text=headings[col]
            )

            self.tree.column(
                col,
                width=widths[col],
                anchor="center"
            )

        scrollbar = ttk.Scrollbar(
            frame,
            orient="vertical",
            command=self.tree.yview
        )

        self.tree.configure(
            yscrollcommand=scrollbar.set
        )

        self.tree.pack(
            side="left",
            fill="both",
            expand=True
        )

        scrollbar.pack(
            side="right",
            fill="y"
        )

    def register_stock(self):

        code = self.code_var.get().strip()

        name = self.name_var.get().strip()

        if not code or not name:

            messagebox.showwarning(
                "入力不足",
                "銘柄コードと銘柄名を入力してください。"
            )

            return

        code = normalize_ticker(code)

        self.db.add_stock(
            code,
            name
        )

        self.status_var.set(
            f"{code} を登録しました"
        )

        self.refresh_table()

    def register_purchase(self):

        code = normalize_ticker(
            self.purchase_code_var.get().strip()
        )

        try:

            shares = int(
                self.shares_var.get()
            )

            price = float(
                self.purchase_price_var.get()
            )

            if shares <= 0 or price <= 0:
                raise ValueError

        except ValueError:

            messagebox.showwarning(
                "入力エラー",
                "株数と購入価格を正しく入力してください。"
            )

            return

        try:

            self.db.add_purchase(
                code,
                shares,
                price
            )

        except Exception as exc:

            messagebox.showerror(
                "登録エラー",
                str(exc)
            )

            return

        self.status_var.set(
            f"{code} の買付を登録しました"
        )

        self.refresh_table()

    def set_target(self):

        selected = self.tree.selection()

        if not selected:

            messagebox.showwarning(
                "未選択",
                "目標価格を設定する銘柄を選択してください。"
            )

            return

        code = self.tree.item(
            selected[0]
        )["values"][0]

        buy = simpledialog.askfloat(
            "買い目標価格",
            f"{code} の買い目標価格を入力してください。\n不要なら0",
            minvalue=0
        )

        if buy is None:
            return

        sell = simpledialog.askfloat(
            "売り目標価格",
            f"{code} の売り目標価格を入力してください。\n不要なら0",
            minvalue=0
        )

        if sell is None:
            return

        self.db.set_targets(
            code,
            buy,
            sell
        )

        self.status_var.set(
            f"{code} の目標価格を更新しました"
        )

    def update_prices_async(self):

        threading.Thread(
            target=self._update_prices_worker,
            daemon=True
        ).start()

    def _update_prices_worker(self):

        self.root.after(
            0,
            lambda: self.status_var.set(
                "株価を取得中..."
            )
        )

        stocks = self.db.get_stocks()

        if not stocks:

            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "株価更新",
                    "登録されている銘柄がありません。"
                )
            )

            return

        success = 0

        errors = []

        for stock in stocks:

            try:

                data = self.provider.get_price(
                    stock["code"]
                )

                self.db.update_price(
                    stock["code"],
                    data
                )

                success += 1

                signal = self.calculate_signal(
                    stock,
                    data
                )

                self.send_signal_if_needed(
                    stock,
                    data,
                    signal
                )

            except Exception as exc:

                errors.append(
                    f'{stock["code"]}: {exc}'
                )

        self.root.after(
            0,
            self.refresh_table
        )

        if errors:

            message = (
                f"{success}銘柄更新しました。\n\n"
                + "\n".join(
                    errors[:5]
                )
            )

            if len(errors) > 5:

                message += (
                    f"\n...ほか "
                    f"{len(errors) - 5}件"
                )

            self.root.after(
                0,
                lambda m=message:
                messagebox.showwarning(
                    "株価更新結果",
                    m
                )
            )

            self.root.after(
                0,
                lambda:
                self.status_var.set(
                    "一部更新失敗"
                )
            )

        else:

            self.root.after(
                0,
                lambda:
                self.status_var.set(
                    f"{success}銘柄の株価を更新しました"
                )
            )

    def calculate_signal(
        self,
        stock,
        data
    ):

        price = data["price"]

        closes = data.get(
            "closes",
            []
        )

        score = 0

        reasons = []

        if (
            stock["target_buy"] > 0
            and price <= stock["target_buy"]
        ):

            score += 2

            reasons.append(
                "買い目標価格以下"
            )

        if (
            stock["target_sell"] > 0
            and price >= stock["target_sell"]
        ):

            score -= 2

            reasons.append(
                "売り目標価格以上"
            )

        if len(closes) >= 25:

            sma5 = (
                sum(closes[-5:]) / 5
            )

            sma25 = (
                sum(closes[-25:]) / 25
            )

            if sma5 > sma25:

                score += 1

                reasons.append(
                    "短期移動平均が中期移動平均を上回る"
                )

            elif sma5 < sma25:

                score -= 1

                reasons.append(
                    "短期移動平均が中期移動平均を下回る"
                )

        if price > data["previous_close"]:

            reasons.append(
                "前日終値より上昇"
            )

        elif price < data["previous_close"]:

            reasons.append(
                "前日終値より下落"
            )

        if score >= 2:

            label = "買い候補"

        elif score <= -2:

            label = "売り候補"

        else:

            label = "様子見"

        return {

            "label": label,

            "score": score,

            "reasons": reasons,
        }

    def send_signal_if_needed(
        self,
        stock,
        data,
        signal
    ):

        if signal["label"] == "様子見":
            return

        today = date.today().isoformat()

        if self.db.has_alert(
            stock["code"],
            signal["label"],
            today
        ):

            return

        message = (

            "【株価シグナル】\n"

            f"{stock['name']} "
            f"({stock['code']})\n"

            f"現在価格："
            f"{data['price']:,.0f}円\n"

            f"判定："
            f"{signal['label']}\n"

            f"理由："
            f"{'、'.join(signal['reasons'])}"
        )

        self.send_line(
            message
        )

        self.db.add_alert(
            stock["code"],
            signal["label"],
            today
        )

        self.root.after(
            0,
            lambda m=message:
            messagebox.showinfo(
                "株価シグナル",
                m
            )
        )

    def send_line(self, message):

        if (
            not self.line_token
            or not self.line_user_id
            or requests is None
        ):

            return False

        try:

            response = requests.post(

                "https://api.line.me/v2/bot/message/push",

                headers={

                    "Content-Type":
                    "application/json",

                    "Authorization":
                    f"Bearer {self.line_token}",
                },

                json={

                    "to":
                    self.line_user_id,

                    "messages": [

                        {
                            "type": "text",

                            "text":
                            message[:5000]
                        }

                    ],
                },

                timeout=15,
            )

            return response.ok

        except Exception:

            return False

    def test_line(self):

        if (
            not self.line_token
            or not self.line_user_id
        ):

            messagebox.showwarning(
                "LINE設定",
                "LINE_CHANNEL_ACCESS_TOKEN と LINE_USER_ID を設定してください。"
            )

            return

        if self.send_line(
            "Stock Watcher のLINE通知テストです。"
        ):

            messagebox.showinfo(
                "LINE",
                "LINE通知を送信しました。"
            )

        else:

            messagebox.showerror(
                "LINE",
                "LINE通知の送信に失敗しました。"
            )

    def toggle_monitoring(self):

        if self.monitoring:

            self.monitoring = False

            self.monitor_var.set(
                "自動監視：停止中"
            )

            self.status_var.set(
                "自動監視を停止しました"
            )

            return

        self.monitoring = True

        self.monitor_var.set(
            "自動監視：稼働中"
        )

        self.status_var.set(
            "自動監視を開始しました"
        )

        self.monitor_thread = threading.Thread(
            target=self.monitor_loop,
            daemon=True
        )

        self.monitor_thread.start()

    def monitor_loop(self):

        while self.monitoring:

            self._update_prices_worker()

            for _ in range(300):

                if not self.monitoring:
                    break

                time.sleep(1)

    def show_daily_report(self):

        stocks = self.db.get_stocks()

        if not stocks:

            messagebox.showinfo(
                "日次レポート",
                "登録銘柄がありません。"
            )

            return

        total_cost = sum(
            s["total_cost"]
            for s in stocks
        )

        total_value = sum(
            s["value"]
            for s in stocks
        )

        total_profit = (
            total_value - total_cost
        )

        lines = [

            f"日次レポート"
            f"（{date.today().isoformat()}）",

            "",

            f"取得総額："
            f"{total_cost:,.0f}円",

            f"評価総額："
            f"{total_value:,.0f}円",

            f"含み損益："
            f"{total_profit:+,.0f}円",

            "",
        ]

        for stock in stocks:

            if stock["shares"] <= 0:
                continue

            change = (
                stock["current_price"]
                - stock["previous_close"]
            )

            change_rate = (

                change
                / stock["previous_close"]
                * 100

                if stock["previous_close"]
                else 0
            )

            if change > 0:
                direction = "上昇"

            elif change < 0:
                direction = "下落"

            else:
                direction = "変化なし"

            lines.append(

                f"{stock['name']} "
                f"({stock['code']})："

                f"{stock['current_price']:,.0f}円 / "

                f"損益 "
                f"{stock['profit']:+,.0f}円 / "

                f"前日比 "
                f"{change:+,.0f}円 "

                f"({change_rate:+.2f}%) / "

                f"{direction}"
            )

        report = "\n".join(
            lines
        )

        window = tk.Toplevel(
            self.root
        )

        window.title(
            "日次レポート"
        )

        window.geometry(
            "800x500"
        )

        text = tk.Text(
            window,
            wrap="word"
        )

        text.pack(
            fill="both",
            expand=True,
            padx=10,
            pady=10
        )

        text.insert(
            "1.0",
            report
        )

        text.configure(
            state="disabled"
        )

        ttk.Button(
            window,
            text="LINEへ送信",
            command=lambda:
            self.send_daily_report_to_line(
                report
            )
        ).pack(
            pady=(0, 10)
        )

    def send_daily_report_to_line(
        self,
        report
    ):

        if self.send_line(
            report
        ):

            messagebox.showinfo(
                "LINE",
                "日次レポートをLINEへ送信しました。"
            )

        else:

            messagebox.showerror(
                "LINE",
                "LINEへの送信に失敗しました。"
            )

    def refresh_table(self):

        for item in self.tree.get_children():

            self.tree.delete(
                item
            )

        for stock in self.db.get_stocks():

            self.tree.insert(

                "",

                "end",

                values=(

                    stock["code"],

                    stock["name"],

                    f"{stock['shares']:,}",

                    f"{stock['average']:,.2f}",

                    f"{stock['current_price']:,.2f}",

                    f"{stock['value']:,.0f}",

                    f"{stock['profit']:+,.0f}",

                    f"{stock['rate']:+.2f}%",

                    stock["updated_at"].replace(
                        "T",
                        " "
                    ),
                )
            )


def main():

    root = tk.Tk()

    StockWatcherApp(
        root
    )

    root.mainloop()


if __name__ == "__main__":

    main()
