# 시장 지표 텔레그램 알림 봇 v2.1

GitHub 서버에서 5분마다 무료 자동 실행 → 조건 충족 시 텔레그램 푸시.
(노트북·폰 꺼져 있어도 동작) 규칙 24개 / 종목 10개 / 트리거 3종 + 편향방지 장치.

## 데이터 소스 (하이브리드)
| 소스 | 종목 | 이유 |
|---|---|---|
| **토스 Open API (공식)** | QQQ, SPY, RSP, HYG | 공식·안정. /api/v1/prices(배치)+/api/v1/candles |
| **야후 (보조)** | ^VIX, ^TNX, NQ=F, ^KS11, KRW=X, CL=F | 토스가 지수·선물·금리·원자재 미제공 |

## 편향 방지 장치 (v2.1 신규)
- **alerts_log.csv** — 모든 발동 자동 기록 → 나중에 적중률을 데이터로 검증 (기억 편집 방지)
- **세션 가드** — 한·미 디커플링은 한국 장중(KST 09:00~15:30)에만 평가 (세션 불일치 가짜신호 차단)
- **데일리 하트비트** — 매일 아침 8시 이후 1회 "체크 N회/실패 M건/알림 K건" 보고 (침묵=고장 오독 방지)
  - QQQ 레벨(meta.levels_updated)이 7일 경과하면 갱신 경고 포함
- **양방향 규칙** — 하락 감지뿐 아니라 회복(VIX 20 하향 복귀, HYG 50일선 회복, QQQ $720, RSP 로테이션, NQ +3%)

## 셋업 (한 번, ~15분)
1. 텔레그램 @BotFather → 봇 토큰 / getUpdates로 chat ID
2. GitHub Public repo 생성 → 파일 업로드 (`.github/workflows/` 구조 유지)
3. **Settings → Secrets → Actions** 에 등록:
   | 이름 | 값 |
   |---|---|
   | TELEGRAM_TOKEN / TELEGRAM_CHAT_ID | 텔레그램 |
   | TOSS_CLIENT_ID / TOSS_CLIENT_SECRET | 토스 Open API 키 |
   | ANTHROPIC_API_KEY | (선택) 알림에 AI 해석 한 줄 |
4. Actions → market-alerts → **Run workflow** 로 테스트

### 첫 실행 시 꼭 확인 (1회)
Actions 로그에서:
- `[경고] ... 캔들 필드 해석 실패` 가 **없으면** 토스 캔들 파싱 정상
- 있으면 로그의 "샘플: {...}" 부분을 복사해서 알려주세요 (필드명 1곳만 추가하면 됨)
- 이미 조건을 넘어 있는 규칙들이 첫 알림으로 한꺼번에 오는 건 정상

## 평소 운영
- 고치는 곳 = **config.yaml 한 파일** (name/symbol/metric/direction/threshold)
- QQQ 레벨($654/$720)은 주 1회 트레이딩뷰(Ichimoku 9/26/52)에서 실제값 확인 후 갱신,
  갱신하면 `meta.levels_updated` 날짜도 같이 수정
- **사용 원칙: 알림 = 매매 신호가 아니라 "확인을 시작하라는 호출"** (5분 폴링 = 시장에서 늦은 정보)

## 보안
- 토스 호출은 시세 읽기(prices/candles)만. 주문 엔드포인트는 코드에 없음.
- access token은 메모리에서만 사용, 출력/저장 안 함 (Public repo 안전)
- 키는 GitHub Secrets에만. 코드·채팅·커밋에 평문 금지.

## 한계 (알고 쓰기)
- GitHub cron은 "최선 노력" — 몇 분 지연·간헐 누락 가능. 틱 단위 실시간 아님.
- 야후는 비공식 → 간헐 차단 가능(해당 지표만 건너뜀, 하트비트 실패 건수로 확인).
- 임계값은 레짐 의존 → 분기 1회 재점검. 백테스트 캘리브레이션은 2단계 TODO.
