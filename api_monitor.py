#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 智慧文書系統（POLICE AI）API 即時花費監控 — 介接 /admin/stats，即時顯示 + Telegram 推播
import os, sys, json, time, threading, argparse, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests

DEF = {"api_base":"http://127.0.0.1:5000","admin_token":"","poll_seconds":30,
       "web_host":"127.0.0.1","web_port":8787,"full_machine_id":False,
       "telegram_bot_token":"","telegram_chat_id":"",
       "alerts":{"daily_levels_ntd":[20,50,100,200,500,1000],
                 "machine_spike_ntd":30,"hourly_calls_alert":200,"alert_unknown_model":True}}
STATE="monitor_state.json"; SNAP={"ok":False,"error":"尚未取得資料"}; LOCK=threading.Lock()
SPARK="▁▂▃▄▅▆▇█"

def _du(b,e):
    for k,v in (e or {}).items():
        b[k]=_du(b[k],v) if isinstance(v,dict) and isinstance(b.get(k),dict) else v
    return b

def load_config(path):
    c=json.loads(json.dumps(DEF))
    if path and os.path.exists(path):
        try: _du(c,json.load(open(path,encoding="utf-8")))
        except Exception as e: print(f"⚠️ 讀設定檔失敗：{e}",file=sys.stderr)
    e=os.environ
    c["api_base"]=e.get("POLICEAI_API_BASE",c["api_base"])
    c["admin_token"]=e.get("POLICEAI_ADMIN_TOKEN",c["admin_token"])
    c["telegram_bot_token"]=e.get("TELEGRAM_BOT_TOKEN",c["telegram_bot_token"])
    c["telegram_chat_id"]=e.get("TELEGRAM_CHAT_ID",c["telegram_chat_id"])
    if e.get("POLICEAI_POLL_SECONDS"):
        try: c["poll_seconds"]=int(e["POLICEAI_POLL_SECONDS"])
        except ValueError: pass
    return c

def fetch_stats(c,timeout=15):
    p={"token":c["admin_token"]}
    if c.get("full_machine_id"): p["full"]="1"
    r=requests.get(c["api_base"].rstrip("/")+"/admin/stats",params=p,
                   headers={"X-Admin-Token":c["admin_token"]},timeout=timeout)
    r.raise_for_status(); return r.json()

def send_telegram(c,text):
    t=c.get("telegram_bot_token"); ch=c.get("telegram_chat_id")
    if not t or not ch: return False,"未設定 Telegram"
    try:
        r=requests.post(f"https://api.telegram.org/bot{t}/sendMessage",
                        json={"chat_id":ch,"text":text,"disable_web_page_preview":True},timeout=15)
        b={}
        try: b=r.json()
        except Exception: pass
        return (True,"") if (r.ok and b.get("ok")) else (False,(r.text or "")[:200])
    except Exception as ex: return False,str(ex)

def _ls():
    try: return json.load(open(STATE,encoding="utf-8"))
    except Exception: return {}
def _ss(s):
    try: json.dump(s,open(STATE,"w",encoding="utf-8"),ensure_ascii=False)
    except Exception as e: print(f"⚠️ 存 state 失敗：{e}",file=sys.stderr)
def _fresh(d): return {"date":d,"fired_levels":[],"machines":[],"hours":[],"unknown":False}

def check_alerts(c,s):
    a=c.get("alerts",{}); date=s.get("date") or datetime.datetime.now().strftime("%Y-%m-%d")
    st=_ls()
    if st.get("date")!=date: st=_fresh(date)
    msgs=[]; t=s.get("totals",{}) or {}
    cost=float(t.get("cost_ntd") or 0); usd=t.get("cost_usd")
    calls=int(t.get("calls") or 0); mac=int(t.get("active_machines") or 0)
    bm=s.get("by_machine") or []
    for lv in sorted(a.get("daily_levels_ntd",[]) or []):
        if cost>=lv and lv not in st["fired_levels"]:
            st["fired_levels"].append(lv)
            top=f"\n最高：{bm[0]['machine']} NT${float(bm[0]['cost_ntd']):.1f}（{bm[0]['count']} 次）" if bm else ""
            us=f"（約 US${usd:.2f}）" if usd else ""
            msgs.append(f"🔔 智慧文書系統｜今日全站花費突破 NT${lv}\n目前 NT${cost:.1f}{us}"
                        f"｜呼叫 {calls} 次｜在線 {mac} 台{top}\n重置：{s.get('resets_at','明日 00:00')}")
    sp=float(a.get("machine_spike_ntd") or 0)
    if sp:
        for m in bm:
            if float(m["cost_ntd"])>=sp and m["machine"] not in st["machines"]:
                st["machines"].append(m["machine"])
                msgs.append(f"⚠️ 單機花費偏高：{m['machine']} 今日 NT${float(m['cost_ntd']):.1f}"
                            f"（超過警戒 NT${sp:.0f}）｜{m['count']} 次 / {m['tokens']:,} tokens")
    unk=int(t.get("unknown_model_calls") or 0)
    if a.get("alert_unknown_model") and unk>0 and not st["unknown"]:
        st["unknown"]=True
        msgs.append(f"❓ 今日出現未列價模型 {unk} 筆（unknown_model）。被以 Pro 價高估，"
                    f"且代表伺服器 _PRICING 可能漏更新——請檢查價目表。")
    hl=int(a.get("hourly_calls_alert") or 0)
    if hl:
        h=(s.get("server_time","") or "")[11:13]
        if h:
            hc=int(((s.get("by_hour") or {}).get(h) or {}).get("calls",0)); tag=f"{date}T{h}"
            if hc>=hl and tag not in st["hours"]:
                st["hours"].append(tag); msgs.append(f"⏱ 本小時（{h} 時）全站呼叫 {hc} 次，超過警戒 {hl} 次。")
    _ss(st); return msgs

def poll_loop(c,push=True,stop=None):
    iv=max(5,int(c.get("poll_seconds",30)))
    while not (stop and stop.is_set()):
        try:
            d=fetch_stats(c); d["ok"]=True; d["fetched_at"]=datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            with LOCK: SNAP.clear(); SNAP.update(d)
            if push:
                for m in check_alerts(c,d):
                    ok,err=send_telegram(c,m)
                    if not ok: print(f"⚠️ Telegram 推播失敗：{err}",file=sys.stderr)
        except Exception as e:
            with LOCK: SNAP.clear(); SNAP.update({"ok":False,"error":str(e),"fetched_at":datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")})
            print(f"⚠️ 取用量失敗：{e}",file=sys.stderr)
        for _ in range(iv):
            if stop and stop.is_set(): break
            time.sleep(1)

HTML=r"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>智慧文書系統 · API 即時花費</title><style>
:root{--navy:#1F3864;--gold:#D4AF37;--bg:#0d1526;--card:#141f38;--line:#26355c;--mut:#93a4c8;--ok:#3ecf8e;--bad:#ff5a5a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#e8eefc;font-family:"標楷體",-apple-system,"Segoe UI","Noto Sans TC",sans-serif}
header{background:var(--navy);padding:14px 20px;border-bottom:2px solid var(--gold);display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}
header h1{font-size:17px;margin:0}header h1 b{color:var(--gold)}.meta{font-size:12px;color:var(--mut)}
.wrap{padding:16px 20px;max-width:1100px;margin:0 auto}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.big{font-size:42px;font-weight:800;color:var(--gold);line-height:1}.sub{color:var(--mut);margin-top:6px;font-size:13px}
.kpis{display:flex;gap:24px;flex-wrap:wrap;margin-top:12px}.kpi b{display:block;font-size:20px}.kpi span{color:var(--mut);font-size:12px}
.bar{height:10px;border-radius:6px;background:#0b1730;border:1px solid var(--line);overflow:hidden;margin-top:12px}.bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--ok),var(--gold))}
.lvl{font-size:12px;color:var(--mut);margin-top:6px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:820px){.grid{grid-template-columns:1fr}}
h2{font-size:14px;margin:0 0 10px;color:var(--gold)}table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid #1c2b4d}th:first-child,td:first-child{text-align:left}th{color:var(--mut)}
.mono{font-family:ui-monospace,Consolas,monospace}.hours{display:flex;align-items:flex-end;gap:3px;height:70px;margin-top:6px}
.hours>div{flex:1;background:var(--navy);border-radius:3px 3px 0 0;min-height:2px}.hours>div.now{background:var(--gold)}
.badge{padding:2px 8px;border-radius:999px;font-size:12px;font-weight:700}.badge.bad{background:rgba(255,90,90,.16);color:var(--bad)}.badge.ok{background:rgba(62,207,142,.14);color:var(--ok)}
.off{color:var(--bad)}.dim{color:var(--mut)}footer{color:var(--mut);font-size:11px;text-align:center;padding:12px}</style></head><body>
<header><h1>智慧文書系統 <b>POLICE AI</b> · API 即時花費</h1><div class="meta" id="meta">連線中…</div></header>
<div class="wrap"><div class="card"><div class="big" id="cost">—</div><div class="sub" id="costsub"></div>
<div class="bar"><i id="bf" style="width:0%"></i></div><div class="lvl" id="lvl"></div>
<div class="kpis"><div class="kpi"><b id="calls">—</b><span>今日呼叫</span></div><div class="kpi"><b id="tok">—</b><span>tokens</span></div>
<div class="kpi"><b id="mac">—</b><span>在線機器</span></div><div class="kpi"><b id="unk">—</b><span>未列價筆數</span></div>
<div class="kpi"><b id="rst">—</b><span>重置</span></div></div></div>
<div class="grid"><div class="card"><h2>逐機（花費高→低）</h2><table><thead><tr><th>機器</th><th>次數</th><th>tokens</th><th>NT$</th></tr></thead><tbody id="mtb"></tbody></table></div>
<div class="card"><h2>逐模型</h2><table><thead><tr><th>模型</th><th>次數</th><th>in/out</th><th>NT$</th></tr></thead><tbody id="ptb"></tbody></table></div></div>
<div class="card"><h2>逐時花費（00→23，金色＝現在）</h2><div class="hours" id="hrs"></div></div></div><footer id="ft">—</footer>
<script>
const $=i=>document.getElementById(i),fm=(n,d=2)=>Number(n||0).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d}),it=n=>Number(n||0).toLocaleString('en-US');
let L=[20,50,100,200,500,1000];
async function tick(){try{const r=await fetch('/data.json',{cache:'no-store'}),s=await r.json();
if(!s.ok){$('meta').innerHTML='<span class="off">✗ '+(s.error||'無資料')+'</span>';return;}const t=s.totals||{};
$('meta').textContent='更新 '+(s.fetched_at||'')+' · 伺服器 '+(s.server_time||'')+' · USD/TWD '+(s.usd_twd||'');
$('cost').textContent='NT$ '+fm(t.cost_ntd);$('costsub').textContent='≈ US$ '+fm(t.cost_usd)+'　顧問池 NT$'+fm(t.advisor_ntd)+'　'+(s.gcp_note||'');
$('calls').textContent=it(t.calls);$('tok').textContent=it(t.tokens);$('mac').textContent=it(t.active_machines);
const u=Number(t.unknown_model_calls||0);$('unk').innerHTML=u>0?'<span class="badge bad">'+u+'</span>':'<span class="badge ok">0</span>';
$('rst').textContent=(s.resets_at||'').slice(11,16)||'—';const c=Number(t.cost_ntd||0);let hi=L[L.length-1]||1;for(const v of L){if(c<v){hi=v;break;}}
const p=Math.max(0,Math.min(100,(hi?c/hi:0)*100));$('bf').style.width=p.toFixed(1)+'%';$('lvl').textContent='距下一門檻 NT$'+hi+'：'+p.toFixed(0)+'%';
const mt=$('mtb');mt.innerHTML='';(s.by_machine||[]).slice(0,15).forEach(m=>mt.insertAdjacentHTML('beforeend',`<tr><td class="mono">${m.machine}</td><td>${it(m.count)}</td><td>${it(m.tokens)}</td><td>${fm(m.cost_ntd)}</td></tr>`));
if(!(s.by_machine||[]).length)mt.innerHTML='<tr><td class="dim" colspan="4">今日尚無用量</td></tr>';
const pt=$('ptb');pt.innerHTML='';Object.entries(s.by_model||{}).sort((a,b)=>b[1].cost_ntd-a[1].cost_ntd).forEach(([k,v])=>pt.insertAdjacentHTML('beforeend',`<tr><td class="mono">${k}</td><td>${it(v.calls)}</td><td>${it(v.in_tok)}/${it(v.out_tok)}</td><td>${fm(v.cost_ntd)}</td></tr>`));
if(!Object.keys(s.by_model||{}).length)pt.innerHTML='<tr><td class="dim" colspan="4">今日尚無用量</td></tr>';
const bh=s.by_hour||{};let mx=0;for(let h=0;h<24;h++)mx=Math.max(mx,(bh[String(h).padStart(2,'0')]||{}).cost_ntd||0);mx=mx||1;
const nh=(s.server_time||'').slice(11,13),hd=$('hrs');hd.innerHTML='';for(let h=0;h<24;h++){const hh=String(h).padStart(2,'0'),v=(bh[hh]||{}).cost_ntd||0;hd.insertAdjacentHTML('beforeend',`<div class="${hh===nh?'now':''}" style="height:${Math.max(2,v/mx*70)}px" title="${hh}時 NT$${fm(v)}"></div>`);}
$('ft').textContent='方案每日上限 NT$：'+JSON.stringify(s.plan_quota_ntd||{})+' · 金鑰 '+JSON.stringify(s.keys_loaded||{});
}catch(e){$('meta').innerHTML='<span class="off">✗ 無法連上監控程式：'+e+'</span>';}}
tick();setInterval(tick,5000);</script></body></html>"""

def make_handler():
    class H(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def _s(self,code,ct,b):
            self.send_response(code); self.send_header("Content-Type",ct)
            self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if self.path.startswith("/data.json"):
                with LOCK: b=json.dumps(SNAP,ensure_ascii=False).encode("utf-8")
                self._s(200,"application/json; charset=utf-8",b)
            elif self.path in ("/","/index.html"): self._s(200,"text/html; charset=utf-8",HTML.encode("utf-8"))
            else: self._s(404,"text/plain",b"not found")
    return H

def cmd_web(c):
    threading.Thread(target=poll_loop,args=(c,True),daemon=True).start()
    on=bool(c["telegram_bot_token"] and c["telegram_chat_id"])
    srv=ThreadingHTTPServer((c["web_host"],int(c["web_port"])),make_handler())
    print(f"▶ 儀表板：http://{c['web_host']}:{c['web_port']}　每 {c['poll_seconds']}s 更新　Telegram：{'開' if on else '關（未設）'}　Ctrl-C 結束")
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\n已停止")

def cmd_tui(c,push=False):
    from rich.live import Live; from rich.table import Table; from rich.panel import Panel
    from rich.console import Group; from rich.text import Text; from rich import box
    def spk(v):
        v=[float(x or 0) for x in v]; hi=max(v) or 1.0
        return "".join(SPARK[min(7,int(x/hi*7))] for x in v)
    def render():
        with LOCK: s=json.loads(json.dumps(SNAP))
        if not s.get("ok"): return Panel(Text(f"⏳ {s.get('error','連線中…')}",style="yellow"),title="智慧文書系統 API 監控",border_style="red")
        t=s.get("totals",{}); head=Text()
        head.append(f"今日全站花費  NT$ {float(t.get('cost_ntd') or 0):,.2f}",style="bold gold3")
        if t.get("cost_usd") is not None: head.append(f"   (≈US${float(t['cost_usd']):.2f})",style="dim")
        head.append(f"\n呼叫 {int(t.get('calls') or 0)} 次    tokens {int(t.get('tokens') or 0):,}    在線 {int(t.get('active_machines') or 0)} 台    ")
        u=int(t.get("unknown_model_calls") or 0)
        if u: head.append(f"未列價 {u} 筆",style="bold red")
        head.append(f"\n伺服器 {s.get('server_time','')}   重置 {s.get('resets_at','')}   更新 {s.get('fetched_at','')}",style="dim")
        mt=Table(title="逐機（今日花費高→低）",box=box.SIMPLE,expand=True)
        for col in ("機器","次數","tokens","NT$"): mt.add_column(col,justify="right" if col!="機器" else "left")
        for m in (s.get("by_machine") or [])[:12]: mt.add_row(str(m["machine"]),str(m["count"]),f"{int(m['tokens']):,}",f"{float(m['cost_ntd']):.2f}")
        if not (s.get("by_machine") or []): mt.add_row("（今日尚無用量）","","","")
        pt=Table(title="逐模型",box=box.SIMPLE,expand=True)
        for col in ("模型","次數","in/out tok","NT$"): pt.add_column(col,justify="right" if col!="模型" else "left")
        for n,d in sorted((s.get("by_model") or {}).items(),key=lambda kv:-kv[1]["cost_ntd"]):
            pt.add_row(n,str(d["calls"]),f"{int(d['in_tok']):,}/{int(d['out_tok']):,}",f"{d['cost_ntd']:.2f}")
        bh=s.get("by_hour") or {}
        hp=Panel(Text(spk([(bh.get(f'{h:02d}') or {}).get('cost_ntd',0) for h in range(24)])+"\n00                        12                        23",style="gold3"),title="逐時花費（00→23）",border_style="blue")
        return Panel(Group(head,mt,pt,hp),title="智慧文書系統（POLICE AI）· API 即時花費"+("　[推播:開]" if push else ""),border_style="blue")
    stop=threading.Event(); threading.Thread(target=poll_loop,args=(c,push,stop),daemon=True).start()
    try:
        with Live(render(),refresh_per_second=2,screen=False) as live:
            while True: time.sleep(1); live.update(render())
    except KeyboardInterrupt: stop.set(); print("\n已停止")

def cmd_once(c):
    try: s=fetch_stats(c)
    except Exception as e: print(f"✗ 取用量失敗：{e}"); sys.exit(1)
    t=s.get("totals",{})
    print(f"[{s.get('server_time','')}] 今日全站花費 NT${float(t.get('cost_ntd') or 0):,.2f}"
          f"（≈US${float(t.get('cost_usd') or 0):.2f}）｜呼叫 {int(t.get('calls') or 0)} 次"
          f"｜在線 {int(t.get('active_machines') or 0)} 台"
          + (f"｜未列價 {int(t['unknown_model_calls'])} 筆" if t.get("unknown_model_calls") else ""))
    for m in (s.get("by_machine") or [])[:10]:
        print(f"   {str(m['machine']):<12} {int(m['count']):>4} 次  {int(m['tokens']):>9,} tok  NT${float(m['cost_ntd']):>8.2f}")

def cmd_test(c):
    ok,err=send_telegram(c,"✅ 智慧文書系統 API 監控：Telegram 推播測試成功。")
    print("✓ 已送出（去 Telegram 看看）" if ok else f"✗ 失敗：{err}")

def main():
    ap=argparse.ArgumentParser(description="智慧文書系統 API 即時花費監控")
    ap.add_argument("cmd",choices=["web","tui","once","test-telegram"])
    ap.add_argument("--config",default="monitor_config.json")
    ap.add_argument("--push",action="store_true")
    a=ap.parse_args(); c=load_config(a.config)
    if not c["admin_token"]:
        print("✗ 未設定 admin_token（monitor_config.json 或環境變數 POLICEAI_ADMIN_TOKEN）。",file=sys.stderr); sys.exit(1)
    {"web":lambda:cmd_web(c),"tui":lambda:cmd_tui(c,a.push),"once":lambda:cmd_once(c),"test-telegram":lambda:cmd_test(c)}[a.cmd]()

if __name__=="__main__": main()
