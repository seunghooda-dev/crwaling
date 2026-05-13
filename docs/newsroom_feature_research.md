# 뉴스룸 크롤링 프로그램 기능 조사

조사일: 2026-05-13

## 다른 뉴스룸/방송 제작 시스템에서 자주 보이는 기능

상용 뉴스룸 시스템과 미디어 모니터링 도구들은 단순 수집보다 제작 흐름 전체를 돕는 기능을 강조한다.

- 실시간 알림: 속보, 특정 키워드, 지역, 인물, 기관명 변화 감지
- 스토리 중심 관리: 기사 단건보다 하나의 이슈/사건 단위로 묶어서 관리
- 런다운/큐시트 연동: 방송 순서, 원고, 그래픽, 송출 시스템과 연결
- 검증 워크플로: 이미지 역검색, 영상 프레임 분석, 위치/시간 검증, 번역/OCR
- 소셜/UGC 모니터링: 목격자 영상, SNS 확산, 위험 신호 감지
- 협업: 기자, PD, 데스크가 같은 이슈에 메모와 상태를 남김
- 검색/필터: 출처, 분야, 지역, 중요도, 검증 상태 기준으로 빠른 조회
- 운영 안정성: 실패 재시도, 사이트 변경 감지, 수집 로그, 관리자 설정

## 이번 프로젝트에 반영할 기능

### 1차 MVP

- 공개 RSS/API/HTML 수집 구조
- 기사 DB 저장
- URL/제목 기반 중복 방지
- 출처별 enabled/interval 설정
- 간단한 조회 API
- 수집 로그와 실패 원인 기록

### 2차

- 키워드 알림: 재난, 사건사고, 정치인/기관명, 지역명
- 이슈 클러스터링: 같은 사건 기사 묶음
- 중요도 점수: 속보성, 출처 신뢰도, 반복 보도량, 키워드 조합
- 검증 상태: 미확인, 확인중, 확인완료, 사용주의
- 데스크 메모/담당자 배정

### 3차

- 뉴스룸 대시보드
- AI 요약/키워드 추출
- 방송 원고 초안
- 큐시트/런다운 export
- Slack/Teams/문자 알림
- 이미지/영상 검증 보조 링크

## 참고한 공개 자료

- Octopus Newsroom: story-centric newsroom, rundown management, MOS/open API integration
- AP Verify: reverse image search, video frame analysis, shadow detection, geolocation, OCR/translation, social monitoring
- Storyful Newswire: eyewitness video monitoring and verification, source/location/timeframe checks
- Dataminr for News: breaking-event early alerts for newsrooms
- Hootsuite/Talkwalker-style media monitoring: news, social, broadcast, radio, print, forum, podcast monitoring

