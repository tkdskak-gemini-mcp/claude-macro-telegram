#!/usr/bin/env python3
"""채점 대기열(state.json의 score_queue)의 실적 공시를 Claude가 원문으로 채점해 텔레그램으로 보낸다.

    python alerts/score.py                  대기열 처리 (한 번에 최대 MAX_JOBS건)
    python alerts/score.py --dry-run        발송 없이 출력
    python alerts/score.py --prepare-only   원문·XBRL만 준비하고 Claude는 부르지 않음 (디버그)

원문은 파이썬이 SEC에서 받아 텍스트 파일로 만들고, Claude는 그 파일만 읽는다(웹 접근 불필요).
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run as core  # noqa: E402

MAX_JOBS = 2            # 실행 1회당 채점 건수 (실적 시즌 몰림 분산)
MAX_TRIES = 3           # 실패 재시도 횟수
MAX_CHARS = 600_000     # 원문 텍스트 상한
CLAUDE_TIMEOUT = 900    # 초
REPO = os.path.dirname(core.HERE)
SEC_HDR = {"User-Agent": core.SEC_UA}

KEEP_TYPES = re.compile(r"^(8-K|10-Q|10-K|20-F|6-K|EX-99(\.\d+)?)$")
EARNINGS_6K = re.compile(r"(revenue|quarter|three months|financial results)", re.I)


# ───────────────────────── 원문 준비 ─────────────────────────

def html_to_text(raw):
    s = re.sub(r"(?is)<(script|style|ix:header)[^>]*>.*?</\1>", " ", raw)
    s = re.sub(r"(?i)</t[dh]>", " | ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"( ?\| ?){2,}", " | ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def filing_documents(job):
    """공시 목차에서 본문과 EX-99(보도자료)만 골라 텍스트로 반환."""
    acc_nd = job["acc"].replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{job['cik']}/{acc_nd}/"
    idx = core.http(base + f"{job['acc']}-index.htm", SEC_HDR).decode("utf-8", "replace")
    table = re.search(r"<table[^>]*tableFile[^>]*>(.*?)</table>", idx, re.S)
    docs = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1) if table else "", re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        href = re.search(r'href="([^"]+)"', row)
        if len(cells) >= 4 and href and KEEP_TYPES.match(cells[3]):
            docs.append((cells[3], "https://www.sec.gov" + href.group(1).replace("/ix?doc=", "")))
    if not docs:  # 목차 파싱 실패 시 주 문서만
        docs = [(job["form"], job["url"])]
    parts = []
    for typ, url in docs:
        text = html_to_text(core.http(url, SEC_HDR).decode("utf-8", "replace"))
        parts.append(f"\n\n===== [{typ}] {url} =====\n\n{text}")
    return "".join(parts)[:MAX_CHARS]


REV_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax"]
LINES = [("매출", REV_TAGS), ("매출총이익", ["GrossProfit"]), ("영업이익", ["OperatingIncomeLoss"]),
         ("순이익", ["NetIncomeLoss"])]


def _days(u):
    return (datetime.fromisoformat(u["end"]) - datetime.fromisoformat(u["start"])).days


def quarterly(facts, tags):
    """XBRL companyfacts에서 분기(3개월) 값을 {분기말일: 값}으로.
    10-K에는 4분기 3개월 값이 없으므로 '연간 − 같은 기초일의 9개월 누계'로 복원한다(결산월 무관).
    여러 태그 중 가장 최근 분기가 있는 것을 쓴다."""
    best = {}
    for tag in tags:
        units = [u for u in facts.get("us-gaap", {}).get(tag, {}).get("units", {}).get("USD", [])
                 if u.get("start") and u.get("end")]
        q = {u["end"]: u["val"] for u in units if 80 <= _days(u) <= 100}
        ytd9 = {(u["start"], u["end"]): u["val"] for u in units if 260 <= _days(u) <= 285}
        for u in units:
            if 350 <= _days(u) <= 380 and u["end"] not in q:
                nine = [v for (s, e), v in ytd9.items() if s == u["start"]
                        and 80 <= (datetime.fromisoformat(u["end"]) - datetime.fromisoformat(e)).days <= 100]
                if nine:
                    q[u["end"]] = u["val"] - nine[0]
        if q and (not best or max(q) > max(best)):
            best = q
    return best


def _year_ago(series, end):
    target = datetime.fromisoformat(end) - timedelta(days=364)
    for e, v in series.items():
        if abs((datetime.fromisoformat(e) - target).days) <= 20:
            return v
    return None


def xbrl_table(cik):
    """과거 분기 시계열 표 (보도자료 시점엔 이번 분기가 아직 없음 → 직전 분기까지)."""
    try:
        facts = core.http_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", SEC_HDR)["facts"]
    except Exception as e:  # noqa: BLE001
        return f"(XBRL 조회 실패: {e})"
    series = {name: quarterly(facts, tags) for name, tags in LINES}
    rev = series["매출"]
    if not rev:
        return "(분기 XBRL 매출 데이터 없음 — 해외기업(IFRS)이거나 태그 불일치. 원문만으로 판단할 것)"
    ends = sorted(rev)[-9:]
    out = ["분기말 | 매출($M) | YoY | QoQ | 매출총이익률 | 영업이익률 | 순이익($M)"]
    prev_rev = None
    for e in ends:
        r = rev[e]
        ya = _year_ago(rev, e)
        yoy = f"{(r / ya - 1) * 100:+.1f}%" if ya else "-"
        qoq = f"{(r / prev_rev - 1) * 100:+.1f}%" if prev_rev else "-"
        prev_rev = r
        gp, op, ni = (series[k].get(e) for k in ("매출총이익", "영업이익", "순이익"))
        gm = f"{gp / r * 100:.1f}%" if gp is not None else "-"
        om = f"{op / r * 100:.1f}%" if op is not None else "-"
        nis = f"{ni / 1e6:,.1f}" if ni is not None else "-"
        out.append(f"{e} | {r / 1e6:,.1f} | {yoy} | {qoq} | {gm} | {om} | {nis}")
    out.append("※ 회계연도 4분기는 '연간 − 9개월 누계'로 복원한 값. QoQ는 바로 위 행 대비.")
    return "\n".join(out[:1] + out[-9:] if len(out) > 10 else out)


def build_prompt(job, doc_path, table, checks):
    with open(os.path.join(REPO, "prompts", "earnings_score.md"), encoding="utf-8") as f:
        tpl = f.read()
    kind = ("실적 보도자료(8-K). 10-Q는 아직 제출 전" if job["form"] == "8-K"
            else f"{job['form']} 원문(재무제표·주석·MD&A)")
    rep = {"{{TICKER}}": job["ticker"], "{{OWNER}}": job["owner"], "{{FORM}}": job["form"],
           "{{KIND}}": kind, "{{FILED}}": job["filed"], "{{URL}}": job["url"], "{{DOC_PATH}}": doc_path,
           "{{XBRL_TABLE}}": table,
           "{{CHECKS}}": "\n".join(f"- {c}" for c in checks) or "- (등록된 체크 항목 없음 — 6대 신호만 채점)"}
    for k, v in rep.items():
        tpl = tpl.replace(k, v)
    return tpl


# ───────────────────────── 실행 ─────────────────────────

def run_claude(prompt, workdir):
    exe = shutil.which("claude") or "claude"
    r = subprocess.run(
        [exe, "-p", "--allowedTools", "Read,Grep,Glob", "--max-turns", "40", "--output-format", "text"],
        input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=CLAUDE_TIMEOUT, cwd=workdir)
    out = (r.stdout or "").strip()
    if r.returncode != 0 or len(out) < 200:
        raise RuntimeError(f"claude exit={r.returncode} out={out[:200]!r} err={(r.stderr or '')[-300:]!r}")
    return out


def score_job(job, dry, prepare_only):
    checks = core.load_thesis().get("tickers", {}).get(job["ticker"], {}).get("checks", [])
    text = filing_documents(job)
    if job["form"] == "6-K" and not EARNINGS_6K.search(text[:20000]):
        return None  # 실적과 무관한 6-K
    workdir = tempfile.mkdtemp(prefix="score_")
    doc_path = os.path.join(workdir, "filing.txt")
    with open(doc_path, "w", encoding="utf-8") as f:
        f.write(text)
    table = xbrl_table(job["cik"])
    prompt = build_prompt(job, "filing.txt", table, checks)
    if prepare_only:
        print(prompt[:3000], f"\n... (원문 {len(text):,}자 → {doc_path})")
        return None
    result = run_claude(prompt, workdir)
    return f"🧾 [실적 채점] {job['ticker']} {job['form']} · {job['owner']} · 제출 {job['filed']}\n\n{result}\n\n원문: {job['url']}"


def main():
    dry, prep = "--dry-run" in sys.argv, "--prepare-only" in sys.argv
    st = core.load_state()
    if not st or not st.get("score_queue"):
        print("대기열 비어 있음")
        return
    queue, done = st["score_queue"], 0
    for job in list(queue):
        if done >= MAX_JOBS:
            break
        done += 1
        try:
            msg = score_job(job, dry, prep)
            if prep:
                continue
            if msg:
                core.send(msg, dry)
            queue.remove(job)
            print(f"scored {job['ticker']} {job['form']}")
        except Exception as e:  # noqa: BLE001
            job["tries"] += 1
            print(f"FAIL {job['ticker']} {job['form']} try {job['tries']}: {e}", file=sys.stderr)
            if job["tries"] >= MAX_TRIES:
                queue.remove(job)
                core.send(f"⚠️ [실적 채점 실패] {job['ticker']} {job['form']} — {MAX_TRIES}회 시도 실패, 원문 직접 확인 필요\n"
                          f"   {str(e)[:200]}\n   {job['url']}", dry)
    if not prep:
        core.save_state(st)
    print(f"남은 대기열 {len(queue)}건")


if __name__ == "__main__":
    main()
