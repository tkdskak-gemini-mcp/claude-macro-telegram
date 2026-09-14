#!/usr/bin/env python3
"""핫이슈 수집(뉴스·논문) + 하루 2회 Claude 요약.

- collect(st, seed): 피드를 모아 풀에 쌓고, 여러 매체가 동시에 다룬 '핫이슈'만 즉시 알림
- digest(dry):       풀에 쌓인 것을 Claude가 골라 3줄 요약 + 내 thesis 연결로 보냄

유료 매체(Bloomberg·FT)는 제목과 짧은 요약까지만 받을 수 있다(본문 불가).
따라서 요약에 없는 내용을 지어내지 않도록 프롬프트에서 못 박는다.
"""
import email.utils
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# run.py가 자기 자신을 주입한다(직접 실행 시 __main__ 중복 로딩 방지). 아래 __main__에서도 채운다.
core = None
CLAUDE_TIMEOUT = 900


def N():
    return core.CFG["news"]


def repo_dir():
    return os.path.dirname(core.HERE)


STOP = {"the", "and", "for", "with", "from", "that", "this", "will", "says", "after", "over", "into",
        "amid", "than", "its", "his", "her", "new", "more", "but", "are", "has", "have", "was", "were",
        "week", "day", "year", "market", "markets", "stocks", "update", "analysis", "report"}


# ───────────────────────── 수집 ─────────────────────────

def _text(el):
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def _strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _when(raw):
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw).astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def fetch_feed(feed):
    """RSS·Atom·HuggingFace JSON을 공통 형식으로."""
    raw = core.http(feed["url"], timeout=25)
    out = []
    if feed.get("kind") == "hf":
        for row in json.loads(raw):
            p = row.get("paper", {})
            if not p.get("id"):
                continue
            out.append({"title": p.get("title", ""), "url": f"https://huggingface.co/papers/{p['id']}",
                        "summary": _strip_html(p.get("summary", ""))[:600],
                        # '오늘의 논문' 목록이라 게재일이 며칠 전이어도 오늘 화제인 것으로 본다
                        "ts": core.NOW, "votes": p.get("upvotes", 0)})
        return out
    root = ET.fromstring(raw)
    items = list(root.iter("item")) or list(root.iter("{http://www.w3.org/2005/Atom}entry"))
    for it in items:
        def get(tag):
            return it.find(tag) if it.find(tag) is not None else it.find("{http://www.w3.org/2005/Atom}" + tag)
        link_el = get("link")
        url = (link_el.text if link_el is not None and link_el.text else
               link_el.get("href") if link_el is not None else "")
        title = _text(get("title"))
        if not url or not title:
            continue
        out.append({"title": title, "url": url.strip(),
                    "summary": _strip_html(_text(get("description")) or _text(get("summary")))[:600],
                    "ts": _when(_text(get("pubDate")) or _text(get("updated")) or _text(get("published"))) or core.NOW,
                    "votes": 0})
    return out


def labels_of(text):
    low = text.lower()
    return [lab for lab, kws in N()["match"].items() if any(k in low for k in kws)]


JUNK = re.compile(r"Stock Price & Latest News|Latest .* News and Analysis for|^\W*[A-Z0-9.]{1,8}\s*-\s*\|", re.I)


def tokens(title):
    w = re.findall(r"[a-z][a-z0-9'-]{3,}", title.lower())
    return {x for x in w if x not in STOP}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def key_of(url):
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def collect(st, seed):
    pool = st.setdefault("news", {})
    errs, fresh = [], []
    for feed in N()["feeds"]:
        try:
            for it in fetch_feed(feed):
                if (core.NOW - it["ts"]).total_seconds() > N()["pool_keep_h"] * 3600:
                    continue
                k = key_of(it["url"])
                # 풀에서 빠진 뒤 같은 기사가 다시 들어오는 것도 막는다(별도 기록은 14일 보존)
                if k in pool or not core.mark_seen(st, "news_ingest", k, core.NOW.strftime("%Y-%m-%d")):
                    continue
                if JUNK.search(it["title"]):   # 구글뉴스의 종목 시세 페이지·데일리 총정리 등
                    continue
                labs = labels_of(it["title"] + " " + it["summary"])
                pool[k] = {"t": it["title"][:300], "s": feed["name"], "g": feed["grade"],
                           "ts": it["ts"].isoformat(), "sum": it["summary"][:400], "u": it["url"],
                           "kind": feed.get("kind", "news"), "labs": labs, "v": it["votes"]}
                fresh.append((k, pool[k]))
        except Exception as e:  # noqa: BLE001
            errs.append(f"{feed['name']}: {str(e)[:80]}")
    if errs and len(errs) >= len(N()["feeds"]) // 2:
        raise RuntimeError("; ".join(errs[:3]))
    # 오래된 항목 정리
    cut = core.NOW - timedelta(hours=N()["pool_keep_h"])
    for k in [k for k, v in pool.items() if datetime.fromisoformat(v["ts"]) < cut]:
        del pool[k]

    out = []
    if not seed:
        out += hot_alerts(st, pool, fresh)
        # 논문은 즉시 알리지 않는다 — 추천수가 높아도 투자와 무관한 연구가 대부분이라
        # 하루 2회 요약에서 Claude가 "투자 판단으로 환산되는 것"만 고르게 한다
    mark_digest_due(st)
    return out


def hot_alerts(st, pool, fresh):
    """여러 매체가 같은 사건을 동시에 다루면 '핫이슈'로 본다.
    매체마다 제목 표현이 달라 단순 단어 겹침만으로는 안 묶인다(실측 0건) →
    ①단어 겹침 22% 이상 또는 ②겹침 15%+ 희귀단어(고유명사) 공유 를 같은 사건으로 본다."""
    win = timedelta(hours=N()["hot_window_h"])
    recent = [(k, v) for k, v in pool.items()
              if v["kind"] == "news" and core.NOW - datetime.fromisoformat(v["ts"]) <= win]
    toks = {k: tokens(v["t"]) for k, v in recent}
    df = {}
    for t in (t for s in toks.values() for t in s):
        df[t] = df.get(t, 0) + 1

    def same_event(a, b):
        j = jaccard(a, b)
        return j >= 0.22 or (j >= 0.15 and any(df.get(t, 9) <= 3 for t in a & b))

    out, used = [], set()
    for k, v in sorted(fresh, key=lambda x: x[1]["ts"], reverse=True):
        if k in used or v["kind"] != "news" or not v["labs"]:
            continue
        group = [(k2, v2) for k2, v2 in recent if k2 not in used
                 and (k2 == k or same_event(toks.get(k, set()), toks.get(k2, set())))]
        srcs = {g[1]["s"] for g in group}
        if len(srcs) < N()["hot_min_sources"]:
            continue
        used |= {g[0] for g in group}
        ckey = min(g[0] for g in group)
        if not core.mark_seen(st, "news", ckey, core.NOW.strftime("%Y-%m-%d")):
            continue
        labs = sorted({l for g in group for l in g[1]["labs"]})
        lines = "\n".join(f"   · [{g[1]['s']}] {g[1]['t'][:110]}" for g in group[:3])
        sev = core.RED if len(srcs) >= 3 else core.YEL
        out.append(core.Alert(sev, f"🗞 [핫이슈·{v['g']}] 매체 {len(srcs)}곳 동시 보도 · 관련: {', '.join(labs[:4])}\n"
                                   f"{lines}\n   {v['sum'][:160]}",
                              f"이 이슈가 내 thesis에 주는 영향 분석해줘 — {v['t'][:70]}: {v['u']}"))
        if len(out) >= N()["max_hot_per_run"]:
            break
    return out


def mark_digest_due(st):
    now = core.NOW.astimezone(core.KST)
    for h in N()["digest_hours_kst"]:
        slot = f"{now:%Y-%m-%d}-{h}"
        if now.hour >= h and st.get("news_digest_last", "") < slot:
            st["news_digest_due"] = slot


# ───────────────────────── 요약 (Claude) ─────────────────────────

def digest(dry=False):
    st = core.load_state()
    slot = (st or {}).get("news_digest_due")
    if not slot:
        print("요약 대상 없음")
        return
    pool = st.get("news", {})
    since = core.NOW - timedelta(hours=14)
    items = [v for v in pool.values() if datetime.fromisoformat(v["ts"]) >= since and (v["labs"] or v["kind"] != "news")]
    items.sort(key=lambda v: (len(v["labs"]) + (2 if v["g"] == "S" else 0) + min(v["v"], 200) / 100), reverse=True)
    items = items[:45]
    if not items:
        st["news_digest_last"] = slot
        st.pop("news_digest_due", None)
        core.save_state(st)
        print("요약할 기사 없음")
        return

    lines = []
    for v in items:
        when = datetime.fromisoformat(v["ts"]).astimezone(core.KST).strftime("%m/%d %H:%M")
        tag = f"논문·추천{v['v']}" if v["kind"] == "hf" else v["kind"]
        lines.append(f"- [{v['g']}·{v['s']}·{tag}·{when}] {v['t']}\n  요약: {v['sum'][:300]}\n  링크: {v['u']}\n"
                     f"  키워드매칭: {', '.join(v['labs']) or '없음'}")
    with open(os.path.join(repo_dir(), "prompts", "news_digest.md"), encoding="utf-8") as f:
        tpl = f.read()
    with open(os.path.join(core.HERE, "context.md"), encoding="utf-8") as f:
        ctx = f.read()
    prompt = (tpl.replace("{{NOW}}", core.NOW.astimezone(core.KST).strftime("%m/%d %H:%M"))
                 .replace("{{CONTEXT}}", ctx).replace("{{ITEMS}}", "\n".join(lines)))

    exe = shutil.which("claude") or "claude"
    r = subprocess.run([exe, "-p", "--allowedTools", "none", "--max-turns", "3", "--output-format", "text"],
                       input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=CLAUDE_TIMEOUT)
    out = (r.stdout or "").strip()
    if r.returncode != 0 or len(out) < 200:
        raise RuntimeError(f"claude exit={r.returncode} out={out[:150]!r} err={(r.stderr or '')[-200:]!r}")
    core.send(f"🗞 핫이슈 요약 · {core.NOW.astimezone(core.KST):%m/%d %H:%M} KST ({len(items)}건 중 선별)\n\n{out}", dry)
    st["news_digest_last"] = slot
    st.pop("news_digest_due", None)
    core.save_state(st)
    print(f"digest sent: slot={slot} items={len(items)}")


if __name__ == "__main__":
    import run as _run

    core = _run
    digest("--dry-run" in sys.argv)
