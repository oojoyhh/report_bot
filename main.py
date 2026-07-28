"""
SK Report Bot
매일 아침 SK하이닉스(반도체/메모리) + IT서비스/AI(SK AX 관련 업종) 증권사 리포트를 모아
텔레그램으로 보내는 스크립트.

GitHub Actions 등 일반 인터넷 접속이 가능한 환경에서 실행하는 것을 전제로 합니다.

환경변수:
  TELEGRAM_BOT_TOKEN   (필수) 텔레그램 봇 토큰
  TELEGRAM_CHAT_ID     (필수) 메시지를 받을 chat id
  ANTHROPIC_API_KEY    (선택) 있으면 상위 3개 PDF를 양산관리·SK AX 면접용으로 분석
  DRY_RUN              "1"이면 텔레그램 전송 없이 콘솔에만 출력
"""

import os
import re
import sys
import json
import html
import datetime
from io import BytesIO
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

NAVER_BASE = "https://finance.naver.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# SK하이닉스 종목코드
HYNIX_CODE = "000660"

# 산업분석 리포트 중 이 키워드가 제목에 포함되면 채택 (반도체/메모리 + IT서비스·AI)
INDUSTRY_KEYWORDS = [
    "반도체", "메모리", "HBM", "D램", "낸드", "파운드리",
    "IT서비스", "소프트웨어", "클라우드", "인공지능", "AI", "디지털전환", "DX",
]

LOOKBACK_DAYS = 3  # 최근 N일 이내 리포트만 채택 (주말/휴장 대비 여유)


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    # 네이버금융은 EUC-KR 인코딩을 쓰는 경우가 많음
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = "euc-kr"
    return resp.text


def parse_date(date_str: str):
    """'25.07.28' 형태 문자열을 datetime.date로 변환. 실패 시 None."""
    date_str = date_str.strip()
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{2})", date_str)
    if not m:
        return None
    yy, mm, dd = m.groups()
    try:
        return datetime.date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None


def is_recent(date_str: str, days: int = LOOKBACK_DAYS) -> bool:
    d = parse_date(date_str)
    if d is None:
        return False
    return (datetime.date.today() - d).days <= days


DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{2}")
# 리포트 상세페이지 링크 패턴 (컬럼 위치에 의존하지 않기 위해 이걸로 제목 링크를 찾는다)
READ_LINK_RE = re.compile(r"(company_read|industry_read|market_read|debenture_read)\.naver")
PDF_URL_RE = re.compile(r"https?://[^\s\"'<>]+\.pdf", re.IGNORECASE)


def normalize_research_link(raw_href: str):
    """
    네이버 리서치 상세 링크를 /research/<read_type>.naver 주소로 정규화한다.

    상세 화면에 필요한 nid/page만 남기고 목록 검색용 파라미터는 제거한다.
    모바일에서는 상세 페이지보다 PDF 직접 링크를 우선 사용한다.
    """
    match = READ_LINK_RE.search(raw_href or "")
    if not match:
        return None

    query = parse_qs(urlsplit(raw_href).query)
    nid = query.get("nid", [None])[0]
    if not nid:
        return None
    page = query.get("page", ["1"])[0]
    return f"{NAVER_BASE}/research/{match.group(1)}.naver?nid={nid}&page={page}"


def parse_research_table(html_text: str, base_url: str):
    """
    네이버금융 research 목록 페이지를 파싱.
    컬럼 순서(종목명/제목/증권사/첨부/작성일)가 페이지마다 조금씩 달라질 수 있어서,
    컬럼 인덱스에 의존하지 않고 각 행(tr)에서
      - 제목: '_read.naver' 상세페이지로 가는 <a> 태그
      - 날짜: 행 텍스트에서 'YY.MM.DD' 패턴
      - 첨부(pdf): href가 '.pdf'로 끝나는 <a> 태그
      - 증권사: 나머지 <td> 텍스트 중 제목/날짜가 아닌 것
    을 각각 찾아서 조립한다.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        print("[DEBUG] 페이지에서 <table>을 하나도 찾지 못함", file=sys.stderr)
        return []

    reports = []
    for table in tables:
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue

            # 제목 링크 찾기
            title_tag = None
            for a in tr.find_all("a"):
                href = a.get("href", "")
                if READ_LINK_RE.search(href):
                    title_tag = a
                    break
            if title_tag is None:
                continue
            title = title_tag.get_text(strip=True)
            if not title:
                continue

            raw_href = title_tag.get("href", "")
            link = normalize_research_link(raw_href)
            if not link:
                continue

            # 날짜 찾기
            row_text = tr.get_text(" ", strip=True)
            date_match = DATE_RE.search(row_text)
            date_str = date_match.group(0) if date_match else ""

            # 네이버는 PDF 주소를 href 외 속성에 넣는 경우도 있어 행 전체를 확인한다.
            pdf_link = None
            pdf_match = PDF_URL_RE.search(str(tr))
            if pdf_match:
                pdf_link = pdf_match.group(0)
            else:
                for a in tr.find_all("a"):
                    href = a.get("href", "")
                    if urlsplit(href).path.lower().endswith(".pdf"):
                        pdf_link = urljoin(base_url, href)
                        break

            # 증권사(작성기관) 추정: 제목이 들어있는 td '이후'의 td들 중
            # 비어있지 않고 날짜/제목이 아닌 첫 텍스트 (제목 이전 td는 종목명 컬럼일 수 있어 제외)
            org = ""
            title_td = title_tag.find_parent("td")
            try:
                title_idx = tds.index(title_td)
            except ValueError:
                title_idx = -1
            for td in tds[title_idx + 1:]:
                txt = td.get_text(strip=True)
                if not txt or txt == title or DATE_RE.fullmatch(txt):
                    continue
                org = txt
                break

            reports.append({
                "title": title,
                "org": org,
                "date": date_str,
                "link": link,
                "pdf": pdf_link,
            })

    print(f"[DEBUG] parse_research_table: {len(reports)}건 파싱됨", file=sys.stderr)
    return reports


MAX_ITEMS = 5  # 섹션당 최대 표시 건수

# 각 단계(tier)에 대한 사람이 읽을 설명 (메시지에 표시됨)
FALLBACK_NOTES = {
    "recent": None,
    "keyword_any_date": f"※ 최근 {LOOKBACK_DAYS}일 내 신규 리포트가 없어서, 조건에 맞는 가장 최근 리포트로 채웠어요.",
    "fallback_latest": "※ 조건에 맞는 리포트가 없어서, 최신 리포트로 대신 채웠어요.",
    "none": None,
}


def fetch_hynix_reports():
    url = f"{NAVER_BASE}/research/company_list.naver?searchType=itemCode&itemCode={HYNIX_CODE}"
    try:
        html_text = fetch_html(url)
    except Exception as e:
        print(f"[WARN] SK하이닉스 리포트 조회 실패: {e}", file=sys.stderr)
        return [], "none"
    reports = parse_research_table(html_text, url)

    tier1 = [r for r in reports if is_recent(r["date"])]
    if tier1:
        print(f"[DEBUG] 하이닉스: 최근 {LOOKBACK_DAYS}일 이내 {len(tier1)}건 (전체 {len(reports)}건)", file=sys.stderr)
        return tier1[:MAX_ITEMS], "recent"

    if reports:
        print(f"[DEBUG] 하이닉스: 최근 리포트 없음 → 전체 {len(reports)}건 중 최신 {MAX_ITEMS}건으로 대체", file=sys.stderr)
        return reports[:MAX_ITEMS], "fallback_latest"

    print("[DEBUG] 하이닉스: 파싱된 리포트 자체가 0건", file=sys.stderr)
    return [], "none"


def fetch_industry_reports():
    url = f"{NAVER_BASE}/research/industry_list.naver"
    try:
        html_text = fetch_html(url)
    except Exception as e:
        print(f"[WARN] 산업분석 리포트 조회 실패: {e}", file=sys.stderr)
        return [], "none"
    reports = parse_research_table(html_text, url)
    keyword_matched = [r for r in reports if any(kw in r["title"] for kw in INDUSTRY_KEYWORDS)]

    tier1 = [r for r in keyword_matched if is_recent(r["date"])]
    if tier1:
        print(
            f"[DEBUG] 산업분석: 키워드+최근 {len(tier1)}건 "
            f"(키워드매칭 {len(keyword_matched)}건 / 전체 {len(reports)}건)",
            file=sys.stderr,
        )
        return tier1[:MAX_ITEMS], "recent"

    if keyword_matched:
        print(f"[DEBUG] 산업분석: 최근 것 없어 키워드매칭 {len(keyword_matched)}건(날짜무관)으로 대체", file=sys.stderr)
        return keyword_matched[:MAX_ITEMS], "keyword_any_date"

    if reports:
        print(f"[DEBUG] 산업분석: 키워드매칭 0건 → 전체 {len(reports)}건 중 최신 {MAX_ITEMS}건으로 대체", file=sys.stderr)
        return reports[:MAX_ITEMS], "fallback_latest"

    print("[DEBUG] 산업분석: 파싱된 리포트 자체가 0건", file=sys.stderr)
    return [], "none"


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    return text


def _clean(text: str, max_chars=None) -> str:
    """모델이 실수로 붙인 마크다운 표식을 제거하고 과도한 출력을 제한한다."""
    text = (text or "").replace("\r\n", "\n")
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣"]
PDF_TEXT_MAX_CHARS = 8000
PDF_MAX_BYTES = 15 * 1024 * 1024
PDF_MIN_LETTER_CHARS = 120
PRODUCTION_AREAS = [
    "생산계획", "설비·생산능력", "수율·품질",
    "병목·납기", "재고·원가", "고객수요",
]
AX_AREAS = [
    "AI·데이터", "클라우드·인프라", "고객문제·사업가치",
    "서비스기획·운영", "프로젝트·협업", "비용·보안·리스크",
]
TRACK_CONFIG = {
    "hynix": {
        "label": "SK하이닉스 양산관리",
        "areas": PRODUCTION_AREAS,
        "icon": "🏭",
    },
    "ax": {
        "label": "SK AX",
        "areas": AX_AREAS,
        "icon": "🧠",
    },
}


def primary_link(report):
    """모바일에서도 바로 열리는 PDF 직접 링크를 우선한다."""
    return report.get("pdf") or report["link"]


def extract_pdf_text(pdf_url: str):
    """서버가 PDF를 내려받아 본문 텍스트를 추출한다. 실패하면 None."""
    try:
        import pypdf

        response = requests.get(pdf_url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        content = response.content
        if len(content) > PDF_MAX_BYTES:
            raise ValueError(f"PDF가 {PDF_MAX_BYTES // (1024 * 1024)}MB를 초과함")
        if b"%PDF" not in content[:1024]:
            raise ValueError("응답이 PDF 형식이 아님")

        reader = pypdf.PdfReader(BytesIO(content))
        pages = []
        for page in reader.pages[:6]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue

        text = "\n".join(pages).replace("\x00", "").strip()
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        if not text:
            raise ValueError("텍스트가 없는 이미지형 PDF")
        letter_count = len(re.findall(r"[가-힣A-Za-z]", text))
        if letter_count < PDF_MIN_LETTER_CHARS:
            raise ValueError(
                f"의미 있는 글자가 {letter_count}자로 부족한 차트·이미지형 PDF"
            )
        return text[:PDF_TEXT_MAX_CHARS]
    except Exception as e:
        print(f"[WARN] PDF 텍스트 추출 실패 ({pdf_url}): {e}", file=sys.stderr)
        return None


def select_top_candidates(hynix_reports, industry_reports, n=3):
    """
    상위 n개 안에 하이닉스와 AX 트랙을 최소 1개씩 포함한다.
    실제 본문을 읽을 수 있도록 각 트랙에서 PDF가 있는 리포트를 우선한다.
    남은 자리는 PDF 유무와 날짜를 차례로 고려해 채운다.
    """
    hynix_pool = [{**report, "track": "hynix"} for report in hynix_reports]
    ax_pool = [{**report, "track": "ax"} for report in industry_reports]

    def best_in_track(pool):
        return next((report for report in pool if report.get("pdf")), pool[0] if pool else None)

    selected = [
        report
        for report in (best_in_track(hynix_pool), best_in_track(ax_pool))
        if report is not None
    ][:n]
    remaining = [
        report
        for report in hynix_pool + ax_pool
        if report not in selected
    ]
    remaining.sort(
        key=lambda report: (
            bool(report.get("pdf")),
            parse_date(report.get("date", "")) or datetime.date.min,
        ),
        reverse=True,
    )
    selected.extend(remaining[:max(0, n - len(selected))])
    return selected[:n]


def build_briefing_prompt(materials):
    """PDF 본문을 양산관리와 SK AX 두 트랙의 초보자용 브리핑으로 바꾼다."""
    blocks = []
    for index, material in enumerate(materials, start=1):
        report = material["report"]
        body = material["body"]
        track_label = TRACK_CONFIG[report["track"]]["label"]
        header = (
            f"[리포트 {index} / {track_label} 트랙] "
            f"{report['title']} ({report['org']}, {report['date']})"
        )
        if body:
            blocks.append(f"{header}\nPDF 실제 본문에서 서버가 추출한 텍스트:\n{body}")
        elif material["source_mode"] == "visual_pdf":
            blocks.append(
                f"{header}\n이 리포트는 앞쪽 PDF 문서 블록으로 첨부됨. "
                "PDF의 텍스트·표·차트를 직접 읽어 근거를 확인할 것"
            )
        else:
            blocks.append(f"{header}\nPDF 본문 추출 실패: 확인되지 않은 내용을 추측하지 말 것")

    return (
        "너는 반도체를 처음 공부하는 SK하이닉스 양산관리 지원과 "
        "AI·데이터·클라우드 분야의 SK AX 지원을 함께 준비하는 사람의 학습 코치다. "
        "아래 리포트의 실제 PDF 본문만 근거로 두 직무의 출근길 브리핑을 작성한다.\n\n"
        + "\n\n---\n\n".join(blocks)
        + "\n\n양산관리는 생산 목표, 생산계획, 설비와 생산능력, 수율과 품질, "
        "병목과 납기, 재고와 원가, 고객 수요를 데이터와 협업으로 관리하는 직무다.\n"
        "SK AX는 기업의 업무와 데이터를 이해하고 AI·데이터·클라우드 기술을 연결해 "
        "고객 문제를 해결하며, 서비스의 실무 적용·안정적 운영·비용·보안·사업가치를 함께 고려한다.\n"
        "각 리포트에 지정된 트랙 관점으로만 직무 연결을 작성해라. "
        "PDF에 나온 사실과 네가 직무 관점에서 해석한 내용을 반드시 구분해라. "
        "본문에 없는 수치나 사실은 만들지 말고, PDF 추출 실패 리포트는 확인 불가라고 명시해라. "
        "전문용어는 처음 배우는 사람이 이해하도록 바로 풀어서 설명해라.\n\n"
        "다음 JSON 객체 하나만 출력해. 코드블록과 마크다운은 쓰지 마:\n"
        "{\n"
        '  "overview": "세 리포트에서 공통으로 읽히는 오늘의 흐름을 쉬운 말 4~6문장으로",\n'
        '  "items": [\n'
        "    {\n"
        '      "report_number": 1,\n'
        '      "beginner_summary": "이 리포트가 무슨 말인지 초보자용 2문장",\n'
        '      "evidence": ["PDF에서 직접 확인한 사실 2~3개"],\n'
        '      "role_impact": {\n'
        '        "areas": ["지정된 트랙의 허용 영역 중 관련 항목"],\n'
        '        "explain": "그 사실이 지정된 직무에 어떤 영향을 줄 수 있는지 2~3문장. 해석임을 분명히 표시"\n'
        "      }\n"
        "    }\n"
        "  ],\n"
        '  "daily_concepts": {\n'
        '    "hynix": {"term": "양산관리 필수 개념 1개", "meaning": "쉬운 정의", "analogy": "일상 비유", "why_it_matters": "직무에서 중요한 이유"},\n'
        '    "ax": {"term": "SK AX 필수 개념 1개", "meaning": "쉬운 정의", "analogy": "일상 비유", "why_it_matters": "직무에서 중요한 이유"}\n'
        "  },\n"
        '  "interviews": {\n'
        '    "hynix": {"question": "양산관리 상황형 질문", "thinking_steps": ["사고 순서 3~5단계"], "sample_answer": "30~40초 답변"},\n'
        '    "ax": {"question": "SK AX 상황형 질문", "thinking_steps": ["사고 순서 3~5단계"], "sample_answer": "30~40초 답변"}\n'
        "  },\n"
        '  "quizzes": {\n'
        '    "hynix": {"question": "양산관리 개념 확인 문제", "answer": "간단한 정답"},\n'
        '    "ax": {"question": "SK AX 개념 확인 문제", "answer": "간단한 정답"}\n'
        "  }\n"
        "}\n"
        "items는 입력된 리포트 수만큼 report_number 순서대로 작성한다. "
        f"하이닉스 트랙 role_impact.areas는 {', '.join(PRODUCTION_AREAS)} 중에서만, "
        f"AX 트랙은 {', '.join(AX_AREAS)} 중에서만 고른다. "
        "리포트마다 개념을 만들지 말고 각 트랙에 하루 하나씩만 만든다. "
        "양산관리 면접 답변은 결론, 확인할 데이터, 병목·원인, 협업·조치, 기대효과 순서로 작성한다. "
        "SK AX 면접 답변은 고객 문제, 필요한 데이터·기술, 적용 방법, 운영·비용·보안 위험, 사업가치 순서로 작성한다."
    )


def build_claude_content(materials):
    """차트형 PDF 문서 블록을 프롬프트보다 앞에 배치한다."""
    content = []
    for index, material in enumerate(materials, start=1):
        if material["source_mode"] != "visual_pdf":
            continue
        report = material["report"]
        content.append({
            "type": "document",
            "source": {
                "type": "url",
                "url": report["pdf"],
            },
            "title": f"리포트 {index}: {report['title']}",
            "context": (
                f"{TRACK_CONFIG[report['track']]['label']} 트랙 자료. "
                "텍스트, 표, 차트를 실제 PDF에서 확인할 것."
            ),
        })
    content.append({"type": "text", "text": build_briefing_prompt(materials)})
    return content


def summarize_with_claude(hynix_reports, industry_reports):
    """상위 3개 PDF를 추출해 양산관리·SK AX 두 트랙의 브리핑을 생성한다."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    candidates = select_top_candidates(hynix_reports, industry_reports)
    if not api_key or not candidates:
        return None
    try:
        import anthropic
    except ImportError:
        print("[WARN] anthropic 패키지가 설치되어 있지 않아 요약을 건너뜁니다.", file=sys.stderr)
        return None

    materials = []
    for report in candidates:
        body = extract_pdf_text(report["pdf"]) if report.get("pdf") else None
        source_mode = "text" if body else ("visual_pdf" if report.get("pdf") else "unavailable")
        materials.append({
            "report": report,
            "body": body,
            "source_mode": source_mode,
        })

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=3000,
            messages=[{"role": "user", "content": build_claude_content(materials)}],
        )
        data = json.loads(_strip_code_fence(message.content[0].text))

        items = []
        for raw_item in data.get("items", []):
            try:
                report_index = int(raw_item.get("report_number")) - 1
            except (TypeError, ValueError):
                continue
            if not 0 <= report_index < len(materials):
                continue

            material = materials[report_index]
            report = material["report"]
            track = report["track"]
            impact = raw_item.get("role_impact") or {}
            allowed_areas = TRACK_CONFIG[track]["areas"]
            areas = [
                _clean(area, 30)
                for area in impact.get("areas", [])
                if _clean(area, 30) in allowed_areas
            ][:3]
            evidence = [
                _clean(point, 260)
                for point in raw_item.get("evidence", [])
                if _clean(point, 260)
            ][:3]

            items.append({
                "report_number": report_index + 1,
                "title": report["title"],
                "track": track,
                "beginner_summary": _clean(raw_item.get("beginner_summary", ""), 420),
                "evidence": (
                    evidence
                    if material["source_mode"] in {"text", "visual_pdf"}
                    else []
                ),
                "role_impact": {
                    "areas": areas,
                    "explain": _clean(impact.get("explain", ""), 500),
                },
                "source_mode": material["source_mode"],
                "link": primary_link(report),
            })
        items.sort(key=lambda item: item["report_number"])

        concepts = {}
        interviews = {}
        quizzes = {}
        for track in TRACK_CONFIG:
            concept_raw = (data.get("daily_concepts") or {}).get(track) or {}
            concepts[track] = {
                "term": _clean(concept_raw.get("term", ""), 80),
                "meaning": _clean(concept_raw.get("meaning", ""), 300),
                "analogy": _clean(concept_raw.get("analogy", ""), 300),
                "why_it_matters": _clean(concept_raw.get("why_it_matters", ""), 350),
            }
            interview_raw = (data.get("interviews") or {}).get(track) or {}
            interviews[track] = {
                "question": _clean(interview_raw.get("question", ""), 350),
                "thinking_steps": [
                    _clean(step, 220)
                    for step in interview_raw.get("thinking_steps", [])
                    if _clean(step, 220)
                ][:5],
                "sample_answer": _clean(interview_raw.get("sample_answer", ""), 800),
            }
            quiz_raw = (data.get("quizzes") or {}).get(track) or {}
            quizzes[track] = {
                "question": _clean(quiz_raw.get("question", ""), 300),
                "answer": _clean(quiz_raw.get("answer", ""), 300),
            }
        return {
            "overview": _clean(data.get("overview", ""), 1000) or None,
            "items": items,
            "daily_concepts": concepts,
            "interviews": interviews,
            "quizzes": quizzes,
        }
    except Exception as e:
        print(f"[WARN] Claude 요약 생성 실패: {e}", file=sys.stderr)
        return None


def _report_links(title_line, reports, tier):
    """전체 리포트에는 PDF 원문 링크가 눈에 보이도록 표시한다."""
    lines = [title_line]
    note = FALLBACK_NOTES.get(tier)
    if note:
        lines.append(html.escape(note))
    if not reports:
        lines.append("· 가져온 리포트 없음 (사이트 구조 변경 등으로 파싱 실패 가능성)")
        return lines
    for report in reports:
        link = html.escape(primary_link(report), quote=True)
        link_label = "PDF 원문 열기" if report.get("pdf") else "상세 보기"
        lines.append(
            f'• {html.escape(report["title"])} '
            f'(<a href="{link}">{link_label}</a>)'
        )
    return lines


def build_message(hynix_reports, hynix_tier, industry_reports, industry_tier):
    today = datetime.date.today().strftime("%Y.%m.%d")
    lines = [f"<b>📊 SK 직무·산업동향 브리핑 ({today})</b>", ""]
    summary = summarize_with_claude(hynix_reports, industry_reports)

    if summary:
        if summary.get("overview"):
            lines += ["<b>🗣 오늘의 전체 흐름</b>", html.escape(summary["overview"]), ""]

        lines += ["<b>🔎 PDF로 읽은 핵심 리포트</b>", ""]
        for index, item in enumerate(summary.get("items", [])):
            number = NUMBER_EMOJI[index] if index < len(NUMBER_EMOJI) else f"{index + 1}."
            track = TRACK_CONFIG[item["track"]]
            lines.append(
                f'{number} {track["icon"]} <b>{html.escape(item["title"])}</b> '
                f'[{html.escape(track["label"])}]'
            )
            lines.append(
                f'📎 <a href="{html.escape(item["link"], quote=True)}">'
                "PDF 원문 열기</a>"
            )
            if item["source_mode"] == "unavailable":
                lines.append("⚠️ PDF 본문 추출 실패: 확인되지 않은 내용은 요약하지 않았어요.")
            if item["beginner_summary"]:
                lines.append(f'🔰 <b>초보자 해석</b>  {html.escape(item["beginner_summary"])}')
            if item["evidence"]:
                lines.append("📌 <b>PDF에서 확인한 사실</b>")
                lines += [f'  · {html.escape(point)}' for point in item["evidence"]]
            impact = item["role_impact"]
            if impact["explain"]:
                area_text = f' [{", ".join(impact["areas"])}]' if impact["areas"] else ""
                lines.append(
                    f'{track["icon"]} <b>{html.escape(track["label"])} 연결'
                    f'{html.escape(area_text)}</b>  '
                    f'{html.escape(impact["explain"])}'
                )
            lines.append("")

        for track_key, track in TRACK_CONFIG.items():
            concept = (summary.get("daily_concepts") or {}).get(track_key) or {}
            if concept.get("term"):
                lines += [
                    f'<b>💡 {track["icon"]} {html.escape(track["label"])} 오늘의 개념</b>',
                    f'<b>{html.escape(concept["term"])}</b>',
                    f'뜻: {html.escape(concept["meaning"])}',
                    f'비유: {html.escape(concept["analogy"])}',
                    f'직무에서 중요한 이유: {html.escape(concept["why_it_matters"])}',
                    "",
                ]

            interview = (summary.get("interviews") or {}).get(track_key) or {}
            if interview.get("question"):
                lines += [
                    f'<b>🎤 {track["icon"]} {html.escape(track["label"])} 면접 연습</b>',
                    f'질문: {html.escape(interview["question"])}',
                    "생각 순서:",
                ]
                lines += [
                    f'  {step_number}. {html.escape(step)}'
                    for step_number, step in enumerate(interview["thinking_steps"], start=1)
                ]
                lines += [
                    f'답변 예시: {html.escape(interview["sample_answer"])}',
                    "",
                ]

            quiz = (summary.get("quizzes") or {}).get(track_key) or {}
            if quiz.get("question"):
                lines += [
                    f'<b>🧩 {track["icon"]} {html.escape(track["label"])} 복습</b>',
                    f'문제: {html.escape(quiz["question"])}',
                    f'정답: <tg-spoiler>{html.escape(quiz["answer"])}</tg-spoiler>',
                    "",
                ]
    elif not os.environ.get("ANTHROPIC_API_KEY"):
        lines += [
            "※ ANTHROPIC_API_KEY를 등록하면 상위 3개 PDF 본문을 읽고 "
            "양산관리와 SK AX 면접용 두 트랙으로 정리해드려요.",
            "",
        ]

    lines += _report_links(
        f"<b>🔧 SK하이닉스 리포트 전체 ({len(hynix_reports)}건)</b>",
        hynix_reports,
        hynix_tier,
    )
    lines.append("")
    lines += _report_links(
        f"<b>💻 반도체·IT서비스/AI 산업동향 전체 ({len(industry_reports)}건)</b>",
        industry_reports,
        industry_tier,
    )
    lines += ["", "두 직무 모두 오늘도 한 개념씩 쌓아가요! 🚇"]
    return "\n".join(lines)


def split_telegram_message(text: str, limit=3800):
    """HTML 태그 중간을 자르지 않도록 빈 줄 단위로 메시지를 나눈다."""
    chunks = []
    current = ""
    for block in text.split("\n\n"):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        for line in block.splitlines():
            candidate = line if not current else f"{current}\n{line}"
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = line
    if current or not chunks:
        chunks.append(current)
    return chunks


def send_telegram(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in split_telegram_message(text):
        resp = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[ERROR] 텔레그램 전송 실패: {resp.status_code} {resp.text}", file=sys.stderr)
            resp.raise_for_status()


def main():
    dry_run = os.environ.get("DRY_RUN") == "1" or "--dry-run" in sys.argv

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not dry_run and (not token or not chat_id):
        print("[ERROR] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 필요합니다.", file=sys.stderr)
        sys.exit(1)

    hynix_reports, hynix_tier = fetch_hynix_reports()
    industry_reports, industry_tier = fetch_industry_reports()

    message = build_message(hynix_reports, hynix_tier, industry_reports, industry_tier)

    if dry_run:
        print(message)
        return

    send_telegram(token, chat_id, message)
    print("전송 완료.")


if __name__ == "__main__":
    main()
