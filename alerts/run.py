#!/usr/bin/env python3
"""보유·관심 종목 공시 + 매크로 경보선 + 예측시장 급변 + 정책 발표 → 텔레그램 무음 알림.

    python alerts/run.py             정기 실행 (새 이벤트만 발송)
    python alerts/run.py --status    현재 경보선 현황 스냅샷 발송
    python alerts/run.py --dry-run   발송하지 않고 콘솔에만 출력

상태(alerts/state.json)는 GitHub Actions 캐시로 실행 간 유지된다.
상태가 없으면 첫 실행으로 보고, 이미 나와 있는 공시·발표는 '본 것'으로만 기록한다(폭탄 발송 방지).
표준 라이브러리만 사용한다.
"""
import email.utils
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)
STATE_PATH = os.environ.get("ALERT_STATE", os.path.join(HERE, "state.json"))
SEC_UA = os.environ.get("SEC_USER_AGENT", "")
DART_KEY = os.environ.get("DART_API_KEY", "")
# 브라우저 UA를 흉내 내면 Yahoo는 429, FRED는 응답 지연으로 막는다 → 정직한 식별 UA 사용
DEFAULT_UA = "claude-macro-telegram/1.0 (+https://github.com/tkdskak-gemini-mcp/claude-macro-telegram)"

KST = timezone(timedelta(hours=9))
NOW = datetime.now(timezone.utc)
LOOKBACK_DAYS = 3        # 이보다 오래된 공시·발표는 보지 않는다
SEEN_KEEP_DAYS = 14      # 중복 방지 기록 보존 기간
FAIL_ALERT_AFTER = 4     # 연속 실패 N회(15분 주기면 1시간)면 시스템 경고

RED, YEL, GRN, INFO = "🔴", "🟡", "🟢", "⚪"
SEV_ORDER = {RED: 0, YEL: 1, GRN: 2, INFO: 3}


# ───────────────────────── 공통 ─────────────────────────

def http(url, headers=None, timeout=30, tries=3, data=None):
    last = None
    for i in range(tries):
        try:
            hdr = {"User-Agent": DEFAULT_UA}
            hdr.update(headers or {})
            req = urllib.request.Request(url, headers=hdr, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < tries - 1:
                time.sleep(2 * (i + 1))
    raise last


def http_json(url, headers=None, timeout=30):
    return json.loads(http(url, headers, timeout))


def kst(dt):
    return dt.astimezone(KST).strftime("%m/%d %H:%M")


def kw_regex(words):
    return re.compile(r"\b(" + "|".join(re.escape(w.strip()) for w in words) + r")s?\b", re.I)


RED_KW = kw_regex(CFG["policy_keywords_red"])
ANY_KW = kw_regex(CFG["policy_keywords_red"] + CFG["policy_keywords"])
# 정례 절차 문서(버리지 않고 ⚪로 표시만 낮춘다) / 의례성 포고문(제외)
ROUTINE_KW = re.compile(r"request for comments?|requests? for (public )?comments?|notice of (public )?meeting|"
                        r"information collection|solicitation of comments|public hearing|advisory committee", re.I)
CEREMONIAL = re.compile(r"National .{0,40}(Day|Week|Month)\b|Anniversary of|in Honor of|Proclamation \d+", re.I)


class Alert:
    def __init__(self, sev, text, ask=None):
        """ask: PC의 Claude Code에 그대로 붙여넣을 분석 요청 한 줄"""
        self.sev, self.text = sev, text + (f"\n   💬 {ask}" if ask else "")


ASK = CFG.get("ask", {})


def trig(key, default="프로젝트 포트폴리오"):
    return ASK.get(key, default)


# ───────────────────────── 상태 ─────────────────────────

def load_state():
    if not os.path.exists(STATE_PATH):
        return None
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def new_state():
    return {"v": 1, "seen": {}, "levels": {}, "shocks": {}, "kalshi": {},
            "fail": {}, "fail_alerted": {}, "cik": {}, "cik_ts": 0}


def save_state(st):
    cutoff = (NOW - timedelta(days=SEEN_KEEP_DAYS)).strftime("%Y-%m-%d")
    for src, seen in st["seen"].items():
        st["seen"][src] = {k: d for k, d in seen.items() if d >= cutoff}
    st["shocks"] = {k: v for k, v in st["shocks"].items() if k.split("|")[-1] >= cutoff}
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def mark_seen(st, src, key, date_str):
    seen = st["seen"].setdefault(src, {})
    if key in seen:
        return False
    seen[key] = date_str
    return True


# ───────────────────────── 1. SEC 공시 ─────────────────────────

ITEMS_8K = {
    "1.01": (YEL, "중요 계약 체결"), "1.02": (RED, "중요 계약 해지"), "1.03": (RED, "파산·회생"),
    "1.05": (RED, "사이버보안 사고"), "2.01": (YEL, "인수·매각 완료"), "2.02": (YEL, "실적 발표"),
    "2.03": (YEL, "신규 채무"), "2.04": (RED, "채무 가속(기한이익 상실)"), "2.05": (RED, "구조조정·사업 철수"),
    "2.06": (RED, "자산 손상"), "3.01": (RED, "상장폐지·상장요건 미달 통지"), "3.02": (YEL, "비등록 주식 발행(희석)"),
    "3.03": (YEL, "주주 권리 변경"), "4.01": (RED, "감사인 교체"), "4.02": (RED, "과거 재무제표 신뢰 불가(재작성)"),
    "5.01": (RED, "지배권 변경"), "5.02": (YEL, "임원 선임·사임·보상"), "8.01": (YEL, "기타 중요사항"),
}
FORMS = {
    "10-Q": (YEL, "분기보고서(10-Q) 원문"), "10-K": (YEL, "연간보고서(10-K) 원문"),
    "20-F": (YEL, "연간보고서(20-F) 원문"), "6-K": (YEL, "해외기업 수시공시(6-K)"),
    "S-1": (YEL, "증권 발행 신고(주식=희석·채권=차입)"), "S-3": (YEL, "증권 발행 신고(주식=희석·채권=차입)"),
    "S-3ASR": (YEL, "증권 발행 신고(주식=희석·채권=차입)"), "F-3": (YEL, "증권 발행 신고(주식=희석·채권=차입)"),
    "424B1": (YEL, "증권 발행 확정(주식=희석·채권=차입)"), "424B3": (YEL, "증권 발행 확정(주식=희석·채권=차입)"),
    "424B4": (YEL, "증권 발행 확정(주식=희석·채권=차입)"), "424B5": (YEL, "증권 발행 확정(주식=희석·채권=차입)"),
    "SC 13D": (YEL, "경영참여 목적 지분(13D)"), "SC 13D/A": (YEL, "경영참여 지분 변동(13D/A)"),
    "SCHEDULE 13D": (YEL, "경영참여 목적 지분(13D)"), "SCHEDULE 13D/A": (YEL, "경영참여 지분 변동(13D/A)"),
    "SC TO-T": (RED, "공개매수"), "SC TO-I": (RED, "자사 공개매수"), "SC 14D9": (RED, "공개매수 의견표명"),
    "DEFM14A": (RED, "합병 주주총회 자료"), "PREM14A": (RED, "합병 예비 자료"), "S-4": (RED, "합병 증권신고"),
    "25-NSE": (RED, "상장폐지"), "NT 10-Q": (RED, "분기보고서 제출 지연"), "NT 10-K": (RED, "연간보고서 제출 지연"),
}


def classify_sec(form, items):
    base = form[:-2] if form.endswith("/A") and form not in FORMS else form
    if base in ("8-K", "8-K12B"):
        hits = [ITEMS_8K[i] + (i,) for i in items.split(",") if i in ITEMS_8K]
        if not hits:
            return None  # 7.01·9.01·5.07 등만 있는 8-K는 제외
        sev = RED if any(h[0] == RED for h in hits) else YEL
        label = " · ".join(f"{h[1]}({h[2]})" for h in hits)
        return sev, ("[정정] " if form.endswith("/A") else "") + label
    if form in FORMS:
        return FORMS[form]
    return None


SCORE_FORMS_HOLD = {"10-Q", "10-K", "20-F", "6-K"}   # 보유 종목: 실적 8-K(2.02)도 채점
SCORE_FORMS_WATCH = {"10-Q", "10-K", "20-F"}         # 관심 종목: 분기·연간 보고서만


def wants_score(ticker, form, items):
    if ticker in CFG["us_holdings"]:
        return form in SCORE_FORMS_HOLD or (form == "8-K" and "2.02" in items.split(","))
    return ticker in CFG["us_watch"] and form in SCORE_FORMS_WATCH


def cik_map(st):
    if st["cik"] and time.time() - st.get("cik_ts", 0) < 7 * 86400:
        return st["cik"]
    raw = http_json("https://www.sec.gov/files/company_tickers.json", {"User-Agent": SEC_UA})
    st["cik"] = {v["ticker"]: v["cik_str"] for v in raw.values()}
    st["cik_ts"] = time.time()
    return st["cik"]


def src_sec(st, seed):
    if not SEC_UA:
        raise RuntimeError("SEC_USER_AGENT 시크릿 없음")
    out = []
    mapping = cik_map(st)
    cutoff = (NOW - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    universe = [(t, "보유: " + "·".join(l)) for t, l in CFG["us_holdings"].items()]
    universe += [(t, "관심종목") for t in CFG["us_watch"] if t not in CFG["us_holdings"]]
    errors = []
    for ticker, owner in universe:
        cik = CFG["cik_override"].get(ticker) or mapping.get(ticker)
        if not cik:
            errors.append(f"{ticker}: CIK 없음")
            continue
        try:
            d = http_json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", {"User-Agent": SEC_UA})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ticker}: {e}")
            continue
        r = d["filings"]["recent"]
        for i in range(len(r["form"])):
            fdate = r["filingDate"][i]
            if fdate < cutoff:
                break
            acc = r["accessionNumber"][i]
            if not mark_seen(st, "sec", acc, fdate) or seed:
                continue
            cls = classify_sec(r["form"][i], r["items"][i])
            if not cls:
                continue
            sev, label = cls
            try:
                t = datetime.fromisoformat(r["acceptanceDateTime"][i].replace("Z", "+00:00"))
                when = kst(t) + " KST"
            except ValueError:
                when = fdate
            doc = r["primaryDocument"][i]
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
            scoring = wants_score(ticker, r["form"][i], r["items"][i])
            if scoring:
                st.setdefault("score_queue", []).append(
                    {"ticker": ticker, "cik": int(cik), "acc": acc, "form": r["form"][i], "items": r["items"][i],
                     "doc": doc, "url": url, "owner": owner, "filed": fdate, "tries": 0})
            tail = "\n   🧾 Claude 원문 채점 대기열 등록 (수 분~수십 분 뒤 별도 메시지)" if scoring else ""
            out.append(Alert(sev, f"{sev} [공시·SEC] {ticker} {r['form'][i]} — {label}\n   {owner} · {when}{tail}",
                             f"{ticker} {r['form'][i]} 공시({label}) 원문 검토 — 내 thesis 영향 판정해줘: {url}"))
        time.sleep(0.12)  # SEC 초당 10회 제한 준수
    if errors and len(errors) > len(universe) // 2:
        raise RuntimeError("; ".join(errors[:3]))
    return out


# ───────────────────────── 2. DART 공시 ─────────────────────────

def src_dart(st, seed):
    if not DART_KEY:
        raise RuntimeError("DART_API_KEY 시크릿 없음")
    out = []
    bgn = (NOW.astimezone(KST) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    for corp, info in CFG["kr_holdings"].items():
        q = urllib.parse.urlencode({"crtfc_key": DART_KEY, "corp_code": corp, "bgn_de": bgn, "page_count": 100})
        d = http_json("https://opendart.fss.or.kr/api/list.json?" + q)
        if d.get("status") == "013":  # 조회 결과 없음
            continue
        if d.get("status") != "000":
            raise RuntimeError(f"DART {d.get('status')} {d.get('message')}")
        for x in d.get("list", []):
            rno, name = x["rcept_no"], re.sub(r"\s+", " ", x["report_nm"]).strip()
            rdate = f"{x['rcept_dt'][:4]}-{x['rcept_dt'][4:6]}-{x['rcept_dt'][6:]}"
            if not mark_seen(st, "dart", rno, rdate) or seed:
                continue
            if any(k in name for k in CFG["dart_skip"]):
                continue
            sev = RED if any(k in name for k in CFG["dart_red"]) else YEL
            out.append(Alert(sev, f"{sev} [공시·DART] {info['name']} — {name}\n"
                                  f"   보유: {'·'.join(info['labels'])} · {rdate} · 제출 {x.get('flr_nm', '')}",
                             f"{info['name']} DART 공시 「{name}」 검토 — 내 보유 영향 판정해줘: https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}"))
    return out


# ───────────────────────── 3. 매크로 경보선·급변 ─────────────────────────

_YCACHE = {}


def yahoo(sym):
    if sym in _YCACHE:
        if isinstance(_YCACHE[sym], Exception):
            raise _YCACHE[sym]
        return _YCACHE[sym]
    q = urllib.parse.quote(sym)
    last = None
    for host in ("query1", "query2"):
        try:
            d = http_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{q}?range=1y&interval=1d")
            res = d["chart"]["result"][0]
            break
        except Exception as e:  # noqa: BLE001
            last = e
    else:
        _YCACHE[sym] = RuntimeError(f"Yahoo {sym}: {last}")
        raise _YCACHE[sym]
    m = res["meta"]
    off = timedelta(seconds=m.get("gmtoffset", 0))
    mtime = datetime.fromtimestamp(m["regularMarketTime"], timezone.utc)
    bar_date = (mtime + off).strftime("%Y-%m-%d")
    q0 = res["indicators"]["quote"][0]
    closes, vols = q0["close"], (q0.get("volume") or [])
    prev = None
    hist_c, hist_v, today_v = [], [], None
    for i, (ts, c) in enumerate(zip(res["timestamp"], closes)):
        if c is None:
            continue
        d = (datetime.fromtimestamp(ts, timezone.utc) + off).strftime("%Y-%m-%d")
        v = vols[i] if i < len(vols) else None
        if d < bar_date:
            prev = c
            hist_c.append(c)
            if v:
                hist_v.append(v)
        elif d == bar_date and v:
            today_v = v
    out = {"price": m["regularMarketPrice"], "prev": prev, "high": m.get("fiftyTwoWeekHigh"),
           "date": bar_date, "stale": (NOW - mtime) > timedelta(days=5),
           "vol": today_v,
           "vol_med": statistics.median(hist_v[-40:]) if len(hist_v) >= 20 else None,
           "ma50": (sum(hist_c[-50:]) / 50) if len(hist_c) >= 50 else None}
    _YCACHE[sym] = out
    return out


def level_value(rule):
    y = yahoo(rule["sym"])
    if y["stale"]:
        return None
    kind = rule.get("kind")
    if kind == "drawdown":
        return (y["price"] / y["high"] - 1) * 100
    if kind == "spread":
        y2 = yahoo(rule["sym2"])
        return None if y2["stale"] else y["price"] - y2["price"]
    if kind == "volratio":  # 당일 거래량 ÷ 직전 40거래일 중앙값 (전쟁-운임 플레이북 1순위)
        return None if not (y.get("vol") and y.get("vol_med")) else y["vol"] / y["vol_med"]
    if kind == "ma50gap":   # 50일선 대비 %
        return None if not y.get("ma50") else (y["price"] / y["ma50"] - 1) * 100
    return y["price"]


def level_step(st, key, val, op, v, hyst):
    """경보선 상태 전이. 반환: 'on' / 'off' / None"""
    was = st["levels"].get(key, False)
    hit = val >= v if op == ">=" else val <= v
    cleared = val < v - hyst if op == ">=" else val > v + hyst
    if not was and hit:
        st["levels"][key] = True
        return "on"
    if was and cleared:
        st["levels"][key] = False
        return "off"
    return None


def combo_step(st, combo, vals):
    """복합 경보: 모든 조건이 넘으면 점등, 하나라도 히스테리시스 밖으로 풀리면 해제."""
    was = st["levels"].get(combo["id"], False)
    parts = combo["parts"]
    hit = all((v >= pt["v"]) if pt["op"] == ">=" else (v <= pt["v"]) for pt, v in zip(parts, vals))
    cleared = any((v < pt["v"] - pt["hyst"]) if pt["op"] == ">=" else (v > pt["v"] + pt["hyst"])
                  for pt, v in zip(parts, vals))
    if not was and hit:
        st["levels"][combo["id"]] = True
        return "on"
    if was and cleared:
        st["levels"][combo["id"]] = False
        return "off"
    return None


def src_macro(st, seed):
    out = []
    for rule in CFG["macro_levels"]:
        val = level_value(rule)
        if val is None:
            continue
        ev = level_step(st, rule["id"], val, rule["op"], rule["v"], rule["hyst"])
        if ev and not seed:
            shown, line = rule["fmt"].format(val), rule["fmt"].format(rule["v"])
            ask = f"{trig(rule['id'])} — {rule['name']} {shown} 경보선 {'점등' if ev == 'on' else '해제'}({rule['why'].split(' — ')[0]}), 대응 판정해줘"
            if ev == "on":
                out.append(Alert(RED, f"{RED} [매크로·경보선 점등] {rule['name']} {shown} (기준 {rule['op']} {line})\n   {rule['why']}", ask))
            else:
                out.append(Alert(GRN, f"{GRN} [매크로·경보선 해제] {rule['name']} {shown} (기준 {rule['op']} {line})\n   {rule['why']}", ask))
    for combo in CFG.get("rate_regimes", {}).get("combo", []):
        ys = [yahoo(pt["sym"]) for pt in combo["parts"]]
        if any(y["stale"] for y in ys):
            continue
        ev = combo_step(st, combo, [y["price"] for y in ys])
        if ev and not seed:
            vals = " · ".join(f"{pt['label']} {pt['fmt'].format(y['price'])}(기준 {pt['op']} {pt['fmt'].format(pt['v'])})"
                              for pt, y in zip(combo["parts"], ys))
            word, sev = ("점등", RED) if ev == "on" else ("해제", GRN)
            out.append(Alert(sev, f"{sev} [매크로·2단계 경보 {word}] {combo['name']}\n   {vals}\n   {combo['why']}",
                             f"{trig(combo['id'])} — {combo['name']} 2단계 경보 {word}({vals}), 대응 판정해줘"))
    for rule in CFG["macro_shocks"]:
        y = yahoo(rule["sym"])
        if y["stale"] or not y["prev"]:
            continue
        chg = y["price"] - y["prev"]
        pct = chg / y["prev"] * 100
        mag = abs(chg) if rule["kind"] == "abs" else abs(pct)
        if mag < rule["v"]:
            continue
        key = f"{rule['sym']}|{y['date']}"
        done = st["shocks"].get(key, 0)
        if done and mag < done + rule["v"]:  # 같은 날은 한 단계 더 커질 때만 재알림
            continue
        st["shocks"][key] = mag
        if seed:
            continue
        arrow = "▲" if chg > 0 else "▼"
        move = f"{chg:+.3f}%p" if rule["kind"] == "abs" else f"{pct:+.2f}%"
        out.append(Alert(YEL, f"⚡ [매크로·급변] {rule['name']} {arrow} {move} → {rule['fmt'].format(y['price'])}\n"
                              f"   하루 변동 기준 ±{rule['v']}{rule.get('unit', '%')} 초과 · {y['date']}",
                         f"{trig(rule['sym'])} — {rule['name']} 하루 {move} 급변, 원인과 내 포트 영향 분석해줘"))
    return out


# ───────────────────────── 4. 크레딧 (FRED) ─────────────────────────

def fred_last2(series):
    rows = http(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}").decode().strip().splitlines()[1:]
    vals = [(d, float(v)) for d, v in (r.split(",") for r in rows) if v not in (".", "")]
    return vals[-2], vals[-1]


def fred_series(series):
    rows = http(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}").decode().strip().splitlines()[1:]
    return {d: float(v) for d, v in (r.split(",") for r in rows) if v not in (".", "")}


def steepener_value():
    """설정한 거래일 수 동안의 10Y 변화(bp)와 2s10s 변화(bp). 두 시리즈가 모두 있는 날짜만 쓴다."""
    s = CFG["rate_regimes"]["steepener"]
    t10, t2 = fred_series("DGS10"), fred_series("DGS2")
    days = sorted(set(t10) & set(t2))[-(s["days"] + 1):]
    if len(days) < s["days"] + 1:
        raise RuntimeError(f"DGS10·DGS2 공통 관측일 부족({len(days)})")
    d0, d1 = days[0], days[-1]
    return {"d0": d0, "d1": d1, "t10": t10[d1], "t2": t2[d1],
            "d10": (t10[d1] - t10[d0]) * 100,
            "sp": (t10[d1] - t2[d1]) * 100,
            "dsp": ((t10[d1] - t2[d1]) - (t10[d0] - t2[d0])) * 100}


def src_steepener(st, seed):
    s = CFG.get("rate_regimes", {}).get("steepener")
    if not s:
        return []
    v = steepener_value()
    was = st["levels"].get(s["id"], False)
    hit = v["d10"] >= s["tnx_bp"] and v["dsp"] >= s["spread_bp"]
    cleared = v["d10"] < s["tnx_bp"] / 2 or v["dsp"] < 0
    ev = None
    if not was and hit:
        st["levels"][s["id"]], ev = True, "on"
    elif was and cleared:
        st["levels"][s["id"]], ev = False, "off"
    if not ev or seed:
        return []
    word, sev = ("점등", RED) if ev == "on" else ("해제", GRN)
    body = (f"10Y {v['t10']:.2f}% ({v['d10']:+.0f}bp) · 2Y {v['t2']:.2f}% · 2s10s {v['sp']:+.0f}bp ({v['dsp']:+.0f}bp)"
            f" · {v['d0']}→{v['d1']} ({s['days']}거래일)")
    return [Alert(sev, f"{sev} [매크로·2단계 경보 {word}] 약세 스티프닝(2s10s)\n   {body}\n"
                       f"   기준: 10Y {s['tnx_bp']}bp↑ 그리고 2s10s {s['spread_bp']}bp↑\n   {s['why']}",
                  f"{trig(s['id'])} — 약세 스티프닝 2단계 경보 {word}({body}), 대응 판정해줘")]


def src_credit(st, seed):
    c = CFG["credit"]
    (hd0, hy0), (hd1, hy1) = fred_last2("BAMLH0A0HYM2")
    (_, ccc0), (cd1, ccc1) = fred_last2("BAMLH0A3HYC")
    out = []
    ratio = ccc1 / hy1
    ev = level_step(st, "ccc_hy", ratio, ">=", c["ratio_level"], c["ratio_hyst"])
    if ev and not seed:
        sev, word = (RED, "점등") if ev == "on" else (GRN, "해제")
        out.append(Alert(sev, f"{sev} [크레딧·경보선 {word}] CCC÷HY {ratio:.2f}배 (기준 {c['ratio_level']}배) · CCC {ccc1:.2f}% / HY {hy1:.2f}%\n   {c['why_ratio']}",
                         f"프로젝트 크레딧 — CCC÷HY {ratio:.2f}배 경보선 {word}, 헷지·SGOV 트랜치 대응 판정해줘"))
    for name, d0, v0, d1, v1, lim in (("HY OAS", hd0, hy0, hd1, hy1, c["hy_shock_bp"]),
                                      ("CCC OAS", None, ccc0, cd1, ccc1, c.get("ccc_shock_bp", 40))):
        bp = (v1 - v0) * 100
        key = f"fred-{name}|{d1}"
        if bp >= lim and key not in st["shocks"]:
            st["shocks"][key] = bp
            if not seed:
                out.append(Alert(YEL, f"⚡ [크레딧·급변] {name} {v0:.2f}% → {v1:.2f}% (+{bp:.0f}bp 하루) · {d1}\n   스프레드 급확대 — SGOV STOP 조건(신용스트레스) 점검",
                                 f"프로젝트 크레딧 — {name} 하루 +{bp:.0f}bp 급확대, 원인과 SGOV STOP 조건 판정해줘"))
    return out


# ───────────────────────── 5. 예측시장 (Kalshi) ─────────────────────────

def kalshi_markets(series):
    d = http_json(f"https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker={series}&status=open&limit=400")
    return d.get("markets", [])


def kalshi_prob(m):
    f = lambda k: float(m.get(k) or 0)  # noqa: E731
    bid, ask, last = f("yes_bid_dollars"), f("yes_ask_dollars"), f("last_price_dollars")
    if bid > 0 and ask > 0 and ask >= bid:
        if ask - bid > 0.10:
            return None  # 호가가 너무 벌어져 신뢰 불가
        return (bid + ask) / 2 * 100
    return last * 100 if last > 0 else None


def src_kalshi(st, seed):
    k = CFG["kalshi"]
    out, now = [], time.time()
    for series, label in k["series"].items():
        for m in kalshi_markets(series):
            oi = float(m.get("open_interest_fp") or m.get("open_interest") or 0)
            if oi < k["min_oi"]:
                continue
            p = kalshi_prob(m)
            if p is None:
                continue
            tk = m["ticker"]
            base = st["kalshi"].get(tk)
            if not base or now - base[1] > 86400:
                st["kalshi"][tk] = [p, now]
                continue
            b = base[0]
            big = abs(p - b) >= k["move_pp"] or (b < 10 and p >= max(b * 3, b + 5))
            if not big:
                continue
            st["kalshi"][tk] = [p, now]
            if seed:
                continue
            grade = "A" if oi >= 100000 else "B"
            sub = m.get("yes_sub_title") or m.get("subtitle") or tk
            hrs = (now - base[1]) / 3600
            out.append(Alert(YEL, f"🎲 [예측시장·{grade}] {label} — {sub}\n   {b:.1f}% → {p:.1f}% ({p - b:+.1f}%p, {hrs:.0f}시간 내) · OI {oi:,.0f} · {tk}",
                             f"{trig(series)} — Kalshi {label}({sub}) {b:.0f}%→{p:.0f}% 급변, 원인과 대응 판정해줘"))
    # 만기된 마켓 정리
    st["kalshi"] = {t: v for t, v in st["kalshi"].items() if now - v[1] < 7 * 86400}
    return out


# ───────────────────────── 6. 정책 (백악관·연방관보) ─────────────────────────

def src_whitehouse(st, seed):
    raw = http("https://www.whitehouse.gov/presidential-actions/feed/")
    root = ET.fromstring(raw)
    out = []
    for it in root.iter("item"):
        link, title = it.findtext("link") or "", (it.findtext("title") or "").strip()
        try:
            pub = email.utils.parsedate_to_datetime(it.findtext("pubDate"))
        except (TypeError, ValueError):
            continue
        if NOW - pub > timedelta(days=LOOKBACK_DAYS):
            continue
        if not mark_seen(st, "wh", link, pub.strftime("%Y-%m-%d")) or seed:
            continue
        if not ANY_KW.search(title):
            continue
        if CEREMONIAL.search(title):   # 기념일 포고문 등 의례성 문서
            continue
        cats = [c.text for c in it.findall("category") if c.text and c.text != "Presidential Actions"]
        sev = RED if RED_KW.search(title) else YEL
        out.append(Alert(sev, f"{sev} [정책·백악관] {' / '.join(cats) or '대통령 조치'} · {kst(pub)} KST\n   {title[:180]}",
                         f"이 대통령 조치가 내 전 계좌 보유종목에 미치는 영향 분석해줘: {link}"))
    return out


def src_fedreg(st, seed):
    d = http_json("https://www.federalregister.gov/api/v1/public-inspection-documents/current.json")
    out = []
    for doc in d.get("results", []):
        agencies = [a.get("name", "") for a in doc.get("agencies", [])]
        if not any(a in CFG["fedreg_agencies"] for a in agencies):
            continue
        num = doc.get("document_number") or doc.get("html_url")
        if not mark_seen(st, "fr", num, NOW.strftime("%Y-%m-%d")) or seed:
            continue
        title = (doc.get("title") or "").strip()
        who = "상무부 BIS(수출통제)" if "Industry and Security Bureau" in agencies else "USTR(무역대표부)"
        link = doc.get("html_url") or doc.get("pdf_url", "")
        routine = ROUTINE_KW.search(title) and doc.get("type") == "Notice"
        # 정례 절차(의견요청·설명회·자료수집)는 버리지 않고 ⚪로 낮춰 메시지 아래쪽에 모은다
        sev = INFO if routine else (RED if RED_KW.search(title) else YEL)
        kind = "의견요청·정례" if routine else doc.get("type", "")
        out.append(Alert(sev, f"{sev} [정책·연방관보 공개열람] {who} · {kind}\n   {title[:180]}",
                         f"이 {who} 문서가 내 보유종목(반도체 등)에 미치는 영향 분석해줘: {link}"))
    return out


# ───────────────────────── 7. 판정일 캘린더 ─────────────────────────

APPROX_DAY = {"초": 1, "중": 10, "말": 20}


def load_thesis():
    p = os.path.join(HERE, "thesis.json")
    if not os.path.exists(p):
        return {"tickers": {}, "macro_events": []}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def event_day(ev):
    """확정 날짜 또는 '2026-12-09경'·'2026-10-21 전후'처럼 일자가 있는 대략 일정의 날짜."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", ev.get("date") or ev.get("approx") or "")
    return datetime(int(m[1]), int(m[2]), int(m[3])).date() if m else None


def remind_date(ev):
    """일자가 있으면 전날, '2026-11 초'·'2026-11' 같은 월 단위 일정은 그 달 1·10·20일에 알린다.
    '2026 하반기'처럼 월이 없는 일정은 알리지 않는다."""
    d = event_day(ev)
    if d:
        return d - timedelta(days=1)
    m = re.match(r"(\d{4})-(\d{2})\s*(초|중|중순|말)?", ev.get("approx") or "")
    return datetime(int(m[1]), int(m[2]), APPROX_DAY.get((m[3] or "초")[0], 1)).date() if m else None


def owner_of(ticker):
    if not ticker:
        return "매크로"
    if ticker in CFG["us_holdings"]:
        return "보유: " + "·".join(CFG["us_holdings"][ticker])
    return "관심종목"


def src_calendar(st, seed):  # noqa: ARG001 — 일정 알림은 과거 이벤트가 아니므로 초기화 억제 없음
    now_kst = NOW.astimezone(KST)
    if now_kst.hour < 8:
        return []
    th, today, out = load_thesis(), now_kst.date(), []
    items = [(t, ev) for t, v in th.get("tickers", {}).items() for ev in v.get("events", [])]
    items += [(None, ev) for ev in th.get("macro_events", [])]
    for t, ev in items:
        rd = remind_date(ev)
        if not rd or not (rd <= today <= rd + timedelta(days=1)):  # 하루 놓쳐도 다음날까지 보냄
            continue
        key = f"{t}|{ev.get('date') or ev.get('approx')}|{ev.get('title')}"
        if not mark_seen(st, "cal", key, today.isoformat()):
            continue
        d = event_day(ev)
        if d:
            when = {1: "내일", 0: "오늘"}.get((d - today).days, "") + f"({d:%m/%d})"
            if not ev.get("date"):
                when += f" · {ev['approx']}"
        else:
            when = f"{ev['approx']} 예정"
        checks = ev.get("checks") or (th["tickers"].get(t, {}).get("checks", []) if t else [])
        body = "".join(f"\n    {'①②③④⑤⑥'[i]} {c}" for i, c in enumerate(checks[:6]))
        name = f"{t} " if t else ""
        out.append(Alert(YEL, f"📅 [판정일] {name}{ev.get('title', '')} — {when}\n   {owner_of(t)}"
                              + (f"\n   볼 것:{body}" if body else ""),
                         f"{name}{ev.get('title', '')} 결과 확인 — 볼 것 체크리스트로 판정해줘"))
    return out


# ───────────────────────── 현황 스냅샷 ─────────────────────────

def status_lines(st):
    lines = ["[경보선 현황]"]
    groups = {}  # 같은 지표(VIX 25·30·35 등)는 한 줄로
    for rule in CFG["macro_levels"]:
        groups.setdefault(rule["name"], []).append(rule)
    for name, rules in groups.items():
        try:
            val = level_value(rules[0])
        except Exception as e:  # noqa: BLE001
            lines.append(f"  ? {name}: 조회 실패 ({str(e)[:80]})")
            continue
        if val is None:
            lines.append(f"  ? {name}: 시세가 오래됨")
            continue
        fmt = rules[0]["fmt"]
        on = [r for r in rules if st["levels"].get(r["id"], False)]
        marks = " · ".join(("●" if r in on else "") + fmt.format(r["v"]) for r in rules)
        lines.append(f"  {RED if on else GRN} {name} {fmt.format(val)}  (기준 {rules[0]['op']} {marks})")
    try:
        (_, _), (_, hy) = fred_last2("BAMLH0A0HYM2")
        (_, _), (d, ccc) = fred_last2("BAMLH0A3HYC")
        on = st["levels"].get("ccc_hy", False)
        lines.append(f"  {RED if on else GRN} CCC÷HY {ccc / hy:.2f}배  (기준 >= {CFG['credit']['ratio_level']}배 · {d})")
    except Exception as e:  # noqa: BLE001
        lines.append(f"  ? 크레딧: 조회 실패 ({e})")
    rr = CFG.get("rate_regimes", {})
    for combo in rr.get("combo", []):
        try:
            ys = [yahoo(pt["sym"]) for pt in combo["parts"]]
            on = st["levels"].get(combo["id"], False)
            vals = " · ".join(f"{pt['label']} {pt['fmt'].format(y['price'])}/{pt['fmt'].format(pt['v'])}"
                              for pt, y in zip(combo["parts"], ys))
            lines.append(f"  {RED if on else GRN} {combo['name']}  ({vals})")
        except Exception as e:  # noqa: BLE001
            lines.append(f"  ? {combo['name']}: 조회 실패 ({str(e)[:80]})")
    if rr.get("steepener"):
        s = rr["steepener"]
        try:
            v = steepener_value()
            on = st["levels"].get(s["id"], False)
            lines.append(f"  {RED if on else GRN} 약세 스티프닝  (10Y {v['d10']:+.0f}bp/{s['tnx_bp']} · "
                         f"2s10s {v['sp']:+.0f}bp, 변화 {v['dsp']:+.0f}bp/{s['spread_bp']} · {s['days']}거래일~{v['d1']})")
        except Exception as e:  # noqa: BLE001
            lines.append(f"  ? 약세 스티프닝: 조회 실패 ({str(e)[:80]})")
    n_us = len(CFG["us_holdings"]) + len([t for t in CFG["us_watch"] if t not in CFG["us_holdings"]])
    lines += ["", f"[감시 대상] 미국 {n_us}종(보유 {len(CFG['us_holdings'])}·관심 {n_us - len(CFG['us_holdings'])}) · "
                  f"한국 {len(CFG['kr_holdings'])}종 · 예측시장 {len(CFG['kalshi']['series'])}개 시리즈 · 백악관·연방관보"]
    off = [s for s, n in st["fail"].items() if n]
    if off:
        lines.append(f"[수집 실패 중] {', '.join(off)}")
    return lines


# ───────────────────────── 발송 ─────────────────────────

def send(text, dry):
    if dry:
        print(text)
        print("-" * 40)
        return
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > 3500 and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{block}" if cur else block
    chunks.append(cur)
    for ch in chunks:
        body = json.dumps({"chat_id": chat, "text": ch, "disable_web_page_preview": True,
                           "disable_notification": True}).encode()
        r = json.loads(http(f"https://api.telegram.org/bot{token}/sendMessage",
                            {"Content-Type": "application/json"}, data=body))
        if not r.get("ok"):
            raise RuntimeError(f"Telegram: {r}")


BASE_SOURCES = [("SEC 공시", src_sec), ("DART 공시", src_dart), ("매크로 시세", src_macro),
           ("크레딧(FRED)", src_credit), ("금리 레짐(FRED)", src_steepener), ("예측시장(Kalshi)", src_kalshi),
           ("백악관 발표", src_whitehouse), ("연방관보", src_fedreg), ("판정일 캘린더", src_calendar)]


def sources():
    """뉴스 모듈은 실행 시점에 불러오고 core를 주입한다(순환 참조 방지)."""
    import news

    news.core = sys.modules[__name__]
    return BASE_SOURCES + [("뉴스·논문", news.collect)]


def queue_test(st, ticker):
    """수동 시험: 해당 종목의 가장 최근 실적 공시(10-Q·10-K·실적 8-K)를 채점 대기열에 넣는다."""
    ticker = ticker.upper()
    cik = CFG["cik_override"].get(ticker) or cik_map(st).get(ticker)
    r = http_json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", {"User-Agent": SEC_UA})["filings"]["recent"]
    for i in range(len(r["form"])):
        form, items = r["form"][i], r["items"][i]
        if form in ("10-Q", "10-K", "20-F") or (form == "8-K" and "2.02" in items.split(",")):
            acc, doc = r["accessionNumber"][i], r["primaryDocument"][i]
            st.setdefault("score_queue", []).append(
                {"ticker": ticker, "cik": int(cik), "acc": acc, "form": form, "items": items, "doc": doc,
                 "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}",
                 "owner": owner_of(ticker) + " · 시험 실행", "filed": r["filingDate"][i], "tries": 0})
            print(f"test queued: {ticker} {form} {acc}")
            return
    print(f"test: {ticker} 실적 공시 없음")


def main():
    dry = "--dry-run" in sys.argv
    st = load_state()
    seed = st is None
    st = st or new_state()
    test = os.environ.get("TEST_SCORE_TICKER", "").strip()
    if test and not seed:
        queue_test(st, test)

    alerts, errs = [], []
    seeded = st.setdefault("seeded", {})
    for name, fn in sources():
        try:
            # 소스별 첫 성공 실행은 기록만 한다 (시크릿이 나중에 추가돼도 과거 공시가 몰려오지 않게)
            alerts += fn(st, seed or not seeded.get(name))
            seeded[name] = True
            if st["fail_alerted"].get(name):
                alerts.append(Alert(GRN, f"{GRN} [시스템] {name} 수집 복구"))
            st["fail"][name], st["fail_alerted"][name] = 0, False
        except Exception as e:  # noqa: BLE001
            errs.append(f"{name}: {e}")
            n = st["fail"].get(name, 0) + 1
            st["fail"][name] = n
            if n >= FAIL_ALERT_AFTER and not st["fail_alerted"].get(name):
                st["fail_alerted"][name] = True
                alerts.append(Alert(INFO, f"⚠️ [시스템] {name} 수집이 {n}회 연속 실패\n   {str(e)[:200]}"))
    for e in errs:
        print("ERR", e, file=sys.stderr)

    stamp = NOW.astimezone(KST).strftime("%m/%d %H:%M")
    if seed or "--status" in sys.argv:
        head = "✅ 이벤트 알림 가동 시작" if seed else "📋 경보선 현황"
        extra = ["", "이미 나와 있던 공시·발표는 건너뛰고, 지금부터 새로 생기는 이벤트만 보냅니다."] if seed else []
        errs_txt = ["", "[이번 실행 수집 오류]"] + [f"  {e[:150]}" for e in errs] if errs else []
        send("\n".join([f"{head} · {stamp} KST", ""] + status_lines(st) + extra + errs_txt), dry)
    if alerts:
        alerts.sort(key=lambda a: SEV_ORDER.get(a.sev, 9))
        send(f"📡 이벤트 알림 · {stamp} KST ({len(alerts)}건)\n\n" + "\n\n".join(a.text for a in alerts), dry)

    save_state(st)
    queued = len(st.get("score_queue", []))
    due = st.get("news_digest_due")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            if queued:
                f.write("score=true\n")
            if due:
                f.write("news=true\n")
    print(f"done: alerts={len(alerts)} errors={len(errs)} seed={seed} score_queue={queued} digest_due={due}")


if __name__ == "__main__":
    main()
