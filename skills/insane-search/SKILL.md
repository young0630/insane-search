---
name: insane-search
description: >
  Auto-bypass for blocked websites — tries every method until one works.
  Use when WebFetch returns 402/403/blocked, or when accessing X/Twitter, Reddit,
  YouTube, GitHub, Mastodon, Medium, Substack, Stack Overflow, Threads, Naver,
  Coupang, LinkedIn, or any platform with WAF/bot protection. Leverages yt-dlp
  (1,858 media sites), Jina Reader, public APIs (HN, Bluesky, arXiv), and
  curl_cffi TLS impersonation (auto-installed) with Playwright MCP fallback.
  Korean triggers: 트위터/X 못 열어, 레딧 안 읽혀, 유튜브 자막 뽑아줘, 깃헙 검색,
  사이트 차단됨, 스레드 안 열려, 마스토돈, 미디엄, 서브스택, 스택오버플로우,
  네이버 블로그, 디시인사이드, 에펨코리아, 요즘IT, 긱뉴스, 클리앙, 쿠팡, 링크드인,
  당근마켓. English triggers: twitter access, reddit blocked, youtube subtitles,
  github search, arxiv papers, threads, mastodon, medium, substack, stackoverflow,
  naver blog, dcinside, fmkorea, coupang, linkedin, yozm, wishket.
  Do NOT trigger for simple web searches that WebSearch can handle directly.
---

# Insane Search

> URL 접근이 차단될 때, 플랫폼별 최적 방법을 자동으로 안내한다.

## Phase 0 — 특수 엔드포인트 인덱스

> 범용 체인(Phase 1~3)으로는 **발견 불가능한** 전용 API/CLI 도구만 인덱스에 둔다.
> 여기에 없는 사이트는 전부 Phase 1부터 자동 시도한다.

### 소셜/커뮤니티 전용 API

| 플랫폼 | 방법 | 상세 |
|--------|------|------|
| X/Twitter | `syndication.twitter.com/srv/timeline-profile/...` + oEmbed | [twitter.md](references/twitter.md) |
| Reddit | URL + `.json` + Mobile UA | [json-api.md](references/json-api.md) |
| Bluesky | AT Protocol (`public.api.bsky.app/xrpc/...`) | [public-api.md](references/public-api.md) |
| Mastodon | 인스턴스별 공개 API | [public-api.md](references/public-api.md) |
| Hacker News | Firebase API (`hacker-news.firebaseio.com/v0/...`) | [json-api.md](references/json-api.md) |
| Stack Overflow | SE API v2.3 (`api.stackexchange.com/2.3/...`) | [public-api.md](references/public-api.md) |
| Lobste.rs / V2EX / dev.to | 공개 JSON API | [json-api.md](references/json-api.md) |

### 미디어 (CLI 도구 필수)

| 플랫폼 | 방법 | 상세 |
|--------|------|------|
| YouTube/Vimeo/Twitch/TikTok/SoundCloud 등 1,858개 | `yt-dlp --dump-json` | [media.md](references/media.md) |

### 학술/레지스트리

| 플랫폼 | 방법 | 상세 |
|--------|------|------|
| arXiv | Atom API (`export.arxiv.org/api/query`) | [public-api.md](references/public-api.md) |
| CrossRef | REST API | [public-api.md](references/public-api.md) |
| Wikipedia | REST API | [json-api.md](references/json-api.md) |
| OpenLibrary | JSON API | [public-api.md](references/public-api.md) |
| GitHub | gh CLI / REST API | [public-api.md](references/public-api.md) |
| npm / PyPI | Registry API | [json-api.md](references/json-api.md) |
| Wayback Machine | CDX API | [public-api.md](references/public-api.md) |

### 한국 전용

| 플랫폼 | 방법 | 상세 |
|--------|------|------|
| 네이버 금융 시세 | `api.finance.naver.com/siseJson.naver` (비공식 JSON) | [naver.md](references/naver.md) |

**그 외 모든 사이트는 Phase 1~3이 자동 처리한다.**
쿠팡/요즘IT/LinkedIn/네이버 블로그/클리앙/Medium/Substack 등 — 별도 인덱스 불필요.

## 접근 순서 — 적응형 스케줄러

```
Phase 0: 특수 엔드포인트 — 인덱스에 있으면 먼저 시도 (정확성 최고)
  ↓ 실패 또는 인덱스에 없음
Phase 1: 경량 프로브 (병렬) — WebFetch + Jina + curl UA/URL 변형
  ↓ 403/WAF/챌린지/빈 SPA 감지
Phase 2: TLS 임퍼소네이션 — curl_cffi (자동설치, safari→chrome→firefox)
  ↓ TLS 우회도 실패 또는 JS 챌린지
Phase 3: Playwright MCP — 실제 브라우저 (최후 수단)
  ↓ login/paywall 감지
종료: "인증 필요" 알림

사이드카: 캐시/아카이브 (Phase 1과 동시, 원본 성공 시 참고만)
```

**원칙**: 어떤 방법도 미리 제외하지 않는다. 의존성이 없으면 설치하고 시도한다.

상세: [fallback.md](references/fallback.md)

## 빠른 참조 — 범용 명령어

```bash
# 범용 웹 (Jina Reader)
curl -s "https://r.jina.ai/{URL}"

# 미디어 메타데이터 (yt-dlp — 1,858 사이트)
yt-dlp --dump-json "URL"

# Reddit
curl -sL -H "User-Agent: Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15" "https://www.reddit.com/r/{sub}/hot.json?limit=10"

# X/Twitter 타임라인
curl -sL "https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"

# Hacker News
curl -sL "https://hacker-news.firebaseio.com/v0/topstories.json?limitToFirst=10&orderBy=%22%24key%22"

# YouTube 자막
yt-dlp --write-sub --write-auto-sub --sub-lang "en,ko" --skip-download -o "/tmp/%(id)s" "URL"
```

## 응답 검증

curl로 받은 응답의 성공/실패 판정 기준은 [fallback.md](references/fallback.md)의 "응답 검증" 참조.
모든 HTML 응답에서 메타데이터(OGP/JSON-LD)도 같이 추출 — [metadata.md](references/metadata.md) 참조.
