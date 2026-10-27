# -*- coding: utf-8 -*-
"""
台股盤中即時服務（搭配 dashboard.html 的「盤中」分頁）

用法（在 twflow 資料夾內）：
  python live.py            開盤前啟動，抓證交所「基本市況報導」即時報價，並在 http://localhost:8765 提供儀表板
  python live.py --test     不管現在幾點都抓一輪後結束（測試用）

資料來源：臺灣證券交易所基本市況報導網站 mis.twse.com.tw（約 5 秒更新，非逐筆成交）。
只用 Python 標準函式庫。
"""
import argparse
import array
import collections
import math
import datetime as dt
import http.cookiejar
import http.server
import json
import os
import random
import socket
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request

import fetch as F  # 共用設定與路徑

LIVE_DEFAULTS = {
    "live_port": 8765,
    "live_lan": False,            # True：同一個 Wi-Fi 的手機也能連（Windows 可能跳出防火牆詢問）
    "live_start": "08:45",
    "live_end": "13:40",
    "live_focus_sec": 10,         # 重點股更新間隔
    "live_full_sec": 60,          # 全市場掃一輪的時間
    "live_focus_top": 150,        # 重點股：近 20 日平均成交金額前 N 名（另加自選股與畫面送來的候選股）
    "live_batch": 120,            # 每個請求查幾檔（網站上限約 150）
    "live_min_gap_sec": 1.5,      # 兩個請求之間至少間隔幾秒（不要調太小，以免被暫時封鎖）
    "live_provider": "twse_mis",  # 之後可改成券商／Fugle（預留）
    "live_etf": True,             # 盤中也更新 ETF 報價（約多 3 個請求／分鐘）
    "live_macro_sec": 60,         # 布蘭特原油、美元兌台幣幾秒更新一次（Yahoo Finance）；0＝不更新
}
# 台股盤中累計成交量的大致分布（開盤與收盤量大），用來估算「預估全日量比」
VOL_PROFILE = [(0, 0.0), (5, 0.10), (15, 0.20), (30, 0.30), (60, 0.42), (90, 0.51), (120, 0.59), (150, 0.66),
               (180, 0.72), (210, 0.79), (240, 0.86), (265, 0.93), (270, 1.0)]

log = None


def prog(minutes):
    if minutes <= 0:
        return 0.0
    for (m0, p0), (m1, p1) in zip(VOL_PROFILE, VOL_PROFILE[1:]):
        if minutes <= m1:
            return p0 + (p1 - p0) * (minutes - m0) / (m1 - m0)
    return 1.0


def num(s):
    try:
        v = float(s)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def data_time(m, now):
    """報價本身的時間（epoch 秒）：優先用 tlong，其次用今天日期＋t（台灣時間），都沒有就用現在。"""
    tl = num(m.get("tlong"))
    if tl and tl > 1e12:
        return tl / 1000
    t = m.get("t")
    try:
        hh, mm, ss = (int(x) for x in t.split(":"))
        tw = dt.datetime(now.year, now.month, now.day, hh, mm, ss)
        return (tw - dt.timedelta(hours=8)).replace(tzinfo=dt.timezone.utc).timestamp()
    except Exception:
        return time.time()


# ----------------------------------------------------------------- 參考資料（昨天以前）
def load_reference(days_n=25):
    """從 data\\days 取最近 25 個交易日，算出每檔的前 20 日最高價（還原到昨天的價位）、5 日均量、20 日均成交金額"""
    files = sorted(f[:-5] for f in os.listdir(F.DAY_DIR) if f.endswith(".json"))[-days_n:]
    days = [F.load_json(os.path.join(F.DAY_DIR, f + ".json")) for f in files]
    days = [d for d in days if d]
    uni = F.load_json(F.UNIVERSE_PATH, {"stocks": {}})
    us = uni.get("stocks", {})
    codes = sorted(set().union(*[set(d["stocks"]) for d in days]))
    ref = {}
    for code in codes:
        rows = []
        for d in days:
            a = d["stocks"].get(code)
            if not a:
                rows.append(None)
                continue
            f = d["fields"]
            g = lambda k: a[f.index(k)] if k in f else None
            rows.append({"c": g("c"), "h": g("h"), "chg": g("chg"), "vol": g("vol") or 0, "val": (g("val") or 0) / 100,
                         "xref": g("xref"), "nref": g("nref")})
        last = next((r for r in reversed(rows) if r and r["c"]), None)
        if not last:
            continue
        # 參考價（前一日收盤經除權息調整）：上市用除權息參考價、上櫃用前一日公布的次日參考價，其餘用 收盤 − 漲跌
        prev = None
        for k, r in enumerate(rows):
            if not r or not r["c"]:
                continue
            pn = prev["nref"] if prev else None
            r["_ref"] = r["xref"] or pn or (r["c"] - r["chg"] if r["chg"] is not None else r["c"])
            r["_prevc"] = prev["c"] if prev else None
            prev = r
        # 由新往舊還原：把舊的價格換算到昨天的價位
        factor, adj_h = 1.0, []
        for r in reversed(rows):
            if not r or not r["c"]:
                continue
            adj_h.append((r["h"] or r["c"]) * factor)
            if r["_prevc"] and r["_ref"]:
                factor *= r["_ref"] / r["_prevc"]
        adj_h = adj_h[::-1]
        h20 = max(adj_h[-20:]) if adj_h else None
        vols = [r["vol"] for r in rows[-5:] if r]
        vals = [r["val"] for r in rows[-20:] if r]
        m = us.get(code, {})
        mk = "otc" if m.get("mkt") == "上櫃" else "tse" if m.get("mkt") == "上市" else None
        if mk is None:
            mk = "tse"
        ref[code] = {"h20": round(h20, 2) if h20 else None, "vol5": sum(vols) / 5 if vols else 0, "val20": sum(vals) / 20 if vals else 0,
                     "ex": mk, "name": m.get("name", code), "c": last["c"]}
    return ref, files[-1] if files else ""


def load_etf_reference(days_n=25):
    """ETF：從 data\\days 的 etf 欄位取最近一天的名稱、市場，與 20 日均成交金額（億）"""
    files = sorted(f[:-5] for f in os.listdir(F.DAY_DIR) if f.endswith(".json"))[-days_n:]
    out, vals = {}, {}
    for f in files:
        d = F.load_json(os.path.join(F.DAY_DIR, f + ".json")) or {}
        ef, et, names = d.get("etf_fields") or [], d.get("etf") or {}, d.get("etf_names") or {}
        if not et:
            continue
        iv, ic = (ef.index("val") if "val" in ef else None), (ef.index("c") if "c" in ef else None)
        for code, a in et.items():
            if iv is not None and a[iv] is not None:
                vals.setdefault(code, []).append(a[iv] / 100)
            nm = names.get(code) or [code, "上市"]
            out[code] = {"ex": "otc" if nm[1] == "上櫃" else "tse", "name": nm[0], "c": a[ic] if ic is not None else None}
    for code, r in out.items():
        v = vals.get(code, [])[-20:]
        r["val20"] = sum(v) / len(v) if v else 0
    return out


# ----------------------------------------------------------------- 國際：布蘭特原油、美元兌台幣（Yahoo Finance，盤中服務開著時每分鐘更新）
class Macro:
    def __init__(self, cfg):
        self.cfg = cfg
        ctx = ssl.create_default_context()
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self.op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        self.data = {}
        self.errors = 0

    def fetch_one(self, sym):
        q = urllib.parse.quote(sym)
        last = None
        for host in ("query1", "query2"):
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{q}?range=1d&interval=5m&includePrePost=false"
            req = urllib.request.Request(url, headers={"User-Agent": F.DEFAULT_CONFIG["user_agent"], "Accept": "application/json", "Referer": "https://finance.yahoo.com/"})
            try:
                with self.op.open(req, timeout=15) as r:
                    j = json.loads(r.read().decode("utf-8", errors="replace"))
                res = ((j.get("chart") or {}).get("result") or [None])[0]
                if not res:
                    raise RuntimeError(((j.get("chart") or {}).get("error") or {}).get("description") or "沒有資料")
                m = res.get("meta") or {}
                p = m.get("regularMarketPrice")
                if p is None:
                    raise RuntimeError("沒有價格")
                off = int(m.get("gmtoffset") or 0)
                ts = res.get("timestamp") or []
                cl = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
                pts = [[t, round(c, 4)] for t, c in zip(ts, cl) if c is not None][-300:]
                t = int(m.get("regularMarketTime") or time.time())
                return {"p": round(p, 4), "prev": m.get("chartPreviousClose") or m.get("previousClose"), "t": t,
                        "d": dt.datetime.fromtimestamp(t + off, dt.timezone.utc).strftime("%Y%m%d"),
                        "h": m.get("regularMarketDayHigh"), "l": m.get("regularMarketDayLow"), "o": pts[0][1] if pts else None,
                        "pts": pts, "got": int(time.time())}
            except Exception as e:
                last = e
        raise RuntimeError(last)

    def run(self, stop):
        sec = float(self.cfg.get("live_macro_sec") or 0)
        if sec <= 0:
            return
        syms = F.DEFAULT_CONFIG.get("macro_symbols") or {"brent": "BZ=F", "usdtwd": "TWD=X"}
        syms = dict(syms, **(self.cfg.get("macro_symbols") or {}))
        while not stop.is_set():
            ok = 0
            for key, sym in syms.items():
                try:
                    self.data[key] = self.fetch_one(sym)
                    ok += 1
                except Exception as e:
                    self.errors += 1
                    if self.errors in (1, 5) or self.errors % 30 == 0:
                        log(f"  ! 油價／匯率即時更新失敗（{sym}：{e}），之後會繼續重試")
            if ok:
                self.errors = 0
            stop.wait(max(20.0, sec))


# ----------------------------------------------------------------- 證交所基本市況報導
class Mis:
    BASE = os.environ.get("TWFLOW_MIS_BASE", "https://mis.twse.com.tw")

    def __init__(self, cfg):
        self.cfg = cfg
        self.cj = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj), urllib.request.HTTPSHandler(context=ctx))
        self.last = 0.0
        self.count = 0
        self.errors = 0
        self.pause_until = 0.0

    def _get(self, url):
        gap = self.cfg["live_min_gap_sec"] + random.uniform(0, 0.4) - (time.time() - self.last)
        if gap > 0:
            time.sleep(gap)
        self.last = time.time()
        self.count += 1
        req = urllib.request.Request(url, headers={"User-Agent": F.DEFAULT_CONFIG["user_agent"], "Referer": self.BASE + "/stock/index",
                                                   "Accept": "application/json, text/javascript, */*", "Accept-Language": "zh-TW,zh;q=0.9"})
        with self.op.open(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")

    def warmup(self):
        try:
            self._get(self.BASE + "/stock/index?lang=zhHant")
        except Exception as e:
            log(f"  ! 無法連線到基本市況報導網站：{e}")

    def quote(self, keys):
        """keys：['tse_2330.tw', ...]，回傳 msgArray"""
        if not keys or time.time() < self.pause_until:
            return []
        url = self.BASE + "/stock/api/getStockInfo.jsp?ex_ch=" + urllib.parse.quote("|".join(keys)) + f"&json=1&delay=0&_={int(time.time() * 1000)}"
        try:
            j = json.loads(self._get(url))
            if j.get("rtcode") != "0000":
                raise RuntimeError(j.get("rtmessage") or j.get("rtcode"))
            self.errors = 0
            return j.get("msgArray") or []
        except Exception as e:
            self.errors += 1
            wait = min(300, 20 * self.errors)
            log(f"  ! 即時報價失敗（{e}），暫停 {wait} 秒")
            self.pause_until = time.time() + wait
            if self.errors % 3 == 0:
                self.warmup()
            return []


# ----------------------------------------------------------------- 盤中狀態與訊號
class Live:
    def __init__(self, cfg, ref, ref_date, eref=None):
        self.cfg, self.ref, self.ref_date = cfg, ref, ref_date
        self.eref = eref or {}  # ETF：code → {ex, name, val20}
        self.est = {}           # ETF 即時狀態
        self.macro = None       # Macro（油價、匯率）
        self.lock = threading.RLock()   # 可重入：同一個執行緒在鎖內再呼叫 snapshot() 也不會卡死
        self.st = {}            # code → 狀態
        self.idx = {}
        self.events = []
        self.fired = set()
        self.focus_extra = set()
        self.updated = None
        self.date = None
        self.cycle_sec = None
        self.last_saved = 0.0
        self.ibar = {}          # 指數代號 → 今日每分鐘指數（271 格）
        self.radar = []         # 買進雷達：盤中條件觸發的訊號（每種條件每檔有冷卻時間）
        self.mbar = {}          # code → 今日 1 分鐘資料：每分鐘 6 格（收、高、低、累計量張、累計成交額億、累計外盤額億），共 271 分鐘
        self.th = {"minVal5": 0.5, "ratio5": 3.0, "estVR": 1.5, "outer5": 0.6, "near": 0.99}
        th = F.load_json(os.path.join(F.DATA_DIR, "live_settings.json"), {})
        self.th.update({k: v for k, v in th.items() if k in self.th})

    def reload_reference_if_newer(self):
        """data\\days 有比目前參考資料更新的交易日（例如早上補抓到前一天的盤後資料）→ 重算前 20 日高、均量等"""
        try:
            files = sorted(f[:-5] for f in os.listdir(F.DAY_DIR) if f.endswith(".json"))
        except OSError:
            return False
        if not files or files[-1] <= (self.ref_date or ""):
            return False
        try:
            ref, ref_date = load_reference()
        except Exception as e:
            log(f"  ! 參考資料更新失敗：{e}")
            return False
        if not ref:
            return False
        try:
            eref = load_etf_reference() if self.cfg.get("live_etf", True) else {}
        except Exception:
            eref = self.eref
        with self.lock:
            old = self.ref_date
            self.ref, self.ref_date = ref, ref_date
            self.eref = eref or self.eref
        log(f"參考資料更新：{old} → {ref_date}（前 20 日高、均量改用最新資料），共 {len(ref)} 檔")
        return True

    def keys_for(self, codes):
        out = []
        for c in codes:
            r = self.ref.get(c) or self.eref.get(c)
            if r:
                out.append(f"{r['ex']}_{c}.tw")
        return out

    def focus_codes(self):
        top = sorted(self.ref, key=lambda c: -self.ref[c]["val20"])[: self.cfg["live_focus_top"]]
        return list(dict.fromkeys(top + [c for c in self.focus_extra if c in self.ref or c in self.eref]))

    def all_codes(self):
        """全部要輪詢的代號：普通股＋ETF（ETF 依 20 日均成交金額由大到小）"""
        etfs = sorted(self.eref, key=lambda c: -(self.eref[c].get("val20") or 0)) if self.cfg.get("live_etf", True) else []
        return list(self.ref) + [c for c in etfs if c not in self.ref]

    def ingest_etf(self, c, m, now):
        """ETF：只記價格、成交量與估算成交額（不跑雷達、不算進板塊與漲跌家數）"""
        s = self.est.setdefault(c, {"val": 0.0})
        v = num(m.get("v"))
        a1 = num((m.get("a") or "").split("_")[0]) if m.get("a") else None
        b1 = num((m.get("b") or "").split("_")[0]) if m.get("b") else None
        zt, pz = num(m.get("z")), num(m.get("pz"))
        mid = (a1 + b1) / 2 if a1 and b1 else (b1 or a1)
        if zt:
            s["zt"] = zt
        px = zt or s.get("zt") or mid or pz or s.get("z")
        if v is not None and px:
            pv = s.get("v")
            if pv is None:
                s["val"] = v * px / 1e5          # 第一次看到：之前的成交用目前價格估
            elif v > pv:
                s["val"] += (v - pv) * px / 1e5
            s["v"] = v
        s.update({"z": px, "y": num(m.get("y")), "o": num(m.get("o")), "h": num(m.get("h")), "l": num(m.get("l")),
                  "u": num(m.get("u")), "w": num(m.get("w")), "t": m.get("t"), "a1": a1, "b1": b1})

    def minutes(self, now):
        return (now.hour * 60 + now.minute + now.second / 60) - 9 * 60

    def ingest(self, msgs):
        """處理一批報價。注意：基本市況報導網站的「z 成交價」常常是 "-"（這一筆快照剛好沒有成交），
        所以價格依序用：本筆成交價 → 上次看到的成交價 → 買一/賣一中間價 → 買一 → 賣一。
        時間用報價本身的時間（tlong / t），網站有時候整批快照會晚 30～90 秒，用資料時間算 5 分鐘才不會失真。"""
        now = F.now_tw()
        with self.lock:
            for m in msgs:
                c = m.get("c")
                if c in ("t00", "o00"):
                    z = num(m.get("z")) or num(m.get("pz"))
                    y = num(m.get("y"))
                    old = self.idx.get(c, {})
                    self.idx[c] = {"z": z or old.get("z"), "y": y, "t": m.get("t"), "d": m.get("d"), "h": num(m.get("h")), "l": num(m.get("l")),
                                   "o": num(m.get("o")) or old.get("o")}
                    self.date = m.get("d") or self.date
                    # 大盤分時：每分鐘最後一個指數值（09:00～13:30，271 格）
                    zz = self.idx[c]["z"]
                    if zz:
                        k = int(self.minutes_of(data_time(m, now)))
                        if 0 <= k <= 270:
                            self.ibar.setdefault(c, [None] * 271)[k] = round(zz, 2)
                    continue
                if c not in self.ref:
                    if c in self.eref:
                        self.ingest_etf(c, m, now)
                    continue
                s = self.st.setdefault(c, {"samples": collections.deque(maxlen=120), "val": 0.0, "outer": 0.0, "inner": 0.0, "dir": 0})
                ts = data_time(m, now)
                v = num(m.get("v"))
                if v is None:
                    v = s.get("v", 0.0)
                a1 = num((m.get("a") or "").split("_")[0]) if m.get("a") else None
                b1 = num((m.get("b") or "").split("_")[0]) if m.get("b") else None
                zt = num(m.get("z"))                       # 這一筆真的有成交價
                pz = num(m.get("pz"))
                mid = (a1 + b1) / 2 if a1 and b1 else (b1 or a1)
                trial = zt is None and pz is not None and s.get("zt") is None and (m.get("ip") in ("1", "2") or self.minutes_of(ts) <= 0)
                px = zt or s.get("zt") or mid or pz or s.get("z")
                if zt:
                    s["zt"] = zt
                prev_v, prev_px = s.get("v"), s.get("px_est")
                cur_est = zt or mid or px
                if prev_v is not None and v > prev_v and px and ts >= s.get("ts", 0):
                    amt = (v - prev_v) * (cur_est or px) / 1e5     # 張 × 價 → 億
                    s["val"] += amt
                    pa, pb = s.get("a1"), s.get("b1")
                    # 外盤/內盤：有成交價就跟上一筆的買一/賣一比；沒有就看價格往上還是往下（同價延續上一次的方向）
                    if zt and pa and zt >= pa:
                        d = 1
                    elif zt and pb and zt <= pb:
                        d = -1
                    elif prev_px and cur_est and cur_est > prev_px + 1e-9:
                        d = 1
                    elif prev_px and cur_est and cur_est < prev_px - 1e-9:
                        d = -1
                    else:
                        d = s["dir"]
                    s["dir"] = d
                    if d > 0:
                        s["outer"] += amt
                    elif d < 0:
                        s["inner"] += amt
                    else:
                        s["outer"] += amt / 2
                        s["inner"] += amt / 2
                elif prev_v is None:
                    s["val0"] = v * (px or 0) / 1e5             # 第一次看到：之前已成交的量（估）不算進 5 分鐘視窗
                    s["v0"] = v                                 # 均價線只用服務啟動後的成交（開盤前就啟動＝全天均價）
                fv = sum(num(x) or 0 for x in (m.get("f") or "").split("_") if x)    # 五檔委賣量（張）
                gv = sum(num(x) or 0 for x in (m.get("g") or "").split("_") if x)    # 五檔委買量（張）
                if fv or gv:
                    s["askv"], s["bidv"] = fv, gv
                s.update({"z": px, "v": v, "px_est": cur_est or prev_px, "a1": a1 or s.get("a1"), "b1": b1 or s.get("b1"), "y": num(m.get("y")),
                          "o": num(m.get("o")), "h": num(m.get("h")), "l": num(m.get("l")), "u": num(m.get("u")), "w": num(m.get("w")),
                          "t": m.get("t"), "trial": trial, "ts": max(ts, s.get("ts", 0))})
                self.minute_bar(c, s, ts, zt or px, v)
                sm = s["samples"]
                if not sm or ts > sm[-1][0] or v != sm[-1][3]:
                    sm.append((max(ts, sm[-1][0] if sm else 0), s["val"], s["outer"], v))
                self.evaluate(c, s, self.minutes_of(s["ts"]))
                try:
                    self.radar_check(c, s, self.minutes_of(s["ts"]))
                except Exception as e:   # 雷達出錯不能影響報價
                    log(f"  ! 買進雷達 {c} 計算失敗：{e}")
            self.updated = now.strftime("%H:%M:%S")

    MB_W, MB_N = 6, 271     # 09:00～13:30

    def minute_bar(self, c, s, ts, p, v):
        """個股頁的「今日分時」：只記錄服務啟動後看到的報價（基本市況報導沒有提供當天較早的分鐘資料）"""
        if not p or s.get("trial"):
            return
        k = int(self.minutes_of(ts))
        if k < 0 or k >= self.MB_N:
            return
        a = self.mbar.get(c)
        if a is None:
            a = self.mbar[c] = array.array("f", [math.nan]) * (self.MB_N * self.MB_W)
        b = k * self.MB_W
        if math.isnan(a[b]):
            a[b] = a[b + 1] = a[b + 2] = p
        else:
            a[b] = p
            a[b + 1] = max(a[b + 1], p)
            a[b + 2] = min(a[b + 2], p)
        a[b + 3], a[b + 4], a[b + 5] = v or 0, s.get("val", 0.0), s.get("outer", 0.0)

    def stock_detail(self, c):
        with self.lock:
            a = self.mbar.get(c)
            bars = []
            if a is not None:
                W = self.MB_W
                for k in range(self.MB_N):
                    b = k * W
                    if not math.isnan(a[b]):
                        bars.append([k, round(a[b], 2), round(a[b + 1], 2), round(a[b + 2], 2), int(a[b + 3]), round(a[b + 4], 3), round(a[b + 5], 3)])
            s = self.st.get(c) or {}
            r = self.ref.get(c) or {}
            return {"code": c, "date": self.date, "updated": self.updated, "status": status_now(self.cfg), "y": s.get("y"), "h20": r.get("h20"),
                    "val": round(s.get("val", 0.0), 3), "outer": round(s.get("outer", 0.0), 3), "inner": round(s.get("inner", 0.0), 3),
                    "vwap": None if s.get("vwap") is None else round(s["vwap"], 2), "obi": None if s.get("obi") is None else round(s["obi"], 2),
                    "bidv": s.get("bidv"), "askv": s.get("askv"), "out5": None if s.get("out5") is None else round(s["out5"], 2), "est": None if s.get("est") is None else round(s["est"], 2),
                    "radar": [e for e in self.radar if e["code"] == c], "radarNames": self.RADAR_NAMES,
                    "fields": ["m", "c", "h", "l", "vCum", "valCum", "outerCum"], "bars": bars}

    RADAR_NAMES = {"brk15": "爆量突破", "h20": "突破前20日高", "surge": "急拉", "vwap": "站上均價", "obi": "委買強",
                   "lu": "觸及漲停", "lur": "漲停再鎖", "luo": "漲停打開", "vol": "爆量"}

    def radar_check(self, c, s, mins):
        """買進雷達：盤中條件觸發就記一筆（同一檔同一種條件有冷卻時間，避免洗版）。
        分數只算盤中的部分（-10～60）；法人、動能、板塊、均線等加分在網頁上用盤後資料算。"""
        r = self.ref[c]
        z, y = s.get("z"), s.get("y")
        if s.get("trial") or not z or not y or mins < 1 or mins > 271 or (r.get("val20") or 0) < 0.3:
            return
        k = min(270, int(mins))
        chg = (z / y - 1) * 100
        rd = s.setdefault("rd", {})

        def fire(typ, txt, score, gap=None, lv=None):
            last = rd.get(typ)
            if last is not None and (gap is None or k - last < gap):
                return
            rd[typ] = k
            if len(self.radar) >= 6000:
                return
            self.radar.append({"id": len(self.radar) + 1, "time": s.get("t") or F.now_tw().strftime("%H:%M:%S"), "m": k, "code": c, "name": r["name"],
                               "type": typ, "z": round(z, 2), "chg": round(chg, 2), "txt": txt, "s": round(score, 1),
                               "lv": None if lv is None else round(lv, 2)})

        # 均價線（服務啟動後的成交額 ÷ 成交量）與五檔委買委賣力道
        vol_since = (s.get("v") or 0) - s.get("v0", s.get("v") or 0)
        vwap = s["val"] * 1e5 / vol_since if vol_since > 0 and s.get("val", 0) > 0 else None
        s["vwap"] = vwap
        bv, av = s.get("bidv"), s.get("askv")
        s["obi"] = (bv - av) / (bv + av) if bv is not None and av is not None and bv + av > 0 else None
        a = self.mbar.get(c)
        W = self.MB_W
        if a is not None:
            # 爆量突破：價格突破前 15 分鐘（不含最近 3 分鐘）的最高價，而且最近 3 分鐘的量是平常同時段的 3 倍以上
            highs = [a[j * W + 1] for j in range(max(0, k - 18), max(0, k - 2)) if not math.isnan(a[j * W])]
            if len(highs) >= 8:
                hi15 = max(highs)
                jb = next((j for j in range(k - 3, -1, -1) if not math.isnan(a[j * W])), None)
                if jb is not None:
                    v3 = (s.get("v") or 0) - a[jb * W + 3]
                    exp3 = (r.get("vol5") or 0) * max(0.002, prog(k + 1) - prog(jb + 1))
                    ratio = v3 / exp3 if exp3 > 0 else 0
                    if z > hi15 * 1.001 and ratio >= 3 and v3 * z / 1e5 >= 0.2 and chg > 0:
                        fire("brk15", f"突破前 15 分鐘高點 {hi15:g}，近 {k - jb} 分鐘量 {v3:,.0f} 張（平常同時段的 {ratio:.1f} 倍）",
                             30 + min(15, ratio * 1.5) + (5 if (s.get("out5") or 0) >= 0.6 else 0), gap=15, lv=hi15)
            # 急拉：5 分鐘內從低點拉上 2.5% 以上
            lows = [a[j * W + 2] for j in range(max(0, k - 5), k + 1) if not math.isnan(a[j * W])]
            if lows:
                lo5 = min(lows)
                if lo5 > 0 and z / lo5 - 1 >= 0.025 and chg > 0:
                    fire("surge", f"5 分鐘內從 {lo5:g} 拉到 {z:g}（+{(z / lo5 - 1) * 100:.1f}%）", 20, gap=15)
        # 突破前 20 日高（大筆資金突破＝量能也夠）
        if s.get("brk"):
            fire("h20", f"站上前 20 日高 {r.get('h20')}" + ("，5 分鐘量能與外盤都夠（大筆資金突破）" if s.get("big") else ""), 40 if s.get("big") else 25, lv=r.get("h20"))
        # 站上均價：先在均價下方，再站回均價之上，而且最近 5 分鐘外盤偏多
        if vwap and k >= 10 and vol_since >= 50:
            if z < vwap * 0.997:
                s["vw_below"] = True
            elif s.get("vw_below") and z > vwap * 1.003 and (s.get("out5") or 0) >= 0.55:
                s["vw_below"] = False
                fire("vwap", f"從均價 {vwap:.2f} 下方站回，最近 5 分鐘外盤 {(s.get('out5') or 0) * 100:.0f}%", 15, gap=30, lv=vwap)
        # 委買強：五檔委買明顯大於委賣
        if s["obi"] is not None and bv + av >= 100 and s["obi"] >= 0.5 and chg >= 0:
            fire("obi", f"五檔委買 {bv:,.0f} 張 vs 委賣 {av:,.0f} 張", 10, gap=30)
        # 漲停：觸及、打開、再鎖（每次狀態改變都記）
        u = s.get("u")
        if u:
            if z >= u - 1e-6 and not s.get("at_lu"):
                s["at_lu"] = True
                if s.get("lu_open"):
                    fire("lur", f"漲停 {u:g} 再鎖", 10, gap=0, lv=u)
                else:
                    fire("lu", f"觸及漲停 {u:g}", 15, gap=0, lv=u)
            elif s.get("at_lu") and z < u - 1e-6:
                s["at_lu"] = False
                s["lu_open"] = True
                fire("luo", f"漲停 {u:g} 打開到 {z:g}（追高要小心）", -10, gap=0)
        # 爆量：預估全日量是 5 日均量的 3 倍以上（每天一次）
        est = s.get("est")
        if est and est >= 3 and k >= 15 and (s.get("v") or 0) >= 500:
            fire("vol", f"預估全日量是 5 日均量的 {est:.1f} 倍（已成交 {s.get('v'):,.0f} 張）", 15)

    def radar_since(self, since=0):
        with self.lock:
            return {"date": self.date, "updated": self.updated, "status": status_now(self.cfg), "names": self.RADAR_NAMES,
                    "next": len(self.radar), "events": self.radar[max(0, since):]}

    def minutes_of(self, ts):
        return self.minutes(dt.datetime.fromtimestamp(ts, dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=8))

    def window(self, s, sec=300):
        sm = s["samples"]
        if len(sm) < 2:
            return 0.0, 0.0
        t1, val1, out1, _ = sm[-1]
        base = next((x for x in sm if t1 - x[0] <= sec), sm[0])
        dval = val1 - base[1]
        dout = out1 - base[2]
        return dval, (dout / dval if dval > 0 else None)

    def evaluate(self, c, s, mins):
        r = self.ref[c]
        val5, out5 = self.window(s)
        # 平常這個時段 5 分鐘的成交金額＝20 日均額 ×（這 5 分鐘在一般日子占全日的比例）；開盤與收盤前比例較高
        share5 = max(0.005, prog(mins) - prog(mins - 5))
        base5 = r["val20"] * share5 if r["val20"] else 0
        p = max(prog(mins), 0.05)
        est = s["v"] / (r["vol5"] * p) if r["vol5"] and mins > 0 else None
        s.update({"val5": val5, "out5": out5, "ratio5": (val5 / base5) if base5 > 0 else None, "est": est})
        th = self.th
        h20 = r["h20"]
        brk = bool(h20 and s["z"] and s["z"] > h20 and not s.get("trial") and mins > 0)
        s["brk"] = brk
        s["near"] = bool(h20 and s["z"] and not brk and s["z"] >= h20 * th["near"])
        big = val5 >= th["minVal5"] and (s["ratio5"] or 0) >= th["ratio5"] and (est or 0) >= th["estVR"] and (out5 or 0) >= th["outer5"]
        s["big"] = big
        key = (c, "brk")
        if brk and big and key not in self.fired:
            self.fired.add(key)
            ev = {"time": s["t"] or F.now_tw().strftime("%H:%M:%S"), "code": c, "name": r["name"], "type": "大筆資金突破", "z": s["z"], "y": s["y"], "h20": h20,
                  "val5": round(val5, 2), "ratio5": round(s["ratio5"] or 0, 1), "est": round(est or 0, 2), "out5": round(out5 or 0, 2)}
            self.events.append(ev)
            log(f"  ★ {ev['time']} {c} {r['name']} 大筆資金突破：{s['z']}（前20日高 {h20}），5 分鐘 {val5:.2f} 億，外盤 {(out5 or 0) * 100:.0f}%")
            self.save_events()

    def lag(self):
        ts = [x["ts"] for x in self.st.values() if x.get("ts")]
        if not ts:
            return None
        return max(0, round(time.time() - max(ts)))

    def save_events(self):
        d = self.date or F.now_tw().strftime("%Y%m%d")
        os.makedirs(os.path.join(F.DATA_DIR, "live"), exist_ok=True)
        F.save_json(os.path.join(F.DATA_DIR, "live", f"{d}_events.json"), self.events)

    # ---------------- 板塊即時資金流（主動買賣＝外盤成交額 − 內盤成交額）
    def sector_map(self):
        """代號 → [板塊]；名稱 → 分類。用 sectors.csv，沒有列進去的股票歸「未分類」"""
        if getattr(self, "_secmap", None) is not None:
            return self._secmap
        rows = F.read_sectors() or []
        by_code, kinds, order = {}, {}, []
        for name, code, kind in rows:
            if name not in kinds:
                kinds[name] = kind
                order.append(name)
            by_code.setdefault(code, [])
            if name not in by_code[code]:
                by_code[code].append(name)
        for c in self.ref:
            if c not in by_code:
                by_code[c] = ["未分類"]
                if "未分類" not in kinds:
                    kinds["未分類"] = "產業"
                    order.append("未分類")
        self._secmap = (by_code, kinds, order)
        return self._secmap

    def sector_flow(self):
        """每個板塊、每分鐘的累計主動買賣淨額與成交額（億）。從盤中服務啟動開始累計；沒報價的分鐘沿用前一分鐘。
        結果快取 15 秒（全市場約 2,000 檔 × 271 分鐘）。"""
        now = time.time()
        cache = getattr(self, "_flow_cache", None)
        if cache and now - cache[0] < 15:
            return cache[1]
        by_code, kinds, order = self.sector_map()
        N, W = self.MB_N, self.MB_W
        idx = {n: k for k, n in enumerate(order)}
        net = [[0.0] * N for _ in order]
        val = [[0.0] * N for _ in order]
        tot_net, tot_val = [0.0] * N, [0.0] * N
        first, last = N, -1
        with self.lock:
            items = list(self.mbar.items())
        for c, a in items:
            secs = [idx[n] for n in by_code.get(c, []) if n in idx]
            cv = co = None
            row_n, row_v = [0.0] * N, [0.0] * N
            seen = False
            for k in range(N):
                b = k * W
                v4 = a[b + 4]
                if not math.isnan(v4):
                    cv, co = v4, a[b + 5]
                    if not seen:
                        seen = True
                        first = min(first, k)
                    last = max(last, k)
                if cv is not None:
                    row_n[k] = 2 * co - cv          # 外盤 − 內盤（內盤＝成交額 − 外盤）
                    row_v[k] = cv
            if not seen:
                continue
            for k in range(N):
                tot_net[k] += row_n[k]; tot_val[k] += row_v[k]
            for j in secs:
                nj, vj = net[j], val[j]
                for k in range(N):
                    nj[k] += row_n[k]; vj[k] += row_v[k]
        rnd = lambda arr: [round(x, 2) if first <= k <= last else None for k, x in enumerate(arr)]
        out = {"date": self.date, "updated": self.updated, "status": status_now(self.cfg), "first": first if last >= 0 else None, "last": last if last >= 0 else None,
               "sectors": order, "types": [kinds[n] for n in order],
               "net": [rnd(x) for x in net], "val": [rnd(x) for x in val], "totNet": rnd(tot_net), "totVal": rnd(tot_val)}
        self._flow_cache = (now, out)
        return out

    def sector_members(self, name, m=None, k=None):
        """板塊成分股在某一分鐘（m，0＝09:00）的主動買賣淨額與成交額（億）；k＝只算最近 k 分鐘（None＝開盤到 m）"""
        by_code, kinds, order = self.sector_map()
        codes = [c for c, ns in by_code.items() if name in ns]
        N, W = self.MB_N, self.MB_W
        rows = []
        with self.lock:
            for c in codes:
                a = self.mbar.get(c)
                s = self.st.get(c) or {}
                r = self.ref.get(c) or {}
                net = val = None
                if a is not None:
                    mm = N - 1 if m is None else max(0, min(N - 1, int(m)))
                    j1 = next((j for j in range(mm, -1, -1) if not math.isnan(a[j * W + 4])), None)
                    if j1 is not None:
                        v1, o1 = a[j1 * W + 4], a[j1 * W + 5]
                        v0 = o0 = 0.0
                        if k:
                            m0 = mm - int(k)
                            j0 = next((j for j in range(m0, -1, -1) if j >= 0 and not math.isnan(a[j * W + 4])), None) if m0 >= 0 else None
                            if j0 is not None:
                                v0, o0 = a[j0 * W + 4], a[j0 * W + 5]
                        val = v1 - v0
                        net = 2 * (o1 - o0) - val
                if net is None and not k and m is None and s.get("val"):
                    val = s.get("val", 0.0)                       # 沒有分鐘資料（例如收盤後才啟動）：用今天累計
                    net = 2 * s.get("outer", 0.0) - val
                z, y = s.get("z"), s.get("y")
                rows.append({"code": c, "name": r.get("name", c), "z": z, "chg": round((z / y - 1) * 100, 2) if z and y else None,
                             "net": None if net is None else round(net, 3), "val": None if val is None else round(val, 3),
                             "dayVal": round(s.get("val", 0.0), 3), "outer": round(s.get("outer", 0.0), 3)})
        return {"name": name, "type": kinds.get(name), "m": m, "k": k, "date": self.date, "updated": self.updated, "status": status_now(self.cfg), "rows": rows}

    def save_flow(self):
        """板塊即時資金流存檔：data/live/flow_last.js（服務關掉後網頁也能回放今天）與 flow_日期.js（留存）"""
        if not self.mbar:
            return
        try:
            out = self.sector_flow()
            out = dict(out, saved=F.now_tw().strftime("%H:%M:%S"))
            # 每檔今天到目前為止的主動買賣淨額與成交額（億）：服務關掉後，網頁點板塊還能看成分股
            with self.lock:
                out["stocks"] = {c: [round(2 * x.get("outer", 0.0) - x.get("val", 0.0), 3), round(x.get("val", 0.0), 3), x.get("z"), x.get("y")] for c, x in self.st.items() if x.get("val")}
            d = self.date or F.now_tw().strftime("%Y%m%d")
            body = "window.TWFLOW_FLOW=" + json.dumps(out, ensure_ascii=False, separators=(",", ":")) + ";\n"
            for name in ("flow_last.js", f"flow_{d}.js"):
                p = os.path.join(F.DATA_DIR, "live", name)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                tmp = f"{p}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(body)
                os.replace(tmp, p)
        except Exception as e:
            log(f"  ! 板塊資金流存檔失敗：{e}")

    def save_last(self):
        """最後一次報價存成 data/live/last.js：盤中服務關掉後，直接開 dashboard.html 也看得到當天的收盤報價
        （晚上 run.bat 抓到官方收盤資料之前）"""
        if not self.st:
            return
        try:
            snap = self.snapshot()
            snap.pop("events", None)
            snap["saved"] = F.now_tw().strftime("%H:%M:%S")
            p = os.path.join(F.DATA_DIR, "live", "last.js")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = f"{p}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("window.TWFLOW_LIVE_LAST=")
                json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
                f.write(";\n")
            os.replace(tmp, p)
            self.last_saved = time.time()
        except Exception as e:
            log(f"  ! 收盤報價存檔失敗：{e}")
        self.save_flow()
        self.save_radar()

    def save_radar(self):
        """買進雷達存檔：data/live/radar_last.js（服務關掉後網頁也看得到今天的訊號）與 radar_日期.json"""
        if not self.radar:
            return
        try:
            out = dict(self.radar_since(0), saved=F.now_tw().strftime("%H:%M:%S"))
            d = self.date or F.now_tw().strftime("%Y%m%d")
            folder = os.path.join(F.DATA_DIR, "live")
            os.makedirs(folder, exist_ok=True)
            body = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
            for name, text in (("radar_last.js", "window.TWFLOW_RADAR=" + body + ";\n"), (f"radar_{d}.json", body)):
                p = os.path.join(folder, name)
                tmp = f"{p}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, p)
        except Exception as e:
            log(f"  ! 買進雷達存檔失敗：{e}")

    def snapshot(self):
        with self.lock:
            stocks = {}
            for c, s in self.st.items():
                stocks[c] = [s.get("z"), s.get("y"), s.get("o"), s.get("h"), s.get("l"), s.get("v"), round(s.get("val", 0), 3),
                             round(s.get("val5", 0), 3), None if s.get("ratio5") is None else round(s["ratio5"], 2),
                             None if s.get("est") is None else round(s["est"], 2), None if s.get("out5") is None else round(s["out5"], 2),
                             self.ref[c]["h20"], (1 if s.get("brk") else 0) | (2 if s.get("big") else 0) | (4 if s.get("near") else 0) | (8 if s.get("trial") else 0),
                             s.get("u"), s.get("w"), s.get("t")]
            return {"provider": self.cfg["live_provider"], "ver": LIVE_VERSION, "date": self.date, "refDate": self.ref_date, "updated": self.updated,
                    "status": status_now(self.cfg), "index": self.idx, "th": self.th, "requests": MIS.count if MIS else 0,
                    "cycleSec": self.cycle_sec, "lagSec": self.lag(),
                    "fields": ["z", "y", "o", "h", "l", "v", "val", "val5", "ratio5", "est", "out5", "h20", "flags", "u", "w", "t"],
                    "stocks": stocks, "events": self.events, "ibars": {k: list(v) for k, v in self.ibar.items()},
                    "etfFields": ["z", "y", "o", "h", "l", "v", "val", "u", "w", "t"],
                    "etfs": {c: [s.get("z"), s.get("y"), s.get("o"), s.get("h"), s.get("l"), s.get("v"), round(s.get("val", 0.0), 3), s.get("u"), s.get("w"), s.get("t")] for c, s in self.est.items()},
                    "macro": dict(self.macro.data) if self.macro else {}}


def status_now(cfg):
    n = F.now_tw().strftime("%H:%M")
    if n < "09:00":
        return "pre"
    if n <= "13:30":
        return "open"
    return "closed"


# ----------------------------------------------------------------- 輪詢
MIS = None
LIVE_VERSION = 8          # 盤中服務版本：新版啟動時若發現舊版（2 以上）在跑，會請它結束後接手（v3：直接開 dashboard.html 也能連、收盤報價存檔；v4：早上補抓到新資料會自動換參考資料；v5：大盤開盤價與分時；v7：買進雷達；v8：ETF、油價匯率即時、板塊成分股）
QUIT = threading.Event()  # 收到「交棒」要求時結束整個服務


def poller(live, cfg, stop, once=False):
    """重點股（成交金額前 N 名＋自選＋動能清單）每 live_focus_sec 秒掃一次；其餘股票分批輪流掃，
    一輪大約 live_full_sec 秒。網站回應慢的時候（開盤前幾分鐘常常 2～6 秒才回）一輪會自動拉長，不會加快請求。"""
    global MIS
    MIS = Mis(cfg)
    MIS.warmup()
    all_codes = live.all_codes()
    B = cfg["live_batch"]
    last_focus, ptr, cycle_t0, cycles = 0.0, 0, time.time(), 0
    last_refchk = time.time()
    while not stop.is_set():
        # 08:40 啟動時，前一天的盤後資料可能還沒補抓（排程 08:41 才補）→ 每 2 分鐘看一次，有新的交易日資料就換參考資料
        if time.time() - last_refchk >= 120:
            last_refchk = time.time()
            if live.reload_reference_if_newer():
                all_codes = live.all_codes()
        focus = live.focus_codes()
        fset = set(focus)
        rest = [c for c in all_codes if c not in fset]
        if time.time() - last_focus >= cfg["live_focus_sec"]:
            last_focus = time.time()
            keys = ["tse_t00.tw", "otc_o00.tw"] + live.keys_for(focus)
            for k in range(0, len(keys), B):
                live.ingest(MIS.quote(keys[k:k + B]))
        elif not rest:
            live.cycle_sec = cfg["live_focus_sec"]
            if once:
                return
            stop.wait(max(0.2, cfg["live_focus_sec"] - (time.time() - last_focus)))
        else:
            # 在兩次重點股之間，盡量平均地把其他股票掃完：每 live_full_sec 秒一輪
            nreq = max(1, -(-len(rest) // B))
            per_focus = max(1, -(-nreq // max(1, int(cfg["live_full_sec"] / cfg["live_focus_sec"]))))
            for _ in range(per_focus):
                if ptr >= len(rest):
                    live.cycle_sec = round(time.time() - cycle_t0)
                    cycle_t0, ptr = time.time(), 0
                    cycles += 1
                    if once:
                        return
                live.ingest(MIS.quote(live.keys_for(rest[ptr:ptr + B])))
                ptr += B
            wait = cfg["live_focus_sec"] - (time.time() - last_focus)
            if wait > 0:
                stop.wait(wait)
        if time.time() - live.last_saved >= 60:
            live.save_last()
        # 收盤後才啟動時，先把全部股票掃完一輪（約 1 分鐘）再結束，這樣存下來的收盤報價是完整的
        if status_now(cfg) == "closed" and F.now_tw().strftime("%H:%M") >= cfg["live_end"] and (cycles >= 1 or not rest):
            log("已過收盤時間，盤中服務結束。")
            live.save_events()
            live.save_last()
            stop.set()
            return


# ----------------------------------------------------------------- 網頁服務
def make_handler(live):
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=F.BASE_DIR, **kw)

        def log_message(self, *a):
            pass

        def end_headers(self):
            # 網頁與資料檔每次都要跟電腦確認是否有新版（程式更新後不用按 Ctrl+F5）；/api 已經自己帶 no-store
            if not self.path.startswith("/api/"):
                self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def _origin_ok(self):
            """允許：同一個網址（http://localhost:8765）、直接雙擊開啟的 dashboard.html（Origin 是 "null" 或 "file://"）、沒有帶 Origin 的本機程式"""
            o = self.headers.get("Origin")
            port = self.server.server_address[1]
            return o is None or o in ("null", "file://") or o in (f"http://localhost:{port}", f"http://127.0.0.1:{port}")

        def _cors(self):
            o = self.headers.get("Origin")
            if o and self._origin_ok():
                self.send_header("Access-Control-Allow-Origin", o)
                self.send_header("Vary", "Origin")

        def _json(self, obj, code=200):
            b = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            p = urllib.parse.urlparse(self.path).path
            if p == "/api/live":
                return self._json(live.snapshot())
            if p == "/api/flow":
                return self._json(live.sector_flow())
            if p == "/api/radar":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                try:
                    since = max(0, int((q.get("since") or ["0"])[0]))
                except ValueError:
                    since = 0
                return self._json(live.radar_since(since))
            if p == "/api/sector":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                name = (q.get("name") or [""])[0]
                try:
                    m = int((q.get("m") or [""])[0])
                except ValueError:
                    m = None
                try:
                    k = int((q.get("k") or ["0"])[0]) or None
                except ValueError:
                    k = None
                return self._json(live.sector_members(name, m, k))
            if p == "/api/macro":
                return self._json(live.macro.data if live.macro else {})
            if p == "/api/stock":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code = (q.get("code") or [""])[0].strip()
                if not (F.CODE_RE.match(code) or F.ETF_RE.match(code)):
                    return self._json({"error": "bad code"}, 400)
                return self._json(live.stock_detail(code))
            if p == "/":
                self.send_response(302)
                self.send_header("Location", page_path())
                self.end_headers()
                return
            if p.startswith("/logs") or p.endswith(".py") or p.endswith(".bat") or "/raw/" in p:
                return self._json({"error": "forbidden"}, 403)
            return super().do_GET()

        def do_OPTIONS(self):
            # 直接開 dashboard.html 時，瀏覽器送 POST 前會先問一次（預檢）
            if not self._origin_ok():
                return self._json({"error": "forbidden"}, 403)
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def do_POST(self):
            p = urllib.parse.urlparse(self.path).path
            if not self._origin_ok():
                return self._json({"error": "forbidden"}, 403)   # 其他網站不能改這台電腦的盤中設定
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            if p == "/api/focus":
                codes = [str(c) for c in (body.get("codes") or [])][:600]
                with live.lock:
                    live.focus_extra = set(codes)
                return self._json({"ok": True, "focus": len(live.focus_codes())})
            if p == "/api/shutdown":
                # 只接受本機、而且帶自訂標頭的要求（一般網頁不能跨站送自訂標頭），用來讓新版接手
                if self.client_address[0] != "127.0.0.1" or self.headers.get("X-TWFlow-Handoff") != "1":
                    return self._json({"error": "forbidden"}, 403)
                log("收到停止要求（live.bat 接手或 stop_live.bat），這個盤中服務結束。")
                live.save_events()
                live.save_last()
                QUIT.set()
                return self._json({"ok": True})
            if p == "/api/settings":
                with live.lock:
                    for k, v in (body or {}).items():
                        if k in live.th and isinstance(v, (int, float)):
                            live.th[k] = float(v)
                F.save_json(os.path.join(F.DATA_DIR, "live_settings.json"), live.th)
                return self._json({"ok": True, "th": live.th})
            return self._json({"error": "not found"}, 404)
    return H


class LiveServer(socketserver.ThreadingTCPServer):
    """Windows 的 SO_REUSEADDR 會讓兩個程式同時佔用同一個連接埠（兩個盤中服務一起跑、請求量加倍），
    所以 Windows 改用獨占模式；其他系統維持可重用，方便重新啟動。"""
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def running_version(port):
    """本機已經有盤中服務在回應 → 回傳它的版本（舊版沒有版本號 → 1）；沒有在跑 → None"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本機連線不要走公司 proxy
    try:
        with op.open(f"http://127.0.0.1:{port}/api/live", timeout=3) as r:
            j = json.loads(r.read().decode("utf-8"))
            return int(j.get("ver") or 1) if "provider" in j else None
    except Exception:
        return None


def already_running(port):
    """有同版或更新的服務在跑 → True（不啟動第二個）。比較舊的服務若支援交棒，就請它結束、由這個新版接手。"""
    v = running_version(port)
    if v is None:
        return False
    if v >= LIVE_VERSION:
        return True
    if v < 2:
        log(f"！電腦上正在跑的是舊版盤中服務（沒有個股分時功能），它不支援自動交棒。"
            f"請在工作管理員結束 pythonw.exe／python.exe 後再開 live.bat；明天排程會自動用新版。")
        return True
    log(f"發現舊版盤中服務（第 {v} 版），請它結束後由新版接手…")
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "X-TWFlow-Handoff": "1"})
        op.open(req, timeout=5).read()
    except Exception as e:
        log(f"  交棒要求失敗：{e}")
        return True
    for _ in range(20):
        time.sleep(0.5)
        if running_version(port) is None:
            time.sleep(1)
            return False
    log("  舊版服務沒有在 10 秒內結束，維持舊版。")
    return True


def page_path(hash_="live"):
    """儀表板網址加上檔案更新時間（?v=…）：程式更新後網址就不同，瀏覽器一定會載入新版，不會用舊的暫存。"""
    try:
        v = int(os.path.getmtime(os.path.join(F.BASE_DIR, "dashboard.html")))
    except OSError:
        v = 0
    return f"/dashboard.html?v={v}#{hash_}"


def request_shutdown(port, wait_sec=10):
    """請正在執行的盤中服務結束（只接受本機＋自訂標頭的要求）。結束了回傳 True。"""
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "X-TWFlow-Handoff": "1"})
        op.open(req, timeout=5).read()
    except Exception as e:
        log(f"  停止要求失敗：{e}")
        return False
    for _ in range(wait_sec * 2):
        time.sleep(0.5)
        if running_version(port) is None:
            time.sleep(1)
            return True
    return False


def take_over(port):
    """前景模式（live.bat）：如果背景已經有盤中服務在跑（例如 08:40 排程），請它結束，改由這個視窗執行。"""
    v = running_version(port)
    if v is None:
        return True
    if v < 2:
        log("！電腦上正在跑的是很舊的盤中服務，不能自動接手：請在工作管理員結束 pythonw.exe／python.exe 後再開 live.bat。")
        return False
    log("發現已經有盤中服務在執行，請它結束，改由這個視窗接手…")
    if request_shutdown(port):
        return True
    log("！原本的盤中服務沒有在 10 秒內結束，請執行 stop_live.bat 或在工作管理員結束 python 後再試。")
    return False


def stop_service(port):
    """stop_live.bat：手動停止盤中服務（不管是在背景還是在別的視窗）"""
    v = running_version(port)
    if v is None:
        log("目前沒有盤中服務在執行，不用停止。")
        return 0
    if v < 2:
        log("！正在跑的是很舊的版本，不支援用這個方式停止：請在工作管理員結束 pythonw.exe／python.exe。")
        return 1
    if request_shutdown(port):
        log("盤中服務已停止。")
        return 0
    log("！盤中服務沒有在 10 秒內停止，請在工作管理員結束 pythonw.exe／python.exe。")
    return 1


def refresh_old_days(days_n=25):
    """舊版 fetch.py 整理出來的 days 檔缺少成交金額等欄位：用本機的原始資料重算（不連網）"""
    try:
        files = sorted(f[:-5] for f in os.listdir(F.DAY_DIR) if f.endswith(".json"))[-days_n:]
    except FileNotFoundError:
        return
    old = [f for f in files if (F.load_json(os.path.join(F.DAY_DIR, f + ".json")) or {}).get("v") != F.PARSER_VERSION]
    if not old:
        return
    log(f"參考資料是舊格式，先用本機原始資料重算 {len(old)} 天（不連網）…")
    uni = F.load_json(F.UNIVERSE_PATH)
    for f in old:
        try:
            F.build_day(dt.datetime.strptime(f, "%Y%m%d").date(), uni)
        except Exception as e:
            log(f"  ! {f} 重算失敗：{e}")


def ensure_history(cfg):
    """動能分頁需要 data\\history.js：沒有或比最新的每日資料舊，就用本機資料重做一份（不連網）"""
    p = os.path.join(F.DATA_DIR, "history.js")
    try:
        newest = max(os.path.getmtime(os.path.join(F.DAY_DIR, f)) for f in os.listdir(F.DAY_DIR) if f.endswith(".json"))
    except (FileNotFoundError, ValueError):
        return
    if os.path.exists(p) and os.path.getmtime(p) >= newest:
        return
    log("動能用的歷史資料（history.js）不存在或較舊，先用本機資料產生…")
    try:
        F.build_history(cfg, F.load_json(F.UNIVERSE_PATH) or {"stocks": {}})
    except Exception as e:
        log(f"  ! history.js 產生失敗：{e}")


def ensure_background(cfg, open_browser=True):
    """live.bat 用：確認盤中服務在背景執行（沒有就在背景啟動、舊版就交棒），然後打開網頁。
    盤中服務不再綁在這個黑色視窗上，關掉視窗也不會停。回傳結束代碼：3＝服務正常、1＝失敗。"""
    import subprocess
    port = cfg["live_port"]
    url = f"http://localhost:{port}{page_path()}"
    if already_running(port):
        log(f"盤中服務已經在背景正常執行（每個交易日 08:40 自動啟動），不用再開；已幫你打開網頁 {url}")
    else:
        exe = sys.executable
        if os.name == "nt":
            pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            exe = pyw if os.path.exists(pyw) else exe
        kw = {"cwd": F.BASE_DIR, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        subprocess.Popen([exe, os.path.abspath(__file__), "--no-browser"], **kw)
        print("盤中服務在背景啟動中…（第一次可能要 10～60 秒）", flush=True)
        t0 = time.time()
        while time.time() - t0 < 120:
            time.sleep(2)
            if running_version(port) is not None:
                break
        else:
            log("！盤中服務沒有在 2 分鐘內啟動，請看 logs 資料夾今天的紀錄。")
            return 1
        log(f"盤中服務已在背景啟動（關掉這個視窗也不會停，收盤後會自動結束）；已幫你打開網頁 {url}")
    print("（這不是錯誤。這個視窗 5 秒後會自動關閉。）", flush=True)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    return 3


def no_quick_edit():
    """Windows 的黑色視窗預設開著「快速編輯」：在視窗裡點一下就會選取文字，程式下次輸出時整個卡住（報價停止更新）。
    這裡把它關掉（只影響這個視窗）。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)   # 關掉 ENABLE_QUICK_EDIT_MODE，保留 ENABLE_EXTENDED_FLAGS
    except Exception:
        pass


def main():
    global log
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--ensure", action="store_true", help="確認背景服務在跑（沒有就背景啟動），再打開網頁")
    ap.add_argument("--foreground", action="store_true", help="在這個視窗執行（live.bat 用）：已經有服務在跑就請它結束、由這個視窗接手")
    ap.add_argument("--stop", action="store_true", help="停止正在執行的盤中服務（stop_live.bat 用）")
    a = ap.parse_args()
    F.log = log = F.Log()
    cfg = F.load_config()
    for k, v in LIVE_DEFAULTS.items():
        cfg.setdefault(k, v)
    if a.ensure:
        sys.exit(ensure_background(cfg, open_browser=not a.no_browser))
    if a.stop:
        sys.exit(stop_service(cfg["live_port"]))
    log("=" * 50)
    no_quick_edit()
    if a.foreground:
        print("這個視窗就是盤中服務：開著才會更新報價；要停止就直接關掉這個視窗（或按 Ctrl+C）。", flush=True)
        if not take_over(cfg["live_port"]):
            sys.exit(1)
    refresh_old_days()
    ensure_history(cfg)
    ref, ref_date = load_reference()
    log(f"盤中服務啟動：參考資料到 {ref_date}，共 {len(ref)} 檔")
    if not ref:
        log("! 沒有參考資料，請先執行 run.bat")
        return
    eref = {}
    if cfg.get("live_etf", True):
        try:
            eref = load_etf_reference()
        except Exception as e:
            log(f"  ! ETF 參考資料讀取失敗（不影響個股）：{e}")
    live = Live(cfg, ref, ref_date, eref)
    if eref:
        log(f"ETF 也會更新報價：{len(eref)} 檔")
    stop = threading.Event()
    live.macro = Macro(cfg)
    macro_stop = threading.Event()
    if a.test:
        poller(live, cfg, stop, once=True)
        snap = live.snapshot()
        log(f"測試完成：{len(snap['stocks'])} 檔有報價，{MIS.count} 個請求，加權 {snap['index'].get('t00')}")
        return
    host = "0.0.0.0" if cfg["live_lan"] else "127.0.0.1"
    running = False
    try:
        running = already_running(cfg["live_port"])
        if running:
            raise OSError("already running")
        srv = LiveServer((host, cfg["live_port"]), make_handler(live))
    except OSError:
        url = f"http://localhost:{cfg['live_port']}{page_path()}"
        if running:
            # 這不是錯誤：每個交易日 08:40 的排程已經在背景開好了，不用再開第二個
            log(f"盤中服務已經在背景正常執行（每個交易日 08:40 自動啟動），不用再開；已幫你打開網頁 {url}")
            print("（這不是錯誤。這個視窗 5 秒後會自動關閉。）", flush=True)
        else:
            log(f"！連接埠 {cfg['live_port']} 被別的程式占用，盤中服務無法啟動。可以在 config.json 把 live_port 改成別的數字（例如 8766）。")
        if not a.no_browser and running:
            import webbrowser
            webbrowser.open(url)
        sys.exit(3 if running else 1)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=live.macro.run, args=(macro_stop,), daemon=True).start()
    url = f"http://localhost:{cfg['live_port']}{page_path()}"
    log(f"儀表板網址：{url}" + ("（同一個 Wi-Fi 的手機可用這台電腦的 IP 連線）" if cfg["live_lan"] else ""))
    if not a.no_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    # 等到開始時間
    while F.now_tw().strftime("%H:%M") < cfg["live_start"] and not QUIT.is_set():
        QUIT.wait(20)
    th = threading.Thread(target=poller, args=(live, cfg, stop), daemon=True)
    th.start()
    try:
        while not stop.is_set():
            time.sleep(1)
            if QUIT.is_set():
                stop.set()
                break
            # 假日：開盤後 10 分鐘加權指數日期仍不是今天 → 結束
            if F.now_tw().strftime("%H:%M") > "09:10" and live.idx.get("t00") and live.idx["t00"].get("d") not in (None, F.now_tw().strftime("%Y%m%d")):
                log("今天不是交易日，盤中服務結束。")
                stop.set()
    except KeyboardInterrupt:
        stop.set()
        QUIT.set()               # 按 Ctrl+C＝整個停止（不保留網頁服務）
        log("按了 Ctrl+C，盤中服務停止。要再開請雙擊 live.bat。")
    live.save_events()
    live.save_last()
    # 收盤後保留網頁服務：排程開的保留 30 分鐘；自己雙擊 live.bat 開的會一直開著（油價、匯率繼續每分鐘更新），關掉視窗才停
    if not QUIT.is_set():
        keep = a.foreground and float(cfg.get("live_macro_sec") or 0) > 0
        log("台股報價已停止；油價、匯率會繼續每分鐘更新，關掉這個視窗就停止。" if keep else "報價已停止；網頁服務會再保留 30 分鐘。")
        t_end = time.time() + 1800
        try:
            while not QUIT.is_set() and (keep or time.time() < t_end):
                QUIT.wait(1)
        except KeyboardInterrupt:
            log("按了 Ctrl+C，網頁服務停止。")
    macro_stop.set()
    srv.shutdown()
    srv.server_close()


if __name__ == "__main__":
    main()
