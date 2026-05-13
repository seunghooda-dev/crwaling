# 시스템 구조

## 목표

방송 뉴스 제작팀이 공개 정보 흐름을 빠르게 확인하고, 출처와 검증 상태를 놓치지 않도록 돕는 내부 도구를 만든다.

## 구성

```text
sources -> crawlers -> parsers -> repository -> API/dashboard
                         |
                         -> deduplication / classification / alerts
```

## 모듈 설명

- `app.models`: Source, Article 데이터 모델
- `app.crawlers`: RSS/HTML/API 수집기
- `app.repository`: SQLite 저장/조회
- `app.services`: 수집 실행, 향후 분류/알림/검증 서비스
- `app.main`: FastAPI 조회 API
- `app.cli`: DB 초기화와 1회 수집 명령

## 설계 원칙

- 사이트별 파서는 adapter 방식으로 추가한다.
- 원문 전체 재배포보다 내부 취재 보조와 출처 링크 중심으로 설계한다.
- robots.txt, 이용약관, 요청 빈도 제한을 지킨다.
- 수집 데이터에는 출처, 수집 시각, 원문 URL, 검증 상태를 반드시 남긴다.
- AI 기능은 자동 확정이 아니라 데스크 검토를 돕는 보조 기능으로 둔다.

