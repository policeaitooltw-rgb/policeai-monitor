#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
【選用｜軌 B】Google 帳單每日對帳
=================================
兩把金鑰同一 GCP 專案 → Google 帳單以「專案」計、拆不開逐把金鑰、且延遲數小時。
所以「即時」由 api_monitor.py 的 proxy 估算負責；這支只做每天一次的【對帳】：
    Google 專案今日 Generative Language API 實際花費（延遲值）
      vs
    你伺服器 /admin/stats 的 proxy 估算
把兩者差異推一則到 Telegram，長期用來校準 _PRICING 準不準。

前置需求（一次性）：
  1. 到 GCP 開「Cloud Billing 匯出到 BigQuery」（標準用量成本匯出）。
     （這是唯一能用程式讀到花費金額的官方途徑；沒開就只有網頁報表看得到。）
  2. 本機裝 gcloud CLI 並登入：gcloud auth login（或用服務帳戶）。
  3. 填下面三個變數，或用環境變數：
       BQ_TABLE  = "專案ID.billing_dataset.gcp_billing_export_v1_XXXXXX"
       GCP_PROJECT = "你的專案ID"（可留空＝不篩專案，抓整個匯出表）
  4. 建議用排程器每天跑一次（例如台灣時間隔天早上，讓昨天的帳單資料落定）。

用法：
    py -3.13 gcp_reconcile.py            # 對帳「今天」（資料多半還沒齊，僅供概覽）
    py -3.13 gcp_reconcile.py --date 2026-07-20   # 對帳指定日（推薦：對昨天）
"""
import os
import sys
import json
import argparse
import datetime
import subprocess

import requests

# ── 設定（可用環境變數覆蓋）───────────────────────────────────────────
BQ_TABLE    = os.environ.get("BQ_TABLE", "PROJECT.billing_dataset.gcp_billing_export_v1_XXXXXX")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "")          # 留空＝不篩專案
SERVICE_DESC = os.environ.get("GCP_SERVICE_DESC", "Generative Language API")
API_BASE    = os.environ.get("POLICEAI_API_BASE", "https://api.policeaitool.com")
ADMIN_TOKEN = os.environ.get("POLICEAI_ADMIN_TOKEN", "")
USD_TWD     = float(os.environ.get("USD_TWD", "32.5"))
TG_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT     = os.environ.get("TELEGRAM_CHAT_ID", "")


def bq_cost_usd(date_str):
    """用 bq CLI 查該日 Generative Language API 的 Google 實際花費（USD）。"""
    proj_filter = f'AND project.id = "{GCP_PROJECT}"' if GCP_PROJECT else ""
    sql = (
        "SELECT COALESCE(SUM(cost),0) AS cost, "
        "       COALESCE(SUM(SUM(cost)) OVER(),0) AS _x, "
        "       MAX(export_time) AS latest "
        f"FROM `{BQ_TABLE}` "
        f'WHERE service.description = "{SERVICE_DESC}" '
        f'AND DATE(usage_start_time, "Asia/Taipei") = "{date_str}" {proj_filter}'
    )
    # 簡化：分兩段避免 window 與 group 混用問題
    sql = (
        "SELECT COALESCE(SUM(cost),0) AS cost, MAX(export_time) AS latest "
        f"FROM `{BQ_TABLE}` "
        f'WHERE service.description = "{SERVICE_DESC}" '
        f'AND DATE(usage_start_time, "Asia/Taipei") = "{date_str}" {proj_filter}'
    )
    cmd = ["bq", "query", "--nouse_legacy_sql", "--format=json", sql]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=120)
    except FileNotFoundError:
        raise RuntimeError("找不到 bq 指令。請先安裝 gcloud CLI 並執行 gcloud auth login。")
    except subprocess.CalledProcessError as e:
        raise RuntimeError("bq 查詢失敗：" + e.output.decode("utf-8", "ignore")[:400])
    rows = json.loads(out.decode("utf-8"))
    if not rows:
        return 0.0, None
    return float(rows[0].get("cost") or 0), rows[0].get("latest")


def proxy_estimate_ntd(date_str):
    """從 /admin/stats 取 proxy 估算。注意：端點只給『今日』；對帳昨天時，
       這個值僅在你當天有保存快照時才準。實務上建議當天收盤時各記一次。"""
    if not ADMIN_TOKEN:
        return None
    try:
        r = requests.get(API_BASE.rstrip("/") + "/admin/stats",
                         params={"token": ADMIN_TOKEN}, timeout=15)
        r.raise_for_status()
        j = r.json()
        if j.get("date") == date_str:
            return float(j["totals"]["cost_ntd"])
    except Exception:
        pass
    return None


def tg(text):
    if not (TG_TOKEN and TG_CHAT):
        print(text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text,
                            "disable_web_page_preview": True}, timeout=15)
    except Exception as e:
        print(f"⚠️ Telegram 失敗：{e}\n{text}")


def main():
    ap = argparse.ArgumentParser(description="Google 帳單每日對帳（軌 B）")
    ap.add_argument("--date", default=datetime.datetime.now().strftime("%Y-%m-%d"),
                    help="對帳日期 YYYY-MM-DD（推薦對昨天，資料較齊）")
    args = ap.parse_args()

    if "XXXXXX" in BQ_TABLE:
        print("✗ 尚未設定 BQ_TABLE（請填billing 匯出表，或用環境變數）。", file=sys.stderr)
        sys.exit(1)

    google_usd, latest = bq_cost_usd(args.date)
    google_ntd = google_usd * USD_TWD
    est_ntd = proxy_estimate_ntd(args.date)

    lines = [f"📒 Google 帳單對帳｜{args.date}",
             f"Google 實際：US${google_usd:.4f}（≈NT${google_ntd:.2f}）",
             f"帳單資料更新到：{latest or '（無資料，可能尚未匯出）'}"]
    if est_ntd is not None:
        diff = est_ntd - google_ntd
        pct = (diff / google_ntd * 100) if google_ntd else 0.0
        lines.append(f"我方估算：NT${est_ntd:.2f}　差異：{diff:+.2f}（{pct:+.1f}%）")
        lines.append("差異>±10% 值得檢查 _PRICING 或 thinking token 歸類。"
                     if abs(pct) > 10 else "估算與帳單吻合。")
    else:
        lines.append("（未取得同日 proxy 估算；對帳非今日時屬正常，改看每日收盤快照。）")
    tg("\n".join(lines))


if __name__ == "__main__":
    main()
