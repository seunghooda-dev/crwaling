# Broadcast News Crawling Assistant

뉴스 제작팀을 위한 내부 취재 보조형 크롤링 프로그램입니다.

첫 번째 목표는 여러 공개 소스에서 기사/공지/보도자료 메타데이터를 수집하고,
중복을 줄이며, 뉴스룸에서 빠르게 확인할 수 있는 검색 API와 운영 구조를 만드는 것입니다.

## 1단계 범위

- RSS/HTML/API 수집기를 붙일 수 있는 공통 구조
- SQLite 저장소
- 기사 중복 식별용 fingerprint
- 출처별 수집 설정
- FastAPI 기반 조회 API
- 향후 알림, 요약, 검증 워크플로 확장 지점

## 실행

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.cli init-db
python -m app.cli crawl-once
python -m app.cli rescore
python -m app.cli detail-crawl --limit 50
python -m app.cli rebuild-clusters --limit 500
python -m app.cli backup
python -m app.cli maintenance --limit 50
python -m app.cli validate-config
python -m app.cli prune --days 90
python -m app.cli auto-crawl --interval 300
uvicorn app.main:app --reload
```

실제 출처 목록은 `config/sources.example.json`을 `config/sources.json`으로 복사한 뒤 수정합니다.

## 주요 API

- `GET /health`
- `GET /articles`
- `GET /articles/{article_id}`
- `PATCH /articles/{article_id}/workflow`
- `POST /articles/{article_id}/ai-assist`
- `POST /articles/{article_id}/enrich`
- `GET /priority`
- `GET /alerts`
- `GET /alert-events`
- `POST /alert-events/{alert_id}/ack`
- `GET /clusters`
- `POST /clusters/rebuild`
- `GET /sources`
- `GET /crawl-runs`
- `GET /stats`
- `GET /exports/articles.csv`
- `GET /exports/cuesheet.txt`
- `POST /maintenance/backup`
- `GET /maintenance/backups`
- `POST /maintenance/restore/{backup_name}`
- `POST /maintenance/prune`
- `GET /admin/validate`
- `PATCH /sources/{source_id}`

`crawl-runs`에서 출처별 성공/실패와 실패 원인을 확인합니다.
`articles?q=화재`처럼 검색할 수 있고, `articles?min_importance=3`으로 중요 키워드가 걸린 기사만 볼 수 있습니다.

## 1차 실제 출처

- SBS 뉴스 최신/이슈 RSS
- 대한민국 정책브리핑 정책뉴스/보도자료/팩트체크/부처 브리핑/영상/사진 RSS
- KBS World 오늘의 뉴스 RSS
- 연합뉴스 영문 RSS
- 행정안전부 보도자료
- 소방청 보도자료
- 경찰청 보도자료
- 안전디딤돌/국민재난안전포털 재난 정보
- 기상청 특보 현황
- 질병관리청 보도자료
- KBS 재난포털

## 뉴스룸 기능

- 중요 키워드 기반 `importance_score`
- 알림 후보 조회
- 전체/재난/소방/경찰/기상/보건/정부/뉴스 빠른 필터
- 담당자별 필터와 방송 후보 보드
- 새 기사 알림 로그 저장
- 오래된 알림 로그 정리
- 이슈 묶음 조회
- 이슈 묶음 상세 기사 비교
- 기사별 업무 상태, 검증 상태, 담당자, 데스크 메모 저장
- 새 기사 원문/메타데이터 스냅샷 저장
- 외부 AI API 없이 동작하는 요약, 앵커 멘트, 리포트 구성안, 검증 포인트 생성
- `auto-crawl` 명령으로 주기적 자동 수집
- 자동 수집기 상태 표시
- 출처별 타임아웃/재시도 설정
- 긴급 후보 화면 강조와 알림음
- 기사 상세 페이지 수집으로 본문/대표 이미지/영상 URL 보강
- `config/notifications.json` 설정 시 웹훅 또는 이메일 알림 발송
- CSV/큐시트 내보내기
- 선택한 출처 분류 기준 CSV/큐시트 내보내기
- SQLite DB 백업
- SQLite WAL/busy timeout 적용
- 파일 로그: `logs/app.log`, `logs/crawler.log`
- 설정 검증과 간단한 출처 관리 화면
- 백업 목록/복원/보존 정책
- 기본 pytest 테스트

## Windows 실행 파일

- `install.bat`: 새 PC 초기 설치
- `run_all.bat`: 대시보드와 자동 수집 동시 실행
- `run_server.bat`: 대시보드 서버 실행
- `run_crawler.bat`: 5분 간격 자동 수집 실행
- `scripts/register_task.bat`: Windows 작업 스케줄러에 5분 간격 수집 등록
- `scripts/unregister_task.bat`: 작업 스케줄러 등록 해제
- `scripts/package_release.bat`: 다른 PC 배포용 zip 생성
- `healthcheck.bat`: 환경/설정/DB 상태 점검
- `repair.bat`: DB 초기화, 설정검사, 재점수, 클러스터 재계산
