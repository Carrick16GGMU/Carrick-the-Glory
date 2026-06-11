"""
시장 지표 알림 봇 v2.1  (GitHub Actions에서 5분마다 자동 실행)

데이터 소스 하이브리드:
  - 토스 Open API(공식) : QQQ, SPY, RSP, HYG  → /api/v1/prices(배치) + /api/v1/candles
  - 야후(보조)          : ^VIX, ^TNX, NQ=F, ^KS11, KRW=X, CL=F (토스가 지수/선물/금리 미제공)

편향 방지 장치:
  - alerts_log.csv : 모든 발동 기록 → 적중률 사후 검증 가능 (기억 편집 방지)
  - session: KR    : 디커플링은 한국 장중에만 평가 (세션 불일치 가짜신호 방지)
  - 하트비트       : 매일 아침 1회 시스템 생존 보고 (침묵=고장 오독 방지) + 레벨 노후 경고

보안:
  - 토스 호출은 읽기(prices/candles)만. 주문 엔드포인트는 코드에 없음.
  - access token은 메모리에서만 사용, 절대 출력/저장하지 않음 (Public repo).
"""

import os
import json
import csv
import requests
import yaml
from datetime import datetime, timezone, timedelta

# ---- 환경변수 (GitHub Secrets) ----
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")         # 없으면 AI 한 줄 생략
TOSS_ID = os.environ.get("TOSS_CLIENT_ID")
TOSS_SECRET = os.environ.get("TOSS_CLIENT_SECRET")

STATE_FILE = "state.json"
LOG_FILE = "alerts_log.csv"

TOSS_BASE = "https://openapi.tossinvest.com"
TOSS_SET = {"QQQ", "SPY", "RSP", "HYG"}               # 토스로 받는 종목
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
UA = {"User-Agent": "Mozilla/5.0"}
KST = timezone(timedelta(hours=9))


# ═════════════ ① 수집: 토스 ═════════════
def toss_token():
    """토큰 발급. 절대 출력/저장 금지."""
    r = requests.post(
        f"{TOSS_BASE}/oauth2/token",
        auth=(TOSS_ID, TOSS_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def toss_prices(token, symbols):
    """배치 현재가. {symbol: (lastPrice, 'YYYY-MM-DD')} 반환."""
    r = requests.get(
        f"{TOSS_BASE}/api/v1/prices",
        params={"symbols": ",".join(symbols)},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    r.raise_for_status()
    out = {}
    for item in r.json().get("result", []):
        out[item["symbol"]] = (float(item["lastPrice"]), str(item.get("timestamp", ""))[:10])
    return out


def _candle_close(c):
    for k in ("close", "closePrice", "tradePrice", "c", "closingPrice"):
        if k in c and c[k] is not None:
            return float(c[k])
    return None


def _candle_date(c):
    for k in ("dateTime", "datetime", "dt", "timestamp", "time", "date", "baseDate"):
        if k in c and c[k] is not None:
            return str(c[k])[:10]
    return None


def toss_candles(token, symbol):
    """일봉 리스트 [(date, close), ...] 오래된→최신 순. 파라미터/필드명은 관용적으로 처리."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = None
    for param in ("symbol", "symbols"):                 # 명세상 SymbolQuery 이름 미확정 → 둘 다 시도
        r = requests.get(
            f"{TOSS_BASE}/api/v1/candles",
            params={param: symbol, "interval": "1d", "count": 120},
            headers=headers, timeout=10,
        )
        if r.status_code == 200:
            resp = r.json()
            break
    if resp is None:
        print(f"[경고] {symbol} 토스 캔들 조회 실패")
        return []

    raw = resp.get("result", resp)
    if isinstance(raw, dict):                            # {"candles":[...]} 또는 {"symbol":..,"candles":[..]} 형태 대응
        raw = raw.get("candles", raw.get("items", []))
    if raw and isinstance(raw[0], dict) and "candles" in raw[0]:
        raw = raw[0]["candles"]

    rows = []
    for c in raw:
        d, cl = _candle_date(c), _candle_close(c)
        if d and cl is not None:
            rows.append((d, cl))
    if not rows:
        print(f"[경고] {symbol} 캔들 필드 해석 실패. 샘플: {str(raw[:1])[:200]}")
    rows.sort(key=lambda x: x[0])
    return rows


def prev_close_from(rows, price_date):
    """전일종가 결정: 마지막 봉이 '오늘(가격 타임스탬프 날짜)'의 미완성 봉이면 그 직전 봉을 사용."""
    if not rows:
        return None
    if rows[-1][0] == price_date and len(rows) >= 2:
        return rows[-2][1]
    return rows[-1][1]


def fetch_toss_series(symbols):
    """토스 종목들의 시리즈 일괄 생성. 실패 종목은 None."""
    out = {s: None for s in symbols}
    if not (TOSS_ID and TOSS_SECRET):
        print("[경고] 토스 키 없음 → 토스 종목 건너뜀 (Secrets에 TOSS_CLIENT_ID/SECRET 등록 필요)")
        return out
    try:
        token = toss_token()
        prices = toss_prices(token, symbols)
    except Exception as e:
        print(f"[경고] 토스 prices 실패: {e}")
        return out
    for s in symbols:
        if s not in prices:
            print(f"[경고] {s} 토스 현재가 없음")
            continue
        price, pdate = prices[s]
        rows = toss_candles(token, s)
        closes = [c for _, c in rows]
        prev = prev_close_from(rows, pdate)
        out[s] = {
            "price": price,
            "change_pct": (price / prev - 1) * 100 if prev else None,
            "change_abs": (price - prev) if prev else None,
            "closes": closes,
        }
    return out


# ═════════════ ① 수집: 야후 ═════════════
def _prev_from_closes(price, closes):
    """전일종가를 일봉 종가 배열에서 결정.
    야후 chartPreviousClose는 range에 따라 '구간 첫 봉 직전(=3개월 전)' 값을 줘서 쓰면 안 됨.
    마지막 봉이 '오늘 진행중'이면 현재가와 거의 같으므로 그 직전 봉을 전일종가로 사용."""
    if not closes:
        return None
    if price and len(closes) >= 2 and abs(closes[-1] - price) / price < 0.0005:
        return closes[-2]
    return closes[-1]


def fetch_yahoo(symbol):
    try:
        r = requests.get(
            YAHOO_URL.format(symbol=symbol),
            params={"range": "3mo", "interval": "1d"},
            headers=UA, timeout=10,
        )
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        meta = res["meta"]
        price = meta.get("regularMarketPrice")
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        prev = _prev_from_closes(price, closes)   # ← chartPreviousClose 사용 금지(3개월 전 값 버그)
        return {
            "price": price,
            "change_pct": (price / prev - 1) * 100 if price and prev else None,
            "change_abs": (price - prev) if price and prev else None,
            "closes": closes,
        }
    except Exception as e:
        print(f"[경고] {symbol} 야후 조회 실패: {e}")
        return None


# ═════════════ ② 판정 ═════════════
def moving_avg(d, period):
    cs = d["closes"]
    return sum(cs[-period:]) / period if len(cs) >= period else None


def lookback_return(d, days):
    cs = d["closes"]
    if len(cs) <= days or d["price"] is None:
        return None
    return (d["price"] / cs[-days] - 1) * 100


def session_ok(rule, now_kst):
    """session: KR 규칙은 한국 장중(평일 09:00~15:30 KST)에만 평가."""
    if rule.get("session") != "KR":
        return True
    if now_kst.weekday() >= 5:
        return False
    t = now_kst.hour * 60 + now_kst.minute
    return 9 * 60 <= t <= 15 * 60 + 30


def evaluate(rule, cache):
    t = rule.get("type", "single")

    if t == "single":
        d = cache.get(rule["symbol"])
        if not d:
            return None, False
        val = {"price": d["price"], "change_pct": d["change_pct"],
               "change_abs": d["change_abs"]}.get(rule["metric"])
        if val is None:
            return None, False
        trig = val >= rule["threshold"] if rule["direction"] == "above" else val <= rule["threshold"]
        return round(val, 3), trig

    if t == "ma_cross":
        d = cache.get(rule["symbol"])
        if not d or d["price"] is None:
            return None, False
        ma = moving_avg(d, rule["ma_period"])
        if ma is None:
            return None, False
        trig = d["price"] < ma if rule["direction"] == "below" else d["price"] > ma
        return f"{round(d['price'],2)} vs MA{rule['ma_period']} {round(ma,2)}", trig

    if t == "divergence":
        da, db = cache.get(rule["symbol_a"]), cache.get(rule["symbol_b"])
        if not da or not db:
            return None, False
        if rule["mode"] == "daily_gap":
            if da["change_pct"] is None or db["change_pct"] is None:
                return None, False
            gap = da["change_pct"] - db["change_pct"]
            return f"{round(gap,2)}%p", abs(gap) >= rule["threshold"]
        if rule["mode"] == "lookback_spread":
            ra = lookback_return(da, rule["lookback_days"])
            rb = lookback_return(db, rule["lookback_days"])
            if ra is None or rb is None:
                return None, False
            spread = ra - rb
            trig = spread <= rule["threshold"] if rule["direction"] == "below" else spread >= rule["threshold"]
            return f"{round(spread,2)}%p({rule['lookback_days']}일)", trig

    return None, False


# ═════════════ 상태·로그 ═════════════
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def append_log(fired, now_kst):
    """발동 기록 → 사후 적중률 검증용 (생존편향 방지)."""
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["datetime_kst", "rule", "value"])
        for a in fired:
            w.writerow([now_kst.strftime("%Y-%m-%d %H:%M"), a["name"], a["value"]])


# ═════════════ 하트비트 ═════════════
def heartbeat_due(state, now_kst):
    return now_kst.hour >= 8 and state.get("_last_heartbeat") != now_kst.strftime("%Y-%m-%d")


def heartbeat_text(meta_cfg, counters, now_kst):
    lines = [f"✅ <b>알림봇 생존 보고</b> ({now_kst.strftime('%m/%d %H:%M')} KST)"]
    lines.append(f"• 직전 보고 이후: 체크 {counters.get('runs',0)}회 / 수집실패 {counters.get('fails',0)}건 / 알림 {counters.get('alerts',0)}건")
    lu = (meta_cfg or {}).get("levels_updated")
    if lu:
        try:
            age = (now_kst.date() - datetime.strptime(lu, "%Y-%m-%d").date()).days
            if age >= 7:
                lines.append(f"⚠️ QQQ 레벨(선행스팬) 마지막 갱신 {age}일 경과 — 트레이딩뷰에서 재확인 필요")
        except ValueError:
            pass
    return "\n".join(lines)


# ═════════════ ④ AI 한 줄(선택) / ⑤ 발송 ═════════════
def make_ai_line(fired, snapshot):
    """알림 발동 시 OpenAI로 해석 1~2문장 생성. 키 없으면 빈 문자열."""
    if not OPENAI_KEY:
        return ""
    try:
        names = ", ".join(a["name"] for a in fired)
        prompt = (f"방금 충족된 시장 알림: {names}.\n현재 스냅샷: {snapshot}\n"
                  "이 조합을 한국어 1~2문장으로 투자자 관점에서 해석해줘. "
                  "리스크온/오프 성격과 주의점만 간결히. 단정적 매매 지시는 하지 마.")
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}",
                     "Content-Type": "application/json"},
            json={"model": "gpt-4.1", "max_tokens": 200,
                  "messages": [
                      {"role": "system", "content": "너는 한국어로만 답하는 금융 애널리스트다. "
                       "반드시 한국어로만 작성하고 일본어·한자(漢字)·중국어를 절대 섞지 마라."},
                      {"role": "user", "content": prompt}
                  ]},
            timeout=20,
        )
        r.raise_for_status()
        return "\n\n🧠 " + r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[경고] AI 한 줄 실패: {e}")
        return ""


def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        print("[경고] 텔레그램 미설정 → 콘솔 출력만\n" + text)
        return
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )


# ═════════════ 지휘 ═════════════
def collect_symbols(alerts):
    syms = set()
    for a in alerts:
        if a.get("type") == "divergence":
            syms.add(a["symbol_a"]); syms.add(a["symbol_b"])
        else:
            syms.add(a["symbol"])
    return syms


def main():
    with open("config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    alerts, meta_cfg = cfg["alerts"], cfg.get("meta", {})

    now_kst = datetime.now(timezone.utc).astimezone(KST)
    syms = collect_symbols(alerts)
    toss_syms = sorted(syms & TOSS_SET)
    yahoo_syms = sorted(syms - TOSS_SET)

    cache, fails = {}, 0
    cache.update(fetch_toss_series(toss_syms))
    for s in yahoo_syms:
        cache[s] = fetch_yahoo(s)
    fails = sum(1 for v in cache.values() if v is None)

    state = load_state()
    counters = state.get("_counters", {"runs": 0, "fails": 0, "alerts": 0})

    snapshot = {s: {"price": d["price"], "chg%": round(d["change_pct"], 2) if d["change_pct"] is not None else None}
                for s, d in cache.items() if d}

    fired = []
    for rule in alerts:
        if not session_ok(rule, now_kst):
            continue                                   # 세션 밖: 평가/상태 모두 보존
        value, triggered = evaluate(rule, cache)
        if triggered and not state.get(rule["name"], False):
            fired.append({"name": rule["name"], "value": value})
        state[rule["name"]] = triggered

    counters["runs"] += 1
    counters["fails"] += fails
    counters["alerts"] += len(fired)

    if fired:
        append_log(fired, now_kst)
        text = "\n".join(["🚨 <b>시장 알림</b>"] +
                         [f"• {a['name']}  (현재 {a['value']})" for a in fired])
        send_telegram(text + make_ai_line(fired, snapshot))
        print("알림 발송:", [a["name"] for a in fired])
    else:
        print("새로 충족된 알림 없음.")

    if heartbeat_due(state, now_kst):
        send_telegram(heartbeat_text(meta_cfg, counters, now_kst))
        state["_last_heartbeat"] = now_kst.strftime("%Y-%m-%d")
        counters = {"runs": 0, "fails": 0, "alerts": 0}

    state["_counters"] = counters
    save_state(state)


if __name__ == "__main__":
    main()
