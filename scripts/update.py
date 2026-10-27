#!/usr/bin/env python3
"""東証プライム市場の25日騰落レシオを計算して data/data.json に書き出す。

元データは東証(JPX)が公開している「商況プリント」(後場)のPDFにある、
内国株式・プライム市場の値上り銘柄数と値下り銘柄数です。
  25日騰落レシオ = 25営業日の値上り銘柄数の合計 ÷ 値下り銘柄数の合計 × 100
"""
import io
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from pypdf import PdfReader

BASE_URL = "https://www.jpx.co.jp/markets/equities/volume-and-value/tvdivq000000derc-att/2_{ymd}.pdf"
ROOT = Path(os.environ.get("TOURAKU_ROOT") or Path(__file__).resolve().parent.parent)
HISTORY_PATH = ROOT / "data" / "history.json"
OUTPUT_PATH = ROOT / "data" / "data.json"
JST = timezone(timedelta(hours=9))

WINDOW = 25          # 騰落レシオの期間(営業日)
LOOKBACK_DAYS = 90   # 初回に遡る日数(カレンダー日)
KEEP_SERIES = 60     # data.json に残す日数
KEEP_HISTORY = 200   # history.json に残す日数

STATS = Counter()
SAMPLES = []

# 「内国株式・プライム市場」の後ろに数値が14個続く行(値付/騰落銘柄数の表)
ROW = re.compile(r"内国株式・プライム市場((?:\s+[\d,]+(?:\.\d+)?){14})")


def extract_text(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def parse_counts(text, ymd):
    """PDFの文字列から (値上り銘柄数, 値下り銘柄数) を取り出す。"""
    if ymd not in text:
        print(f"警告: {ymd} の日付が本文に見つかりません。数値の整合性だけで確認します。")
    m = ROW.search(text)
    if not m:
        raise ValueError("プライム市場の騰落銘柄数の行が見つかりません")
    n = [float(x.replace(",", "")) for x in m.group(1).split()]
    traded, up, down, flat, none = int(n[2]), int(n[6]), int(n[8]), int(n[10]), int(n[12])
    if up + down + flat + none != traded:
        raise ValueError(f"銘柄数が合いません: 成立{traded} 値上り{up} 値下り{down} 変わらず{flat} 比較なし{none}")
    return up, down


def fetch_day(session, day):
    """その日の (値上り, 値下り) を返す。ファイルが無い日(休場日など)は None。"""
    ymd = day.strftime("%Y%m%d")
    url = BASE_URL.format(ymd=ymd)
    err = ""
    for attempt in range(4):
        try:
            r = session.get(url, timeout=30)
        except requests.RequestException as e:
            err = str(e)
            STATS["通信エラー"] += 1
        else:
            if r.status_code == 404:
                STATS["404(ファイルなし)"] += 1
                return None
            if r.status_code == 200:
                if not r.content.startswith(b"%PDF"):
                    STATS["200だがPDFではない"] += 1
                    if len(SAMPLES) < 2:
                        SAMPLES.append((url, r.headers.get("Content-Type", ""), r.content[:200]))
                    return None
                STATS["PDF取得"] += 1
                return parse_counts(extract_text(r.content), ymd)
            err = f"HTTP {r.status_code}"
            STATS[err] += 1
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url} を取得できませんでした: {err}")


def report():
    """取得結果の内訳を表示する(うまくいかない原因の調査用)。"""
    print("取得結果の内訳:", dict(STATS))
    for url, ctype, head in SAMPLES:
        print("PDFではない応答の例:", url, ctype, head)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def compute_ratios(days):
    """営業日ごとの25日騰落レシオを {日付: 値} で返す。"""
    keys = sorted(days)
    out = {}
    for i in range(WINDOW - 1, len(keys)):
        window = keys[i - WINDOW + 1 : i + 1]
        up = sum(days[k]["up"] for k in window)
        down = sum(days[k]["down"] for k in window)
        if down > 0:
            out[keys[i]] = round(up / down * 100, 2)
    return out


def main():
    now = datetime.now(JST)
    today = now.date()

    history = load_json(HISTORY_PATH, {"days": {}, "holidays": []})
    days = history["days"]
    holidays = set(history["holidays"])
    before_history = json.dumps(history, sort_keys=True)

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (touraku-updater)"

    added = 0
    for i in range(LOOKBACK_DAYS + 1):
        day = today - timedelta(days=i)
        if day.weekday() >= 5:
            continue
        key = day.isoformat()
        if key in days or key in holidays:
            continue
        counts = fetch_day(session, day)
        if counts is None:
            # 直近は公開前の可能性があるので、少し前の日だけ休場日として記録する
            if day < today - timedelta(days=3):
                holidays.add(key)
            continue
        days[key] = {"up": counts[0], "down": counts[1]}
        added += 1
        time.sleep(0.5)

    # 古い日を削る
    for k in sorted(days)[:-KEEP_HISTORY]:
        del days[k]
    history = {"days": dict(sorted(days.items())), "holidays": sorted(h for h in holidays if h >= min(days, default="0"))}
    if json.dumps(history, sort_keys=True) != before_history:
        save_json(HISTORY_PATH, history)

    report()
    ratios = compute_ratios(days)
    if not ratios:
        # 取得できた日数を保存して、日を重ねて25営業日になるのを待つ
        print(f"まだ計算できません(取得済み{len(days)}営業日、必要{WINDOW}営業日)。取得した日数は保存しました。")
        return

    old = load_json(OUTPUT_PATH, {})
    merged = {d: v for d, v in old.get("series", [])}
    merged.update(ratios)
    series = [[d, v] for d, v in sorted(merged.items())][-KEEP_SERIES:]

    if series != old.get("series"):
        save_json(OUTPUT_PATH, {
            "updated": now.isoformat(timespec="minutes"),
            "source": "東京証券取引所 商況プリント(内国株式・プライム市場)から計算",
            "series": series,
        })
        print(f"更新しました: 新規{added}日分、最新 {series[-1][0]} = {series[-1][1]}")
    else:
        print(f"変更なし: 最新 {series[-1][0]} = {series[-1][1]}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
