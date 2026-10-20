# -*- coding: utf-8 -*-
"""
台股盤後法人資金流向 — 資料腳本

用法（在 twflow 資料夾內）：
  python fetch.py                 一般執行：補齊最近 N 個交易日（第一次會回補）、重算、輸出 data\\data.js
  python fetch.py --show 2330     顯示某檔股票在最新交易日的原始數字（可多檔，用逗號分隔）
  python fetch.py --show 2330 --date 20261007
  python fetch.py --rebuild       不抓資料，只用本機快取重新計算、輸出
  python fetch.py --check         顯示每天「個股估算加總 vs 官方金額總表」的誤差

資料來源：臺灣證券交易所、證券櫃檯買賣中心公開資料。只用 Python 標準函式庫。
"""
import argparse
import base64
import csv
import gzip
import http.client
import datetime as dt
import html
import json
import os
import random
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
DAY_DIR = os.path.join(DATA_DIR, "days")
LOG_DIR = os.path.join(BASE_DIR, "logs")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SECTORS_PATH = os.path.join(BASE_DIR, "sectors.csv")
CALENDAR_PATH = os.path.join(DATA_DIR, "calendar.json")
UNIVERSE_PATH = os.path.join(DATA_DIR, "universe.json")

PARSER_VERSION = 8  # 正規化格式有變動時遞增，舊的 days\*.json 會自動重算（v8：發行股數、外資持股）

DEFAULT_CONFIG = {
    "backfill_trading_days": 60,
    "history_trading_days": 490,      # K 線與動能用的歷史（約 2 年）
    "pe_trading_days": 245,           # 本益比／殖利率／淨值比 往回抓幾天（約 1 年）
    "revenue_months": 26,             # 月營收往回抓幾個月
    "fin_quarters": 10,               # 季財報往回抓幾季（算單季要多 1～2 季）
    "refetch_after_hour": 21,
    "dashboard_trading_days": 60,
    "ohlc_trading_days": 35,
    "request_interval_sec": [4.0, 6.0],
    "retry_waits_sec": [10, 30, 90, 180],
    "recent_recheck_trading_days": 7,
    "max_attempts_per_missing_item": 6,
    "universe_refresh_days": 7,
    "mobile_sync_dir": "",
    "twse_base": "https://www.twse.com.tw",
    "tpex_base": "https://www.tpex.org.tw",
    "isin_base": "https://isin.twse.com.tw",
    "mops_base": "https://mopsov.twse.com.tw",
    "tdcc_url": "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5",   # 集保戶股權分散表（每週，只有最新一週）
    "chips_weeks": 52,                # 集保股權分散保留幾週
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
}

# 9 個資料項目：key -> (市場, 說明)
ITEMS = {
    "twse_quotes": ("TWSE", "上市每日收盤行情／指數／漲跌家數 MI_INDEX"),
    "twse_t86": ("TWSE", "上市個股三大法人 T86"),
    "twse_margin": ("TWSE", "上市融資融券 MI_MARGN"),
    "twse_bfi": ("TWSE", "上市法人買賣金額 BFI82U"),
    "tpex_quotes": ("TPEX", "上櫃收盤行情 dailyQuotes"),
    "tpex_insti": ("TPEX", "上櫃個股三大法人 dailyTrade"),
    "tpex_margin": ("TPEX", "上櫃融資融券 margin/balance"),
    "tpex_high": ("TPEX", "上櫃當日彙總 highlight"),
    "tpex_sum": ("TPEX", "上櫃法人買賣金額 insti/summary"),
    "twse_pe": ("TWSE", "上市本益比／殖利率／淨值比 BWIBBU_d"),
    "tpex_pe": ("TPEX", "上櫃本益比／殖利率／淨值比 peQryDate"),
    "twse_qfii": ("TWSE", "上市外資持股與發行股數 MI_QFIIS"),
    "tpex_qfii": ("TPEX", "上櫃外資持股與發行股數 insti/qfii"),
}
QFII_ITEMS = ["twse_qfii", "tpex_qfii"]
PE_ITEMS = ["twse_pe", "tpex_pe"]
REQUIRED_FOR_DAY = ["twse_quotes", "twse_t86", "tpex_quotes", "tpex_insti"]


# ----------------------------------------------------------------- 工具
def now_tw():
    # 以台灣時間為準（電腦時區不是台灣時也正確）
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=8)


class Log:
    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.path = os.path.join(LOG_DIR, now_tw().strftime("%Y%m%d") + ".log")
        self.f = open(self.path, "a", encoding="utf-8")

    def __call__(self, *a):
        msg = now_tw().strftime("%H:%M:%S") + " " + " ".join(str(x) for x in a)
        try:
            print(msg, flush=True)
        except Exception:
            print(msg.encode("ascii", "replace").decode(), flush=True)
        self.f.write(msg + "\n")
        self.f.flush()


log = None


def load_json(path, default=None):
    try:
        if not os.path.exists(path) and os.path.exists(path + ".gz"):
            with gzip.open(path + ".gz", "rt", encoding="utf-8") as f:
                return json.load(f)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, EOFError):
        return default


def save_raw(path, obj):
    """原始回應一律壓縮存成 .json.gz"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".gz.tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path + ".gz")
    if os.path.exists(path):
        os.remove(path)


def raw_exists(path):
    return os.path.exists(path) or os.path.exists(path + ".gz")


def raw_mtime(path):
    for p in (path + ".gz", path):
        if os.path.exists(p):
            return os.path.getmtime(p)
    return 0


def compress_old_raw():
    """把舊版未壓縮的原始檔轉成 .json.gz（節省空間）"""
    n = 0
    if not os.path.isdir(RAW_DIR):
        return
    for day in os.listdir(RAW_DIR):
        dd = os.path.join(RAW_DIR, day)
        if not os.path.isdir(dd):
            continue
        for fn in os.listdir(dd):
            if fn.endswith(".json"):
                p = os.path.join(dd, fn)
                j = load_json(p)
                if j is not None:
                    save_raw(p, j)
                    n += 1
    if n:
        log(f"已把 {n} 個舊原始檔壓縮成 .json.gz")


def save_json(path, obj, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    user = load_json(CONFIG_PATH, None)
    if user is None:
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
    else:
        cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    # 測試用：環境變數可覆寫來源網址
    for k in ("twse_base", "tpex_base", "isin_base", "mops_base"):
        env = os.environ.get("TWFLOW_" + k.upper())
        if env:
            cfg[k] = env
    return cfg


def num(s):
    """'1,234' -> 1234.0；'--'、''、'X' -> None"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = re.sub(r"<[^>]+>", "", str(s)).replace(",", "").strip()
    if s in ("", "-", "--", "---", "----", "X", "N/A", "除權", "除息", "除權息"):
        return None
    try:
        return float(s)
    except ValueError:
        m = re.match(r"^[+-]?\d+(\.\d+)?", s)
        return float(m.group(0)) if m else None


def ir(x):
    return int(round(x)) if x is not None else None


def r2(x):
    if x is None:
        return None
    v = round(x, 2)
    return int(v) if v == int(v) else v


# ----------------------------------------------------------------- HTTP
class Fetcher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.last = 0.0
        self.count = 0
        ctx = ssl.create_default_context()
        # Python 3.13 起預設啟用嚴格憑證檢查，證交所憑證會因此被拒；
        # 這裡仍然驗證憑證，只是關掉「嚴格模式」這個額外檢查。
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self.ctx = ctx

    def _sleep(self):
        lo, hi = self.cfg["request_interval_sec"]
        wait = random.uniform(lo, hi) - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)

    def get(self, url, referer=None, encoding="utf-8", data=None):
        waits = list(self.cfg["retry_waits_sec"])
        attempt = 0
        while True:
            self._sleep()
            self.last = time.time()
            self.count += 1
            req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None, headers={
                "User-Agent": self.cfg["user_agent"],
                "Accept": "application/json, text/html, */*",
                "Accept-Language": "zh-TW,zh;q=0.9",
                **({"Referer": referer} if referer else {}),
                **({"Content-Type": "application/x-www-form-urlencoded"} if data else {}),
            })
            try:
                t0 = time.time()
                with urllib.request.urlopen(req, timeout=40, context=self.ctx) as resp:
                    parts = []
                    while True:                       # 分段讀取，整個回應最多等 120 秒，避免伺服器很慢地吐資料時卡住
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        parts.append(chunk)
                        if time.time() - t0 > 120:
                            raise TimeoutError("回應超過 120 秒仍未傳完")
                    body = b"".join(parts)
                return body.decode(encoding, errors="replace")
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError) as e:   # HTTPException 含傳輸中斷（IncompleteRead）
                code = getattr(e, "code", None)
                if code == 404:
                    raise
                if attempt >= len(waits):
                    raise
                w = waits[attempt]
                attempt += 1
                log(f"  ! 連線失敗（{e}），{w} 秒後重試（第 {attempt} 次）")
                time.sleep(w)


def parse_json_body(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def ok_twse(j):
    return isinstance(j, dict) and str(j.get("stat", "")).upper() == "OK"


def item_url(cfg, key, d):
    ymd = d.strftime("%Y%m%d")
    slash = d.strftime("%Y/%m/%d")
    tw, tp = cfg["twse_base"], cfg["tpex_base"]
    return {
        "twse_quotes": f"{tw}/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json",
        "twse_t86": f"{tw}/rwd/zh/fund/T86?date={ymd}&selectType=ALLBUT0999&response=json",
        "twse_margin": f"{tw}/rwd/zh/marginTrading/MI_MARGN?date={ymd}&selectType=ALL&response=json",
        "twse_bfi": f"{tw}/rwd/zh/fund/BFI82U?type=day&dayDate={ymd}&response=json",
        "tpex_quotes": f"{tp}/www/zh-tw/afterTrading/dailyQuotes?date={slash}&type=EW&response=json",
        "tpex_insti": f"{tp}/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={slash}&response=json",
        "tpex_margin": f"{tp}/www/zh-tw/margin/balance?date={slash}&response=json",
        "tpex_high": f"{tp}/www/zh-tw/afterTrading/highlight?date={slash}&response=json",
        "tpex_sum": f"{tp}/www/zh-tw/insti/summary?type=Daily&date={slash}&response=json",
        "twse_pe": f"{tw}/rwd/zh/afterTrading/BWIBBU_d?date={ymd}&selectType=ALL&response=json",
        "tpex_pe": f"{tp}/www/zh-tw/afterTrading/peQryDate?date={slash}&response=json",
        "twse_qfii": f"{tw}/rwd/zh/fund/MI_QFIIS?date={ymd}&selectType=ALLBUT0999&response=json",
        "tpex_qfii": f"{tp}/www/zh-tw/insti/qfii?date={slash}&response=json",
    }[key]


def is_valid(key, j, d):
    if j is None:
        return False
    if key.startswith("twse"):
        if not ok_twse(j):
            return False
        if j.get("data"):
            return True
        return any((t.get("data") or []) for t in (j.get("tables") or []))
    # TPEx：要檢查回傳日期是否就是要求的日期（櫃買遇到沒資料時可能回傳最近一天）
    if str(j.get("stat", "")).lower() != "ok":
        return False
    rd = str(j.get("date", ""))
    if rd and rd != d.strftime("%Y%m%d"):
        return False
    tabs = j.get("tables") or []
    return any((t.get("data") or []) for t in tabs)


def raw_path(d, key):
    return os.path.join(RAW_DIR, d.strftime("%Y%m%d"), key + ".json")


def fetch_item(fx, cfg, key, d):
    url = item_url(cfg, key, d)
    ref = cfg["twse_base"] + "/zh/" if key.startswith("twse") else cfg["tpex_base"] + "/zh-tw/"
    text = fx.get(url, referer=ref)
    j = parse_json_body(text)
    return j


# ----------------------------------------------------------------- 除權息參考價（上市）
EXR_DIR = os.path.join(RAW_DIR, "_exright")
_EXR_MAP = None


def fetch_exrights(fx, cfg, first_day):
    """證交所「除權除息計算結果表」TWT49U，按月抓（一個月一個請求）；最近兩個月每次重抓"""
    today = now_tw().date()
    y, m = first_day.year, first_day.month
    months = []
    while (y, m) <= (today.year, today.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    for k, (y, m) in enumerate(months):
        p = os.path.join(EXR_DIR, f"{y}{m:02d}.json")
        if raw_exists(p) and k < len(months) - 2:
            continue
        start = dt.date(y, m, 1)
        end = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
        url = f"{cfg['twse_base']}/rwd/zh/exRight/TWT49U?startDate={start:%Y%m%d}&endDate={min(end, today):%Y%m%d}&response=json"
        try:
            j = parse_json_body(fx.get(url, referer=cfg["twse_base"] + "/zh/"))
        except Exception as e:
            log(f"  ! 除權息資料 {y}-{m:02d} 抓取失敗：{e}")
            continue
        if isinstance(j, dict) and (ok_twse(j) or "沒有符合" in str(j.get("stat", ""))):
            save_raw(p, {"stat": j.get("stat"), "fields": j.get("fields"), "data": j.get("data") or []})
            log(f"  除權息資料 {y}-{m:02d} ✓（{len(j.get('data') or [])} 筆）")
    global _EXR_MAP
    _EXR_MAP = None


def exright_map():
    """{YYYYMMDD: {代號: 除權息參考價}}"""
    global _EXR_MAP
    if _EXR_MAP is not None:
        return _EXR_MAP
    out = {}
    if os.path.isdir(EXR_DIR):
        for fn in os.listdir(EXR_DIR):
            j = load_json(os.path.join(EXR_DIR, fn.replace(".gz", "")))
            if not j or not j.get("fields"):
                continue
            f = j["fields"]
            try:
                i_d, i_c, i_r = f.index("資料日期"), f.index("股票代號"), f.index("除權息參考價")
            except ValueError:
                continue
            for r in j["data"]:
                m = re.match(r"(\d+)年(\d+)月(\d+)日", str(r[i_d]))
                ref = num(r[i_r])
                if not m or not ref:
                    continue
                ymd = f"{int(m.group(1)) + 1911}{int(m.group(2)):02d}{int(m.group(3)):02d}"
                out.setdefault(ymd, {})[str(r[i_c]).strip()] = ref
    _EXR_MAP = out
    return out


# ----------------------------------------------------------------- 股票清單與產業別
def fetch_universe(fx, cfg, force=False):
    uni = load_json(UNIVERSE_PATH, None)
    if uni and not force:
        age = (now_tw() - dt.datetime.fromisoformat(uni["updated"])).days
        if age < cfg["universe_refresh_days"]:
            return uni
    log("更新股票清單與產業別（ISIN 頁面）…")
    stocks = {}
    for mode, mkt in (("2", "上市"), ("4", "上櫃")):
        url = f"{cfg['isin_base']}/isin/C_public.jsp?strMode={mode}"
        try:
            text = fx.get(url, encoding="cp950")
        except Exception as e:
            log(f"  ! 無法取得 {mkt} ISIN 清單：{e}")
            continue
        n = 0
        for row in re.findall(r"<tr>(.*?)</tr>", text, flags=re.S | re.I):
            cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)]
            if len(cells) < 6:
                continue
            first = cells[0].replace("　", " ").strip()
            m = re.match(r"^(\S+)\s+(.+)$", first)
            if not m:
                continue
            code, name = m.group(1), m.group(2).strip()
            cfi = cells[5].strip()
            industry = cells[4].strip()
            if not cfi.startswith("ES"):   # ES = 普通股；排除 ETF、權證、特別股、TDR 等
                continue
            if not re.match(r"^\d{4}$", code):
                continue
            stocks[code] = {"name": name, "mkt": mkt, "ind": industry or "未分類"}
            n += 1
        log(f"  {mkt}普通股 {n} 檔")
    if len(stocks) < int(os.environ.get("TWFLOW_MIN_UNIVERSE", "500")):
        if uni:
            log("  ! ISIN 清單筆數異常，沿用舊清單")
            return uni
        log("  ! ISIN 清單取得失敗，暫以代號規則判斷普通股（產業別為「未分類」）")
        uni = {"updated": "1970-01-01T00:00:00", "stocks": {}, "fallback": True}
        save_json(UNIVERSE_PATH, uni)
        return uni
    uni = {"updated": now_tw().isoformat(timespec="seconds"), "stocks": stocks}
    save_json(UNIVERSE_PATH, uni)
    return uni


def in_universe(uni, code, name=""):
    if code in uni.get("stocks", {}):
        return True
    # 已下市（目前清單沒有）的普通股：4 碼數字代號、非 ETF（00 開頭）、非存託憑證
    return bool(re.match(r"^[1-9]\d{3}$", code)) and "-DR" not in name


# ----------------------------------------------------------------- 板塊對照檔
CONCEPT_SEED = [
    # (板塊名稱, [股票代號...])　— 初稿，請依自己的看法增刪
    ("AI 伺服器組裝", ["2382", "3231", "6669", "2317", "2356", "2376", "3706", "2357", "4938", "2324"]),
    ("伺服器機殼與滑軌", ["8210", "2059", "3013", "3693", "6117"]),
    ("散熱", ["3017", "3324", "3653", "2421", "6230", "3338", "8996"]),
    ("PCB 與載板", ["3037", "8046", "3189", "2368", "4958", "3044", "2313", "6153", "5469", "6269", "2367", "3715", "6191", "5439", "8155"]),
    ("銅箔基板與玻纖材料", ["2383", "6274", "6213", "8358", "1815", "1802"]),
    ("被動元件", ["2327", "2492", "3026", "2456", "6173", "2375", "6449", "8042", "2478", "6284", "3236"]),
    ("記憶體", ["2408", "2344", "2337", "3260", "8299", "4967", "3006", "2451", "8271", "5351", "6239"]),
    ("矽光子與光通訊", ["3363", "4979", "3081", "6442", "3450", "4977", "2455", "3105", "3163", "6451"]),
    ("先進封裝 CoWoS 設備", ["3131", "6187", "3583", "2467", "6640", "1560", "3413", "5536"]),
    ("封測與測試介面", ["3711", "2449", "6239", "6515", "6223", "6510", "3374", "2441"]),
    ("晶圓代工", ["2330", "2303", "5347", "6770"]),
    ("IC 設計大型股", ["2454", "3034", "2379", "4966", "5274", "8016", "6415", "5269"]),
    ("ASIC 與矽智財", ["3443", "3661", "3035", "3529", "6643", "6533"]),
    ("網通與交換器", ["2345", "3596", "5388", "6285", "3380", "4906", "2332"]),
    ("低軌衛星", ["2314", "3491", "6285", "2313", "6271"]),
    ("高速連接器與線材", ["3533", "2392", "3665", "6205", "3023", "6290", "3605"]),
    ("電源與 BBU", ["2308", "2301", "6409", "3015", "6412", "2457", "3211", "6781"]),
    ("重電與電網", ["1503", "1513", "1519", "1514", "1504", "1609", "1605"]),
    ("機器人與自動化", ["2049", "1590", "2359", "4583", "1597", "8374"]),
    ("國防與航太", ["2634", "8033", "2645", "4572"]),
    ("廠務工程", ["2404", "6139", "6196", "5536"]),
    ("面板", ["2409", "3481"]),
    ("光學鏡頭", ["3008", "3406"]),
    ("貨櫃航運", ["2603", "2609", "2615"]),
]


def read_sectors():
    """回傳 [(板塊, 代號, 分類)]。分類欄可省略（預設「概念」）。"""
    if not os.path.exists(SECTORS_PATH):
        return None
    rows = []
    for enc in ("utf-8-sig", "cp950"):
        try:
            with open(SECTORS_PATH, "r", encoding=enc, newline="") as f:
                rdr = csv.reader(f)
                rows = list(rdr)
            break
        except UnicodeDecodeError:
            continue
    out = []
    for i, r in enumerate(rows):
        if not r or not r[0].strip() or r[0].strip().startswith("#"):
            continue
        if i == 0 and ("板塊" in r[0] or "sector" in r[0].lower()):
            continue
        name = r[0].strip()
        code = r[1].strip() if len(r) > 1 else ""
        kind = r[3].strip() if len(r) > 3 and r[3].strip() else "概念"
        code = code.replace("=", "").replace('"', "").strip()
        if re.match(r"^\d{1,3}$", code):  # Excel 可能把 0050 存成 50
            code = code.zfill(4)
        if name and code:
            out.append((name, code, kind))
    return out


def create_sectors_csv(uni):
    stocks = uni.get("stocks", {})
    lines = []
    for code in sorted(stocks):
        s = stocks[code]
        lines.append((s["ind"], code, s["name"], "產業"))
    for name, codes in CONCEPT_SEED:
        for c in codes:
            nm = stocks.get(c, {}).get("name", "")
            if stocks and c not in stocks:
                continue
            lines.append((name, c, nm, "概念"))
    with open(SECTORS_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["板塊名稱", "股票代號", "股票名稱(參考用)", "分類(產業/概念)"])
        for ln in lines:
            w.writerow(ln)
    log(f"已建立板塊對照檔 sectors.csv（{len(lines)} 列），之後可自行編輯，腳本不會再覆寫。")


# ----------------------------------------------------------------- 解析
def tbl_find(j, pred):
    for t in j.get("tables") or []:
        if pred(t):
            return t
    return None


def sign_from_html(s):
    s = str(s)
    txt = re.sub(r"<[^>]+>", "", s).strip()
    if "-" in txt or "green" in s:
        return -1
    if "+" in txt or "red" in s:
        return 1
    return 0


def parse_twse_quotes(j):
    """回傳 (stocks{code:{o,h,l,c,chg,vol,val}}, index{close,chg,pct}, updown{...})"""
    q = tbl_find(j, lambda t: "證券代號" in (t.get("fields") or []) and "收盤價" in (t.get("fields") or []))
    out = {}
    if q:
        f = q["fields"]
        ix = {k: f.index(k) for k in ("證券代號", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價", "漲跌(+/-)", "漲跌價差")}
        for r in q["data"]:
            code = str(r[ix["證券代號"]]).strip()
            sg = sign_from_html(r[ix["漲跌(+/-)"]])
            d = num(r[ix["漲跌價差"]])
            out[code] = {
                "n": str(r[1]).strip(),
                "o": num(r[ix["開盤價"]]), "h": num(r[ix["最高價"]]), "l": num(r[ix["最低價"]]),
                "c": num(r[ix["收盤價"]]),
                "chg": (sg * d) if d is not None else None,
                "vol": num(r[ix["成交股數"]]) or 0.0, "val": num(r[ix["成交金額"]]) or 0.0,
            }
    idx = None
    t = tbl_find(j, lambda t: (t.get("fields") or [None])[0] == "指數")
    if t:
        for r in t["data"]:
            if str(r[0]).strip() == "發行量加權股價指數":
                sg = sign_from_html(r[2])
                idx = {"close": num(r[1]), "chg": sg * (num(r[3]) or 0), "pct": sg * abs(num(r[4]) or 0)}
    ud = None
    t = tbl_find(j, lambda t: (t.get("fields") or [None])[0] == "類型")
    if t:
        ud = {}
        col = t["fields"].index("股票") if "股票" in t["fields"] else 2
        for r in t["data"]:
            label = str(r[0])
            m = re.match(r"([\d,]+)(?:\(([\d,]+)\))?", str(r[col]).strip())
            if not m:
                continue
            a = int(m.group(1).replace(",", ""))
            b = int(m.group(2).replace(",", "")) if m.group(2) else 0
            if label.startswith("上漲"):
                ud["up"], ud["limit_up"] = a, b
            elif label.startswith("下跌"):
                ud["down"], ud["limit_down"] = a, b
            elif label.startswith("持平"):
                ud["flat"] = a
    return out, idx, ud


def parse_twse_t86(j):
    t = j["tables"][0] if j.get("tables") else {"fields": j.get("fields"), "data": j.get("data")}
    f = t["fields"]
    def col(name):
        for i, x in enumerate(f):
            if x.startswith(name):
                return i
        raise KeyError(name)
    c_fi = col("外陸資買賣超股數")
    c_fd = col("外資自營商買賣超股數")
    c_it = col("投信買賣超股數")
    c_dsf = col("自營商買賣超股數(自行買賣)")
    c_dsh = col("自營商買賣超股數(避險)")
    out = {}
    for r in t["data"]:
        code = str(r[0]).strip()
        out[code] = {
            "fi": (num(r[c_fi]) or 0) + (num(r[c_fd]) or 0),
            "it": num(r[c_it]) or 0, "dsf": num(r[c_dsf]) or 0, "dsh": num(r[c_dsh]) or 0,
        }
    return out


def nth_col(f, name, n=1, default=None):
    """欄位名稱第 n 次出現的位置（融資、融券欄位名稱重複，例如兩個「今日餘額」）"""
    hits = [k for k, x in enumerate(f) if str(x).strip().startswith(name)]
    return hits[n - 1] if len(hits) >= n else default


def parse_twse_margin(j):
    t = tbl_find(j, lambda t: "代號" in (t.get("fields") or []))
    out = {}
    if not t:
        return out
    f = [str(x).strip() for x in t.get("fields") or []]
    i_lim = nth_col(f, "次一營業日限額", 1, 7)
    i_sprev, i_sbal = nth_col(f, "前日餘額", 2, 11), nth_col(f, "今日餘額", 2, 12)
    for r in t["data"]:
        code = str(r[0]).strip()
        mb, ms, mr, prev, bal = (num(r[i]) for i in (2, 3, 4, 5, 6))
        if bal is None:
            continue
        g = lambda k: num(r[k]) if k is not None and k < len(r) else None
        sprev, sbal = g(i_sprev), g(i_sbal)
        out[code] = {"mb": mb or 0, "md": (bal - (prev or 0)), "mbal": bal, "mlim": g(i_lim),
                     "sbal": sbal, "sd": (sbal - (sprev or 0)) if sbal is not None else None}
    return out


def parse_twse_bfi(j):
    res = {"fi": 0.0, "it": 0.0, "dsf": 0.0, "dsh": 0.0, "total": 0.0}
    for r in j.get("data") or []:
        name, net = str(r[0]), num(r[3]) or 0
        if name.startswith("自營商(自行"):
            res["dsf"] += net
        elif name.startswith("自營商(避險"):
            res["dsh"] += net
        elif name.startswith("投信"):
            res["it"] += net
        elif name.startswith("外資"):
            res["fi"] += net
        elif name.startswith("合計"):
            res["total"] = net
    return res


def parse_tpex_quotes(j):
    out = {}
    for t in j.get("tables") or []:
        f = t.get("fields") or []
        if "代號" not in f or "收盤" not in f:
            continue
        ix = {k: f.index(k) for k in ("代號", "收盤", "漲跌", "開盤", "最高", "最低", "均價", "成交股數")}
        iv = next(i for i, x in enumerate(f) if x.startswith("成交金額"))
        inr = next((i for i, x in enumerate(f) if x.replace(" ", "").startswith("次日參考價")), None)
        for r in t.get("data") or []:
            code = str(r[ix["代號"]]).strip()
            vol = num(r[ix["成交股數"]]) or 0.0
            val = num(r[iv]) or 0.0
            out[code] = {
                "n": str(r[1]).strip(),
                "o": num(r[ix["開盤"]]), "h": num(r[ix["最高"]]), "l": num(r[ix["最低"]]),
                "c": num(r[ix["收盤"]]), "chg": num(r[ix["漲跌"]]),
                "vol": vol, "val": val, "avg": num(r[ix["均價"]]),
                "nref": num(r[inr]) if inr is not None else None,
            }
    return out


def parse_tpex_insti(j):
    # 欄位順序（已實測）：代號,名稱, 外資(不含自營)買/賣/超, 外資自營商買/賣/超, 外資合計買/賣/超,
    # 投信買/賣/超, 自營(自行)買/賣/超, 自營(避險)買/賣/超, 自營合計買/賣/超, 三大法人合計
    out = {}
    t = j["tables"][0]
    for r in t.get("data") or []:
        if len(r) < 24:
            continue
        code = str(r[0]).strip()
        out[code] = {"fi": num(r[10]) or 0, "it": num(r[13]) or 0, "dsf": num(r[16]) or 0, "dsh": num(r[19]) or 0}
    return out


def parse_tpex_margin(j):
    out = {}
    t = j["tables"][0]
    f = [str(x).strip() for x in t.get("fields") or []]
    i_lim = nth_col(f, "資限額", 1, 9)
    i_sprev, i_sbal = nth_col(f, "前券餘額", 1, 10), nth_col(f, "券餘額", 1, 14)
    for r in t.get("data") or []:
        code = str(r[0]).strip()
        prev, mb, ms, mr, bal = (num(r[i]) for i in (2, 3, 4, 5, 6))
        if bal is None:
            continue
        g = lambda k: num(r[k]) if k is not None and k < len(r) else None
        sprev, sbal = g(i_sprev), g(i_sbal)
        out[code] = {"mb": mb or 0, "md": bal - (prev or 0), "mbal": bal, "mlim": g(i_lim),
                     "sbal": sbal, "sd": (sbal - (sprev or 0)) if sbal is not None else None}
    return out


def parse_tpex_high(j):
    t = j["tables"][0]
    f, r = t["fields"], t["data"][0]
    g = lambda k: num(r[f.index(k)]) if k in f else None
    close, chg = g("收市指數"), g("指數漲跌")
    pct = (chg / (close - chg) * 100) if close and chg is not None and close != chg else None
    return {"index": {"close": close, "chg": chg, "pct": round(pct, 2) if pct is not None else None},
            "updown": {"up": int(g("上漲家數") or 0), "limit_up": int(g("漲停家數") or 0),
                       "down": int(g("下跌家數") or 0), "limit_down": int(g("跌停家數") or 0),
                       "flat": int(g("平盤家數") or 0)}}


def parse_pe(j):
    """本益比／殖利率／淨值比：回傳 {代號: (本益比, 殖利率%, 淨值比)}，"-" 或 0 → None"""
    t = j["tables"][0] if j.get("tables") else {"fields": j.get("fields"), "data": j.get("data")}
    f = [str(x).strip() for x in (t.get("fields") or [])]
    def col(*names):
        for n in names:
            for k, x in enumerate(f):
                if x.startswith(n):
                    return k
        return None
    ic, ipe, idy, ipb = col("證券代號", "股票代號"), col("本益比"), col("殖利率"), col("股價淨值比")
    out = {}
    for r in t.get("data") or []:
        code = str(r[ic]).strip()
        g = lambda k: (lambda v: v if v else None)(num(r[k])) if k is not None else None
        out[code] = (g(ipe), g(idy), g(ipb))
    return out


def parse_qfii(j):
    """外資持股統計（上市 MI_QFIIS、上櫃 insti/qfii）：回傳 {代號: (發行股數, 外資持有股數)}，單位：股"""
    t = j["tables"][0] if j.get("tables") else {"fields": j.get("fields"), "data": j.get("data")}
    f = [str(x).strip() for x in (t.get("fields") or [])]
    def col(*names):
        for n in names:
            for k, x in enumerate(f):
                if x.startswith(n):
                    return k
        return None
    ic = col("證券代號", "代號")
    ish = col("發行股數")
    ifh = col("全體外資及陸資持有股數", "僑外資及陸資持有股數")
    out = {}
    if ic is None or ish is None:
        return out
    for r in t.get("data") or []:
        code = str(r[ic]).strip()
        sh = num(r[ish])
        fh = num(r[ifh]) if ifh is not None else None
        if sh:
            out[code] = (sh, fh)
    return out


def parse_tpex_sum(j):
    res = {"fi": 0.0, "it": 0.0, "dsf": 0.0, "dsh": 0.0, "total": 0.0}
    for r in j["tables"][0].get("data") or []:
        name = str(r[0]).replace("　", "").strip()
        net = num(r[3]) or 0
        if name.startswith("外資及陸資合計"):
            res["fi"] = net
        elif name.startswith("投信"):
            res["it"] = net
        elif name.startswith("自營商(自行"):
            res["dsf"] = net
        elif name.startswith("自營商(避險"):
            res["dsh"] = net
        elif name.startswith("三大法人"):
            res["total"] = net
    return res


def build_day(d, uni):
    """把一天的原始檔整理成 days\\YYYYMMDD.json"""
    ymd = d.strftime("%Y%m%d")
    raw = {k: load_json(raw_path(d, k)) for k in ITEMS}
    if any(raw[k] is None for k in REQUIRED_FOR_DAY):
        return None
    tq, taiex, tud = parse_twse_quotes(raw["twse_quotes"])
    tt = parse_twse_t86(raw["twse_t86"])
    pq = parse_tpex_quotes(raw["tpex_quotes"])
    pt = parse_tpex_insti(raw["tpex_insti"])
    tm = parse_twse_margin(raw["twse_margin"]) if raw["twse_margin"] else None
    pm = parse_tpex_margin(raw["tpex_margin"]) if raw["tpex_margin"] else None
    high = parse_tpex_high(raw["tpex_high"]) if raw["tpex_high"] else None
    pe = {}
    for k in PE_ITEMS:
        if raw.get(k):
            try:
                pe.update(parse_pe(raw[k]))
            except Exception as e:
                log(f"  ! {ymd} {ITEMS[k][1]} 解析失敗：{e}")
    qf = {"TWSE": {}, "TPEX": {}}
    for k, mk in (("twse_qfii", "TWSE"), ("tpex_qfii", "TPEX")):
        if raw.get(k):
            try:
                qf[mk] = parse_qfii(raw[k])
            except Exception as e:
                log(f"  ! {ymd} {ITEMS[k][1]} 解析失敗：{e}")

    xmap = exright_map().get(ymd, {})
    stocks = {}
    extra_names = {}  # 不在目前清單（已下市）的股票名稱
    # 估算檢查：所有 T86 列（含 ETF）股數 × 均價，對照官方金額
    check = {}
    for mkt, quotes, inst, margin in (("TWSE", tq, tt, tm), ("TPEX", pq, pt, pm)):
        est = {"fi": 0.0, "it": 0.0, "dsf": 0.0, "dsh": 0.0}
        for code, q in quotes.items():
            vwap = q.get("avg") if mkt == "TPEX" else None
            if not vwap and q["vol"]:
                vwap = q["val"] / q["vol"]
            ins = inst.get(code)
            if ins and vwap:
                for k in est:
                    est[k] += ins[k] * vwap
            if not in_universe(uni, code, q.get("n", "")):
                continue
            if code not in uni.get("stocks", {}):
                extra_names[code] = [q.get("n", code), "上市" if mkt == "TWSE" else "上櫃"]
            ins = ins or {"fi": 0, "it": 0, "dsf": 0, "dsh": 0}
            mg = (margin or {}).get(code)
            qq = qf[mkt].get(code) or (None, None)
            stocks[code] = [
                r2(q["o"]), r2(q["h"]), r2(q["l"]), r2(q["c"]), r2(q["chg"]),
                int(round(q["vol"] / 1000)),                 # 成交量（張）
                r2(vwap) if vwap else None,                   # 當日均價
                int(round(ins["fi"] / 1000)), int(round(ins["it"] / 1000)),
                int(round(ins["dsf"] / 1000)), int(round(ins["dsh"] / 1000)),  # 法人買賣超（張）
                int(mg["mb"]) if mg else None, int(mg["md"]) if mg else None, int(mg["mbal"]) if mg else None,  # 融資買進、增減、餘額（張）
                round(q["val"] / 1e6, 1),                     # 成交金額（百萬元）
                r2(q.get("nref")),                            # 上櫃：次日參考價
                r2(xmap.get(code)) if mkt == "TWSE" else None,  # 上市：除權息參考價（除權息當天）
                *(pe.get(code) or (None, None, None)),        # 本益比、殖利率(%)、股價淨值比
                *((ir(mg.get("mlim")), ir(mg.get("sbal")), ir(mg.get("sd"))) if mg else (None, None, None)),  # 融資限額、融券餘額、融券增減（張）
                ir(qq[0] / 1000) if qq[0] else None,          # 發行股數（張）
                ir(qq[1] / 1000) if qq[1] is not None else None,   # 外資及陸資持股（張）
            ]
        check[mkt] = {k: round(v / 1e8, 2) for k, v in est.items()}
    official = {}
    if raw["twse_bfi"]:
        official["TWSE"] = {k: round(v / 1e8, 2) for k, v in parse_twse_bfi(raw["twse_bfi"]).items()}
    if raw["tpex_sum"]:
        official["TPEX"] = {k: round(v / 1e8, 2) for k, v in parse_tpex_sum(raw["tpex_sum"]).items()}
    day = {
        "v": PARSER_VERSION, "date": ymd,
        "fields": ["o", "h", "l", "c", "chg", "vol", "vwap", "fi", "it", "dsf", "dsh", "mb", "md", "mbal", "val", "nref", "xref", "pe", "dy", "pb", "mlim", "sbal", "sd", "shr", "fh"],
        "market": {
            "taiex": taiex, "tpex": high["index"] if high else None,
            "updown_twse": tud, "updown_tpex": high["updown"] if high else None,
            "inst_official": official, "inst_estimated": check,
            "has_margin": {"TWSE": tm is not None, "TPEX": pm is not None},
            "has_qfii": {"TWSE": bool(qf["TWSE"]), "TPEX": bool(qf["TPEX"])},
        },
        "stocks": stocks,
        "names": extra_names,
    }
    save_json(os.path.join(DAY_DIR, ymd + ".json"), day, compact=True)
    return day


# ----------------------------------------------------------------- 交易日與抓取
def weekdays_back(start, n_max=400):
    d = start
    for _ in range(n_max):
        if d.weekday() < 5:
            yield d
        d -= dt.timedelta(days=1)


def sync(cfg, fx, uni):
    """往回補齊交易日資料：
    - 最近 backfill_trading_days 天：9 個項目全抓（含融資、官方金額）
    - 更早到 history_trading_days 天：只抓行情與三大法人（動能與回測用）
    - 最近 2 個交易日：若是在當天 refetch_after_hour 點以前抓的，晚上會重抓覆蓋（證交所盤後會修正數字）"""
    cal = load_json(CALENDAR_PATH, {"holidays": [], "trading": [], "attempts": {}})
    holidays = set(cal["holidays"])
    trading = set(cal["trading"])
    attempts = cal.setdefault("attempts", {})
    now = now_tw()
    today = now.date()
    full_n = cfg["backfill_trading_days"]
    target = max(full_n, cfg.get("history_trading_days", full_n))
    recheck = cfg["recent_recheck_trading_days"]
    rh = cfg.get("refetch_after_hour", 21)
    found = 0

    def save_cal():
        cal["holidays"] = sorted(holidays)
        cal["trading"] = sorted(trading)
        save_json(CALENDAR_PATH, cal)

    def stale(path, d):
        """在該交易日 rh 點以前抓的，且現在已過 rh 點 → 需要重抓"""
        cut = (dt.datetime(d.year, d.month, d.day, rh) - dt.timedelta(hours=8)).replace(tzinfo=dt.timezone.utc).timestamp()
        return raw_mtime(path) < cut <= time.time()

    t_start = time.time()
    fails = [0]

    def safe_fetch(key, d):
        """單一項目重試後仍失敗：記下來、這次先跳過，不讓整個執行中斷；連續失敗 5 次才停止（可能被暫時封鎖）"""
        try:
            j = fetch_item(fx, cfg, key, d)
            fails[0] = 0
            return j, True
        except Exception as e:
            fails[0] += 1
            log(f"  ! {d.strftime('%Y%m%d')} {ITEMS[key][1]} 重試後仍失敗，這次先跳過：{e}")
            if fails[0] >= 5:
                raise RuntimeError("連續 5 個請求失敗，可能被暫時封鎖或網路中斷，先停止；下次執行會接著抓") from e
            return None, False

    hist_path = os.path.join(DATA_DIR, "history.js")
    new_old_days = 0
    for d in weekdays_back(today, n_max=target * 2 + 60):
        if found >= target:
            break
        ymd = d.strftime("%Y%m%d")
        if ymd in holidays:
            continue
        p = raw_path(d, "twse_quotes")
        if not raw_exists(p):
            log(f"{ymd} 檢查是否為交易日…")
            j, ok = safe_fetch("twse_quotes", d)
            if not ok:
                continue                      # 連線問題：不要誤判成休市
            if is_valid("twse_quotes", j, d):
                save_raw(p, j)
                trading.add(ymd)
                save_cal()
            else:
                if d < today:
                    holidays.add(ymd)
                    log(f"  {ymd} 休市")
                    save_cal()
                else:
                    log(f"  {ymd} 今日資料尚未公布")
                continue
        trading.add(ymd)
        found += 1
        keys = list(ITEMS) if found <= full_n else REQUIRED_FOR_DAY + (PE_ITEMS if found <= cfg.get("pe_trading_days", 245) else [])
        touch_lock()
        refresh = found <= 2
        got_new = False
        for key in keys:
            p = raw_path(d, key)
            have = raw_exists(p)
            if have and not (refresh and stale(p, d)):
                continue
            if not have:
                ak = ymd + ":" + key
                if found > recheck and attempts.get(ak, 0) >= cfg["max_attempts_per_missing_item"]:
                    continue
            j, ok = safe_fetch(key, d)
            if not ok:
                continue
            if is_valid(key, j, d):
                save_raw(p, j)
                got_new = True
                log(f"  {ymd} {ITEMS[key][1]} {'已重抓更新' if have else '✓'}")
            elif not have:
                ak = ymd + ":" + key
                attempts[ak] = attempts.get(ak, 0) + 1
                log(f"  {ymd} {ITEMS[key][1]} 尚無資料（第 {attempts[ak]} 次）")
            save_cal()
        day_file = os.path.join(DAY_DIR, ymd + ".json")
        old = load_json(day_file)
        if got_new or old is None or old.get("v") != PARSER_VERSION:
            build_day(d, uni)
            if found > full_n and got_new:          # 只有真的抓到新資料才算（格式升級重算不用中途輸出）
                new_old_days += 1
        # 第一次回補 1 年要很久：最近 60 天抓完就先輸出儀表板與動能歷史，之後每補 40 天更新一次，不用等全部跑完
        if found == full_n and target > full_n and (not os.path.exists(hist_path) or not os.path.exists(os.path.join(DATA_DIR, "data.js"))):
            log("最近的資料已齊，先輸出儀表板與動能歷史（較早的歷史會在背景繼續回補）")
            build_dashboard_data(cfg, uni)
            build_history(cfg, uni)
        elif new_old_days >= 40:
            build_history(cfg, uni)
            new_old_days = 0
        if found > full_n and found % 20 == 0:
            log(f"  歷史回補進度：{found}／{target} 個交易日，已花 {(time.time() - t_start) / 60:.0f} 分鐘")
    save_cal()
    return sorted(trading)


# ----------------------------------------------------------------- 輸出給儀表板
def build_dashboard_data(cfg, uni):
    files = sorted(f[:-5] for f in os.listdir(DAY_DIR) if f.endswith(".json")) if os.path.isdir(DAY_DIR) else []
    files = files[-cfg["dashboard_trading_days"]:]
    days = [load_json(os.path.join(DAY_DIR, f + ".json")) for f in files]
    days = [x for x in days if x]
    if not days:
        log("! 沒有可用的交易日資料")
        return None
    codes = sorted(set().union(*[set(x["stocks"]) for x in days]))
    pos = {c: i for i, c in enumerate(codes)}
    names, mkts, inds = stock_meta(codes, uni, days)
    # 板塊
    if not os.path.exists(SECTORS_PATH):
        create_sectors_csv(uni)
    sec_rows = read_sectors() or []
    sectors, sidx, unknown = [], {}, set()
    for name, code, kind in sec_rows:
        if code not in pos:
            unknown.add(code)
            continue
        if name not in sidx:
            sidx[name] = len(sectors)
            sectors.append({"name": name, "type": kind, "m": []})
        s = sectors[sidx[name]]
        if pos[code] not in s["m"]:
            s["m"].append(pos[code])
    if unknown:
        log(f"  提醒：sectors.csv 有 {len(unknown)} 個代號不在目前的普通股清單（已略過）：{', '.join(sorted(unknown)[:30])}")
    fields = [f for f in days[-1]["fields"] if f not in ("nref", "xref", "pe", "dy", "pb")]
    alld = sorted(f[:-5] for f in os.listdir(DAY_DIR) if f.endswith(".json"))
    prevd = [x for x in alld if x < days[0]["date"]][-1:]
    pre = [load_json(os.path.join(DAY_DIR, x + ".json")) for x in prevd]
    add_ref([x for x in pre if x] + days, codes, pos)
    n_ohlc = cfg["ohlc_trading_days"]
    out_days = []
    for k, d in enumerate(days):
        cols = {f: [None] * len(codes) for f in fields}
        cols["ref"] = [None] * len(codes)
        for code, arr in d["stocks"].items():
            i = pos[code]
            for fi, f in enumerate(fields):
                if d["fields"] == fields:
                    cols[f][i] = arr[fi]
                elif f in d["fields"]:
                    cols[f][i] = arr[d["fields"].index(f)]
            cols["ref"][i] = d["_ref"].get(code)
        if k < len(days) - n_ohlc:
            for f in ("o", "h", "l"):
                cols.pop(f, None)
        if k < len(days) - 5:
            cols.pop("shr", None)        # 發行股數很少變動：只放最近 5 天，減少檔案大小
        out_days.append({"d": d["date"], "mk": d["market"], **cols})
    data = {
        "generated": now_tw().isoformat(timespec="seconds"),
        "units": {"vol": "張", "fi/it/dsf/dsh": "張（買賣超）", "mb/md/mbal/mlim": "張（融資）", "sbal/sd": "張（融券）", "shr": "張（發行股數）", "fh": "張（外資及陸資持股）","val": "百萬元", "vwap": "元"},
        "codes": codes, "names": names, "mkt": mkts, "ind": inds,
        "sectors": sectors, "days": out_days,
    }
    js_path = os.path.join(DATA_DIR, "data.js")
    with open(js_path + ".tmp", "w", encoding="utf-8") as f:
        f.write("window.TWFLOW_DATA=")
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    os.replace(js_path + ".tmp", js_path)
    size = os.path.getsize(js_path) / 1e6
    log(f"已輸出 data\\data.js：{len(out_days)} 個交易日（{out_days[0]['d']}～{out_days[-1]['d']}），{len(codes)} 檔，{len(sectors)} 個板塊，{size:.1f} MB")
    sync_mobile(cfg)
    return data


def stock_meta(codes, uni, days):
    us = uni.get("stocks", {})
    extra = {}
    for d in days:
        extra.update(d.get("names") or {})
    names, mkts, inds = [], [], []
    for c in codes:
        if c in us:
            names.append(us[c]["name"]); mkts.append("L" if us[c]["mkt"] == "上市" else "O"); inds.append(us[c]["ind"])
        else:
            n, m = extra.get(c, [c, ""])
            names.append(n); mkts.append("L" if m == "上市" else "O" if m == "上櫃" else "?"); inds.append("已下市或未分類")
    return names, mkts, inds


HIST_FIELDS = ["o", "h", "l", "c", "chg", "vol", "vwap", "fi", "it", "dsf", "dsh", "val", "pe", "dy", "pb", "ref"]


def add_ref(days, codes, pos):
    """每天每檔的「參考價」（前一日收盤經除權息調整後的價格），還原報酬＝收盤 ÷ 參考價 − 1
    上市：除權息當天用證交所除權息參考價，其餘用 收盤 − 漲跌；上櫃：用前一交易日公布的「次日參考價」"""
    prev_nref = {}
    for d in days:
        f = d["fields"]
        ic, ich = f.index("c"), f.index("chg")
        inr = f.index("nref") if "nref" in f else None
        ixr = f.index("xref") if "xref" in f else None
        ref = {}
        for code, a in d["stocks"].items():
            c, ch = a[ic], a[ich]
            xr = a[ixr] if ixr is not None else None
            r = None
            if xr:
                r = xr
            elif code in prev_nref and prev_nref[code]:
                r = prev_nref[code]
            elif c is not None and ch is not None:
                r = c - ch
            ref[code] = r2(r) if r else None
        d["_ref"] = ref
        prev_nref = {code: a[inr] for code, a in d["stocks"].items()} if inr is not None else {}


def build_history(cfg, uni):
    """動能與回測用的長期歷史：gzip 壓縮後以 base64 放進 data\\history.js（只有開啟動能模組時才載入）"""
    files = sorted(f[:-5] for f in os.listdir(DAY_DIR) if f.endswith(".json"))
    files = files[-cfg.get("history_trading_days", 245):]
    days = [load_json(os.path.join(DAY_DIR, f + ".json")) for f in files]
    days = [x for x in days if x]
    if not days:
        return
    codes = sorted(set().union(*[set(x["stocks"]) for x in days]))
    pos = {c: i for i, c in enumerate(codes)}
    names, mkts, inds = stock_meta(codes, uni, days)
    add_ref(days, codes, pos)
    out_days = []
    for d in days:
        flds = [f for f in HIST_FIELDS if f != "ref"]
        fidx = [d["fields"].index(f) if f in d["fields"] else None for f in flds]
        cols = {f: [None] * len(codes) for f in HIST_FIELDS}
        for code, arr in d["stocks"].items():
            i = pos[code]
            for f, k in zip(flds, fidx):
                if k is not None and k < len(arr):
                    cols[f][i] = arr[k]
            cols["ref"][i] = d["_ref"].get(code)
        mk = d["market"]
        out_days.append({"d": d["date"], "taiex": mk.get("taiex"), "tpex": mk.get("tpex"), **cols})
    hist = {"generated": now_tw().isoformat(timespec="seconds"), "codes": codes, "names": names, "mkt": mkts, "ind": inds, "days": out_days}
    raw = json.dumps(hist, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    b64 = base64.b64encode(gzip.compress(raw, 9)).decode("ascii")
    p = os.path.join(DATA_DIR, "history.js")
    tmp = f"{p}.{os.getpid()}.tmp"          # 盤中服務也可能同時產生，暫存檔名分開
    with open(tmp, "w", encoding="ascii") as f:
        f.write('window.TWFLOW_HIST_GZ="' + b64 + '";\n')
    os.replace(tmp, p)
    log(f"已輸出 data\\history.js：{len(out_days)} 個交易日（{out_days[0]['d']}～{out_days[-1]['d']}），{len(codes)} 檔，{os.path.getsize(p) / 1e6:.1f} MB（未壓縮 {len(raw) / 1e6:.0f} MB）")


# ----------------------------------------------------------------- 基本面：月營收、季財報（公開資訊觀測站）
REV_DIR = os.path.join(RAW_DIR, "_revenue")
FIN_DIR = os.path.join(RAW_DIR, "_fin")
LOCK_PATH = os.path.join(DATA_DIR, "running.lock")
CODE_RE = re.compile(r"^\d{4}[0-9A-Z]?$")


def touch_lock():
    """執行中定期更新鎖檔時間；超過 20 分鐘沒更新就視為上次執行已經中斷"""
    try:
        if os.path.exists(LOCK_PATH):
            os.utime(LOCK_PATH, None)
    except OSError:
        pass


def html_rows(text):
    """把 HTML 表格拆成 [[儲存格文字...], ...]（含表頭列）"""
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.S | re.I):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.S | re.I)
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).replace("\xa0", " ").strip() for c in cells]
        if cells:
            rows.append(cells)
    return rows


def month_back(y, m, k):
    m2 = m - k
    while m2 <= 0:
        m2 += 12
        y -= 1
    return y, m2


def fetch_revenue(fx, cfg):
    """每月營收彙總表（上市 sii、上櫃 otc，國內外公司同一個檔）。最近 2 個月的檔案每天更新一次。"""
    os.makedirs(REV_DIR, exist_ok=True)
    today = now_tw()
    base = cfg["mops_base"]
    n = int(cfg.get("revenue_months", 26))
    for k in range(1, n + 1):
        y, m = month_back(today.year, today.month, k)
        for mk in ("sii", "otc"):
            p = os.path.join(REV_DIR, f"{y}{m:02d}_{mk}.json")
            recent = k <= 2
            if raw_exists(p) and not (recent and time.time() - raw_mtime(p) > 20 * 3600):
                continue
            touch_lock()
            url = f"{base}/nas/t21/{mk}/t21sc03_{y - 1911}_{m}_0.html"
            try:
                text = fx.get(url, referer=base + "/mops/web/index", encoding="cp950")
            except Exception as e:
                log(f"  ! 月營收 {y}/{m:02d} {mk} 抓取失敗：{e}")
                continue
            if "查無資料" in text or not re.search(r"<td[^>]*>\s*\d{4}", text):
                if not raw_exists(p):
                    log(f"  {y}/{m:02d} {'上市' if mk == 'sii' else '上櫃'}月營收尚未公布")
                continue
            save_raw(p, {"html": text})
            log(f"  {y}/{m:02d} {'上市' if mk == 'sii' else '上櫃'}月營收 ✓")


def parse_revenue(text):
    """{代號: [當月營收(千元), 月增%, 年增%, 累計年增%]}"""
    out = {}
    for c in html_rows(text):
        if len(c) >= 10 and CODE_RE.match(c[0]):
            out[c[0]] = [num(c[2]), num(c[5]), num(c[6]), num(c[9])]
    return out


def quarter_list(n):
    """最近 n 個季度（含還沒公布的本季），由新到舊：[(西元年, 季), ...]"""
    t = now_tw()
    y, q = t.year, (t.month - 1) // 3 + 1
    out = []
    for _ in range(n):
        out.append((y, q))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out


def fetch_fin(fx, cfg):
    """綜合損益表彙總（每季，累計數）。最近兩個已公布的季度在財報季每 3 天更新一次。"""
    os.makedirs(FIN_DIR, exist_ok=True)
    base = cfg["mops_base"]
    n = int(cfg.get("fin_quarters", 10)) + 1
    seen = 0
    for (y, q) in quarter_list(n):
        q_end = dt.date(y, q * 3, 28)
        if (now_tw().date() - q_end).days < 20:   # 季度剛結束，不可能有財報
            continue
        for mk in ("sii", "otc"):
            p = os.path.join(FIN_DIR, f"{y}Q{q}_{mk}.json")
            fresh_needed = seen < 2 and (now_tw().date() - q_end).days < 160 and time.time() - raw_mtime(p) > 3 * 86400
            if raw_exists(p) and not fresh_needed:
                continue
            touch_lock()
            try:
                text = fx.get(base + "/mops/web/ajax_t163sb04", referer=base + "/mops/web/t163sb04",
                              data={"encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1", "isQuery": "Y",
                                    "TYPEK": mk, "year": str(y - 1911), "season": f"{q:02d}"})
            except Exception as e:
                log(f"  ! {y}Q{q} {mk} 財報抓取失敗：{e}")
                continue
            if not re.search(r"<td[^>]*>\s*\d{4}", text):
                if not raw_exists(p):
                    log(f"  {y}Q{q} {'上市' if mk == 'sii' else '上櫃'}財報尚未公布")
                continue
            save_raw(p, {"html": text})
            log(f"  {y}Q{q} {'上市' if mk == 'sii' else '上櫃'}綜合損益表 ✓")
        if raw_exists(os.path.join(FIN_DIR, f"{y}Q{q}_sii.json")):
            seen += 1


def parse_fin(text):
    """{代號: [營業收入, 營業毛利, 營業利益, 歸屬母公司淨利, 基本EPS]}（千元、元；累計數）"""
    out = {}
    hdr = None
    for c in html_rows(text):
        key = [re.sub(r"\s+", "", x) for x in c]
        if "公司代號" in key or ("公司名稱" in key and not CODE_RE.match(key[0])):
            hdr = key
            continue
        if not hdr or not c or not CODE_RE.match(c[0]):
            continue
        def pick(*names, contains=None):
            for nm in names:
                if nm in hdr:
                    k = hdr.index(nm)
                    return num(c[k]) if k < len(c) else None
            if contains:
                for k, h_ in enumerate(hdr):
                    if all(s in h_ for s in contains):
                        return num(c[k]) if k < len(c) else None
            return None
        rev = pick("營業收入")
        gp = pick("營業毛利（毛損）淨額", "營業毛利（毛損）")
        op = pick("營業利益（損失）", "營業利益")
        ni = pick(contains=("淨利", "歸屬於母公司業主"))
        eps = pick(contains=("基本每股盈餘",))
        out[c[0]] = [rev, gp, op, ni, eps]
    return out


def fetch_fundamentals(fx, cfg):
    try:
        log("更新月營收與季財報（公開資訊觀測站）…")
        fetch_revenue(fx, cfg)
        fetch_fin(fx, cfg)
    except Exception as e:
        log(f"  ! 基本面資料更新失敗（不影響每日資料）：{e}")


def build_fund(cfg):
    """data\\fund.js：近 24 個月營收、近 8 季單季財報（gzip + base64）"""
    try:
        rev_files = sorted({f[:6] for f in os.listdir(REV_DIR)} if os.path.isdir(REV_DIR) else [])[-24:]
        months, rev = [], {}
        for ym in rev_files:
            data = {}
            for mk in ("sii", "otc"):
                j = load_json(os.path.join(REV_DIR, f"{ym}_{mk}.json"))
                if j:
                    data.update(parse_revenue(j["html"]))
            if not data:
                continue
            k = len(months)
            months.append(ym)
            for code, v in data.items():
                arr = rev.setdefault(code, [])
                arr.extend([None] * (k - len(arr)))
                arr.append([round(v[0] / 1000, 1) if v[0] is not None else None, v[1], v[2], v[3]])   # 營收換成百萬元
        for arr in rev.values():
            arr.extend([None] * (len(months) - len(arr)))
        qs = sorted({f[:6] for f in os.listdir(FIN_DIR)} if os.path.isdir(FIN_DIR) else [])   # 2026Q2
        cum = {}
        for qk in qs:
            d = {}
            for mk in ("sii", "otc"):
                j = load_json(os.path.join(FIN_DIR, f"{qk}_{mk}.json"))
                if j:
                    d.update(parse_fin(j["html"]))
            if d:
                cum[qk] = d
        quarters = sorted(cum)
        single = {}
        for qk in quarters:
            y, q = int(qk[:4]), int(qk[5])
            prev = cum.get(f"{y}Q{q - 1}") if q > 1 else None
            for code, v in cum[qk].items():
                if q == 1:
                    s = v
                elif prev and code in prev:
                    p_ = prev[code]
                    s = [None if a is None or b is None else a - b for a, b in zip(v, p_)]
                else:
                    s = [None] * 5
                single.setdefault(code, {})[qk] = s
        keep = [qk for qk in quarters if int(qk[5]) == 1 or f"{qk[:4]}Q{int(qk[5]) - 1}" in cum][-8:]
        fin = {}
        for code, m in single.items():
            row = []
            for qk in keep:
                s = m.get(qk)
                row.append(None if not s or all(x is None for x in s) else
                           [round(s[0] / 1000, 1) if s[0] is not None else None, round(s[1] / 1000, 1) if s[1] is not None else None,
                            round(s[2] / 1000, 1) if s[2] is not None else None, round(s[3] / 1000, 1) if s[3] is not None else None,
                            round(s[4], 2) if s[4] is not None else None])
            fin[code] = row
        out = {"generated": now_tw().isoformat(timespec="seconds"),
               "rev": {"m": months, "d": rev}, "q": {"q": keep, "d": fin}}
        raw = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        b64 = base64.b64encode(gzip.compress(raw, 9)).decode("ascii")
        p = os.path.join(DATA_DIR, "fund.js")
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="ascii") as f:
            f.write('window.TWFLOW_FUND_GZ="' + b64 + '";\n')
        os.replace(tmp, p)
        log(f"已輸出 data\\fund.js：月營收 {len(months)} 個月（{months[0] if months else '-'}～{months[-1] if months else '-'}）、季財報 {len(keep)} 季（{', '.join(keep)}），{os.path.getsize(p) / 1e6:.1f} MB")
    except Exception as e:
        import traceback
        log(f"  ! fund.js 產生失敗：{e}")
        log(traceback.format_exc())


# ----------------------------------------------------------------- 集保戶股權分散表（每週）
TDCC_DIR = os.path.join(RAW_DIR, "_tdcc")
# 持股分級：1＝1～999 股、2＝1～5 張、3＝5～10 張、4＝10～15、5＝15～20、6＝20～30、7＝30～40、8＝40～50、
# 9＝50～100、10＝100～200、11＝200～400、12＝400～600、13＝600～800、14＝800～1,000、15＝1,000 張以上、16＝差異數調整、17＝合計
CHIP_BUCKETS = [("le50", range(1, 9)), ("le400", range(9, 12)), ("le1000", range(12, 15)), ("big", range(15, 16))]


def parse_tdcc(text):
    """回傳 (資料日期, {代號: [≤50 張 %, 50～400 張 %, 400～1,000 張 %, 1,000 張以上 %, 股東人數, 千張大戶人數]})"""
    rows = csv.reader(text.lstrip("﻿").splitlines())
    per, date = {}, None
    for r in rows:
        if len(r) < 6 or not r[0].strip().isdigit():
            continue
        date = date or r[0].strip()
        code = r[1].strip()
        if not CODE_RE.match(code):
            continue
        try:
            lv, people, shares = int(r[2]), int(r[3]), int(r[4])
        except ValueError:
            continue
        if 1 <= lv <= 15:
            per.setdefault(code, {})[lv] = (people, shares)
    out = {}
    for code, m in per.items():
        tot = sum(s for _, s in m.values())
        if tot <= 0:
            continue
        row = [round(sum(m.get(k, (0, 0))[1] for k in ks) / tot * 100, 2) for _, ks in CHIP_BUCKETS]
        row += [sum(p for p, _ in m.values()), m.get(15, (0, 0))[0]]
        out[code] = row
    return date, out


def fetch_tdcc(fx, cfg):
    """集保的開放資料只提供最新一週（每週五晚上更新）：每次執行最多抓一次，存成 raw\\_tdcc\\日期.json.gz 累積成歷史"""
    os.makedirs(TDCC_DIR, exist_ok=True)
    mark = os.path.join(TDCC_DIR, "_checked")
    if os.path.exists(mark) and time.time() - os.path.getmtime(mark) < 6 * 3600:
        return
    try:
        text = fx.get(cfg["tdcc_url"], referer="https://www.tdcc.com.tw/")
        date, data = parse_tdcc(text)
        with open(mark, "w") as f:
            f.write(now_tw().isoformat())
        if not date or len(data) < 500:
            log(f"  ! 集保股權分散表內容不完整（{len(data)} 檔），下次再試")
            return
        p = os.path.join(TDCC_DIR, date + ".json")
        if raw_exists(p):
            log(f"  集保股權分散表 {date} 已是最新")
            return
        save_raw(p, data)
        log(f"  集保股權分散表 {date} ✓（{len(data)} 檔）")
    except Exception as e:
        log(f"  ! 集保股權分散表更新失敗（不影響其他資料）：{e}")


def build_chips(cfg):
    """data\\chips.js：每週集保股權分散（gzip + base64）"""
    try:
        files = sorted(f[:8] for f in os.listdir(TDCC_DIR) if re.match(r"^\d{8}\.json", f)) if os.path.isdir(TDCC_DIR) else []
        files = files[-int(cfg.get("chips_weeks", 52)):]
        weeks, d = [], {}
        for w in files:
            data = load_json(os.path.join(TDCC_DIR, w + ".json"))
            if not data:
                continue
            k = len(weeks)
            weeks.append(w)
            for code, row in data.items():
                arr = d.setdefault(code, [])
                arr.extend([None] * (k - len(arr)))
                arr.append(row)
        for arr in d.values():
            arr.extend([None] * (len(weeks) - len(arr)))
        out = {"generated": now_tw().isoformat(timespec="seconds"), "w": weeks,
               "f": ["le50", "le400", "le1000", "big", "people", "bigPeople"], "d": d}
        raw = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        b64 = base64.b64encode(gzip.compress(raw, 9)).decode("ascii")
        p = os.path.join(DATA_DIR, "chips.js")
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="ascii") as f:
            f.write('window.TWFLOW_CHIPS_GZ="' + b64 + '";\n')
        os.replace(tmp, p)
        if weeks:
            log(f"已輸出 data\\chips.js：集保股權分散 {len(weeks)} 週（{weeks[0]}～{weeks[-1]}），{os.path.getsize(p) / 1e6:.2f} MB")
    except Exception as e:
        log(f"  ! chips.js 產生失敗：{e}")


def sync_mobile(cfg):
    target = os.path.expandvars((cfg.get("mobile_sync_dir") or "").strip())
    if not target:
        return
    try:
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(BASE_DIR, "dashboard.html"), "r", encoding="utf-8") as f:
            page = f.read()
        with open(os.path.join(DATA_DIR, "data.js"), "r", encoding="utf-8") as f:
            js = f.read()
        tag = '<script src="data/data.js"></script>'
        if tag not in page:
            log("  ! dashboard.html 找不到資料載入標籤，略過手機版同步")
            return
        page = page.replace(tag, "<script>" + js.replace("</script", "<\\/script") + "</script>")
        hp = os.path.join(DATA_DIR, "history.js")
        if os.path.exists(hp):
            with open(hp, "r", encoding="ascii") as f:
                page = page.replace("<!--HISTORY-->", "<script>" + f.read() + "</script>")
        fp = os.path.join(DATA_DIR, "fund.js")
        if os.path.exists(fp):
            with open(fp, "r", encoding="ascii") as f:
                page = page.replace("<!--FUND-->", "<script>" + f.read() + "</script>")
        cp = os.path.join(DATA_DIR, "chips.js")
        if os.path.exists(cp):
            with open(cp, "r", encoding="ascii") as f:
                page = page.replace("<!--CHIPS-->", "<script>" + f.read() + "</script>")
        out = os.path.join(target, "twflow_dashboard.html")
        with open(out + ".tmp", "w", encoding="utf-8") as f:
            f.write(page)
        os.replace(out + ".tmp", out)
        log(f"已同步單檔版儀表板到 {out}")
    except Exception as e:
        log(f"  ! 手機版同步失敗：{e}")


# ----------------------------------------------------------------- 查詢與檢查
def show(codes, date):
    files = sorted(f[:-5] for f in os.listdir(DAY_DIR) if f.endswith(".json"))
    if not files:
        print("沒有資料")
        return
    ymd = date or files[-1]
    d = load_json(os.path.join(DAY_DIR, ymd + ".json"))
    if not d:
        print(f"沒有 {ymd} 的資料")
        return
    uni = load_json(UNIVERSE_PATH, {"stocks": {}})
    print(f"\n=== {ymd} ===")
    print("（法人買賣超、融資單位：張；證交所網站 T86 是「股」，張 × 1000 = 股）")
    hdr = ["代號", "名稱", "收盤", "漲跌", "成交量", "均價", "外資", "投信", "自營自行", "自營避險", "三大合計", "融資買進", "融資增減", "融資餘額"]
    print(" | ".join(hdr))
    for c in codes:
        a = d["stocks"].get(c)
        if not a:
            print(f"{c} 無資料")
            continue
        o, h, l, cl, chg, vol, vwap, fi, it, dsf, dsh, mb, md, mbal, val = a[:15]
        name = uni["stocks"].get(c, {}).get("name", "")
        print(" | ".join(str(x) for x in [c, name, cl, chg, vol, vwap, fi, it, dsf, dsh, fi + it + dsf + dsh, mb, md, mbal]))
        amt = lambda x: f"{x * 1000 * (vwap or 0) / 1e8:+.2f}億"
        print(f"      估算金額：外資 {amt(fi)}、投信 {amt(it)}、自營 {amt(dsf + dsh)}、合計 {amt(fi + it + dsf + dsh)}")


def check_all():
    files = sorted(f[:-5] for f in os.listdir(DAY_DIR) if f.endswith(".json"))
    print("日期 | 市場 | 項目 | 估算(億) | 官方(億) | 差異%")
    for ymd in files:
        d = load_json(os.path.join(DAY_DIR, ymd + ".json"))
        mk = d["market"]
        for m in ("TWSE", "TPEX"):
            est = mk["inst_estimated"].get(m, {})
            off = mk["inst_official"].get(m)
            if not off:
                continue
            for k, lab in (("fi", "外資"), ("it", "投信"), ("dsf", "自營自行"), ("dsh", "自營避險")):
                e, o = est.get(k, 0), off.get(k, 0)
                diff = (e - o) / abs(o) * 100 if o else 0
                print(f"{ymd} | {m} | {lab} | {e:+.2f} | {o:+.2f} | {diff:+.1f}%")


def finally_unlock(lock, rebuild):
    if not rebuild and os.path.exists(lock):
        try:
            os.remove(lock)
        except OSError:
            pass


# ----------------------------------------------------------------- 主程式
def main():
    global log
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--show")
    ap.add_argument("--date")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--refresh-universe", action="store_true")
    a = ap.parse_args()
    if a.show:
        show([c.strip() for c in a.show.split(",")], a.date)
        return
    if a.check:
        check_all()
        return
    log = Log()
    cfg = load_config()
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(DAY_DIR, exist_ok=True)
    fx = Fetcher(cfg)
    t0 = time.time()
    lock = os.path.join(DATA_DIR, "running.lock")
    if not a.rebuild and os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 20 * 60:
        log("另一個執行還在進行中（data\\running.lock），這次先跳過。若確定沒有在跑，可以刪掉這個檔案。")
        return
    if not a.rebuild:
        with open(lock, "w") as f:
            f.write(now_tw().isoformat())
    log("=" * 50)
    log("開始執行", "（只重算）" if a.rebuild else "")
    try:
        if a.rebuild:
            uni = load_json(UNIVERSE_PATH) or fetch_universe(fx, cfg)
            for f in sorted(os.listdir(RAW_DIR)):
                if not re.match(r"^\d{8}$", f):
                    continue
                d = dt.datetime.strptime(f, "%Y%m%d").date()
                build_day(d, uni)
        else:
            uni = fetch_universe(fx, cfg, force=a.refresh_universe)
            compress_old_raw()
            fetch_fundamentals(fx, cfg)
            build_fund(cfg)
            fetch_tdcc(fx, cfg)
            build_chips(cfg)
            fetch_exrights(fx, cfg, now_tw().date() - dt.timedelta(days=int(cfg.get("history_trading_days", 245) * 1.5) + 10))
            sync(cfg, fx, uni)
        build_history(cfg, uni)
        if a.rebuild:
            build_fund(cfg)
            build_chips(cfg)
        build_dashboard_data(cfg, uni)
    except KeyboardInterrupt:
        log("使用者中斷；已抓到的資料都已存檔，下次執行會接著抓。")
        finally_unlock(lock, a.rebuild)
        sys.exit(1)
    except Exception as e:
        import traceback
        log("! 發生錯誤：", e)
        log(traceback.format_exc())
        log("已抓到的資料都已存檔，下次執行會接著抓。")
        finally_unlock(lock, a.rebuild)
        sys.exit(2)
    finally_unlock(lock, a.rebuild)
    log(f"完成，共 {fx.count} 個請求，耗時 {(time.time() - t0) / 60:.1f} 分鐘")


if __name__ == "__main__":
    main()
