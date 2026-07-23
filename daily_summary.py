#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 智慧文書系統（POLICE AI）每日花費總結推播 — 讀 /admin/stats，推一則當日總結到 Telegram
# 由 systemd timer 每天固定時間觸發（見 policeai-summary.timer）。
# 設定沿用同目錄 monitor_config.json（api_base / admin_token / telegram_*）。
import os, sys, json, datetime
import requests

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_config.json")


def load_config(path=CFG):
    c = {"api_base": "http://127.0.0.1:5000", "admin_token": "",
         "telegram_bot_token": "", "telegram_chat_id": "", "full_machine_id": False}
    try:
        with open(path, encoding="utf-8") as f:
            c.update({k: v for k, v in json.load(f).items() if k in c})
    except Exception as e:
        print(f"⚠️ 讀設定檔失敗：{e}", file=sys.stderr)
    e = os.environ
    c["api_base"] = e.get("POLICEAI_API_BASE", c["api_base"])
    c["admin_token"] = e.get("POLICEAI_ADMIN_TOKEN", c["admin_token"])
    c["telegram_bot_token"] = e.get("TELEGRAM_BOT_TOKEN", c["telegram_bot_token"])
    c["telegram_chat_id"] = e.get("TELEGRAM_CHAT_ID", c["telegram_chat_id"])
    return c


def fetch_stats(c, date=None, timeout=20):
    p = {"token": c["admin_token"]}
    if date:
        p["date"] = date
    if c.get("full_machine_id"):
        p["full"] = "1"
    r = requests.get(c["api_base"].rstrip("/") + "/admin/stats", params=p,
                     headers={"X-Admin-Token": c["admin_token"]}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def send_telegram(c, text):
    tok, chat = c.get("telegram_bot_token"), c.get("telegram_chat_id")
    if not tok or not chat:
        print("⚠️ 未設定 Telegram，改印出：\n" + text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text,
                            "disable_web_page_preview": True}, timeout=20)
    except Exception as e:
        print(f"⚠️ Telegram 失敗：{e}\n{text}")


def build(s):
    t = s.get("totals", {}) or {}
    cost = float(t.get("cost_ntd") or 0)
    usd = t.get("cost_usd")
    lines = [f"📊 智慧文書系統｜昨日花費總結（{s.get('date', '')}）",
             f"全站合計：NT${cost:.2f}" + (f"（≈US${usd:.2f}）" if usd else ""),
             f"呼叫 {int(t.get('calls') or 0)} 次｜tokens {int(t.get('tokens') or 0):,}"
             f"｜在線 {int(t.get('active_machines') or 0)} 台"]
    unk = int(t.get("unknown_model_calls") or 0)
    if unk:
        lines.append(f"⚠️ 未列價模型 {unk} 筆（_PRICING 可能漏更新，建議檢查）")

    bm = s.get("by_machine") or []
    if bm:
        lines.append("\n花費前幾名：")
        for m in bm[:5]:
            lines.append(f"　{m['machine']}　NT${float(m['cost_ntd']):.2f}"
                         f"（{m['count']} 次）")

    by_model = s.get("by_model") or {}
    if by_model:
        top = sorted(by_model.items(), key=lambda kv: -kv[1]["cost_ntd"])[:4]
        lines.append("\n模型分布：")
        for name, d in top:
            lines.append(f"　{name}　{d['calls']} 次　NT${d['cost_ntd']:.2f}")

    if cost == 0:
        lines.append("\n（該日無付費 AI 用量）")
    lines.append(f"\n台灣時間每日 00:00 重置。")
    return "\n".join(lines)


def main():
    c = load_config()
    if not c["admin_token"]:
        print("✗ 未設定 admin_token。", file=sys.stderr)
        sys.exit(1)
    # 每天早上推「昨天一整天」的總結（07:00 推當天只會是 0，故查昨日）
    tz = datetime.timezone(datetime.timedelta(hours=8))
    yday = (datetime.datetime.now(tz) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        s = fetch_stats(c, date=yday)
    except Exception as e:
        send_telegram(c, f"⚠️ 智慧文書系統每日總結：取用量失敗（{e}）")
        sys.exit(1)
    send_telegram(c, build(s))
    print("✓ 每日總結已送出：" + datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))


if __name__ == "__main__":
    main()
