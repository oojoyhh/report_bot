"""
SK Report Bot
매일 아침 SK하이닉스(반도체/메모리) + IT서비스/AI(SK AX 관련 업종) 증권사 리포트를 모아
텔레그램으로 보내는 스크립트.

GitHub Actions 등 일반 인터넷 접속이 가능한 환경에서 실행하는 것을 전제로 합니다.

환경변수:
  TELEGRAM_BOT_TOKEN   (필수) 텔레그램 봇 토큰
  TELEGRAM_CHAT_ID     (필수) 메시지를 받을 chat id
  ANTHROPIC_API_KEY    (선택) 있으면 리포트 제목들을 쉬운 말로 풀어쓴 요약을 추가로 생성
  DRY_RUN              "1"이면 텔레그램 전송 없이 콘솔에만 출력
"""

import os
import re
import sys
import json
import html
import datetime
from urllib.parse import urljoin, urlsplit

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


def normalize_research_link(raw_href: str):
    """
    네이버 리서치 상세 링크를 항상 /research/... 주소로 정규화한다.

    목록 페이지의 href는 ``company_read.naver?...`` 같은 상대경로인데,
    이를 네이버 도메인 루트에 바로 붙이면 존재하지 않는
    ``/company_read.naver`` 주소가 된다. 상대/절대경로 여부와 관계없이
    실제 상세 페이지가 있는 ``/research/<read_type>.naver``로 고정한다.
    """
    match = READ_LINK_RE.search(raw_href or "")
    if not match:
        return None

    query = urlsplit(raw_href).query
    link = f"{NAVER_BASE}/research/{match.group(1)}.naver"
    return f"{link}?{query}" if query else link


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

            # 첨부(pdf) 링크 찾기
            pdf_link = None
            for a in tr.find_all("a"):
                href = a.get("href", "")
                if href.lower().endswith(".pdf"):
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


def _clean_summary_text(text: str) -> str:
    """모델이 실수로 붙인 마크다운 표식을 텔레그램 본문에서 제거한다."""
    cleaned_lines = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = re.sub(r"^\s*[-*]\s+", "", line)
        line = line.replace("**", "").strip()
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def build_summary_prompt(hynix_reports, industry_reports):
    """짧은 한두 문장이 아닌, 충분한 맥락을 담은 핵심 요약용 프롬프트."""
    hynix_listing = "\n".join(
        f"- {r['title']} ({r['org']}, {r['date']})" for r in hynix_reports
    ) or "- 해당 리포트 없음"
    industry_listing = "\n".join(
        f"- {r['title']} ({r['org']}, {r['date']})" for r in industry_reports
    ) or "- 해당 리포트 없음"

    return (
        "너는 SK그룹, 특히 SK하이닉스와 SK AX 취업·이직·면접을 준비하는 사람에게 "
        "매일 아침 산업 동향의 핵심을 충분한 맥락과 함께 설명하는 코치야.\n\n"
        "[SK하이닉스 관련 리포트]\n"
        f"{hynix_listing}\n\n"
        "[반도체·IT서비스·AI 산업 리포트]\n"
        f"{industry_listing}\n\n"
        "위 제목들에서 공통으로 읽히는 흐름만 종합해 하나의 핵심 요약을 작성해. "
        "개별 리포트를 하나씩 나열하거나 별도의 추천 리포트 목록을 만들지 마.\n"
        "요약은 한국어 기준 900~1,200자, 10~14개의 완결된 문장, 4개의 짧은 문단으로 구성해:\n"
        "1문단은 오늘 시장 전체 흐름과 그 배경, "
        "2문단은 반도체·메모리와 SK하이닉스에 미치는 영향, "
        "3문단은 AI·데이터센터·IT서비스와 SK AX에 미치는 영향, "
        "4문단은 지원자가 면접에서 연결해 말할 수 있는 구체적인 관점을 담아.\n"
        "전문용어는 처음 나올 때 쉬운 말로 풀고, 제목만으로 확인할 수 없는 수치·사실은 단정하지 마. "
        "핵심 결론뿐 아니라 왜 그런지, SK의 사업과 어떤 관련이 있는지까지 설명해.\n"
        "제목, 소제목, 불릿, 이모지, #, ** 같은 마크다운은 사용하지 마.\n\n"
        "아래 JSON 형식의 객체 하나만 출력해. 코드블록이나 다른 설명은 쓰지 마:\n"
        '{"overview": "네 문단으로 구성된 핵심 요약"}'
    )


def summarize_with_claude(hynix_reports, industry_reports):
    """
    ANTHROPIC_API_KEY가 있으면 리포트 제목들을 재료로
    { "overview": "..." } 형태의 충분히 상세한 핵심 요약 JSON을 생성.
    키가 없거나 실패하면 None.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        print("[WARN] anthropic 패키지가 설치되어 있지 않아 요약을 건너뜁니다.", file=sys.stderr)
        return None

    if not hynix_reports and not industry_reports:
        return None

    prompt = build_summary_prompt(hynix_reports, industry_reports)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _strip_code_fence(message.content[0].text)
        data = json.loads(raw)

        return {
            "overview": _clean_summary_text(data.get("overview", "")) or None,
        }
    except Exception as e:
        print(f"[WARN] Claude 요약 생성 실패: {e}", file=sys.stderr)
        return None


def _section(title_line, reports, tier):
    """리포트 목록을 링크만 있는 간단한 형태로 렌더링."""
    lines = [title_line]
    note = FALLBACK_NOTES.get(tier)
    if note:
        lines.append(html.escape(note))
    if reports:
        for r in reports:
            lines.append(f'• <a href="{html.escape(r["link"], quote=True)}">{html.escape(r["title"])}</a>')
    else:
        lines.append("· 가져온 리포트 없음 (사이트 구조 변경 등으로 파싱 실패 가능성)")
    return lines


def build_message(hynix_reports, hynix_tier, industry_reports, industry_tier):
    today = datetime.date.today().strftime("%Y.%m.%d")
    lines = [f"<b>📊 SK 산업동향 브리핑 ({today})</b>", ""]

    summary = summarize_with_claude(hynix_reports, industry_reports)

    if summary and summary.get("overview"):
        lines.append("<b>🗣 오늘의 핵심 요약</b>")
        lines.append(html.escape(summary["overview"]))
        lines.append("")

    lines += _section(f"<b>🔧 SK하이닉스 관련 리포트 ({len(hynix_reports)}건)</b>", hynix_reports, hynix_tier)
    lines.append("")
    lines += _section(
        f"<b>💡 반도체·IT서비스/AI 산업동향 ({len(industry_reports)}건)</b>",
        industry_reports,
        industry_tier,
    )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        lines.append("")
        lines.append("※ ANTHROPIC_API_KEY를 등록하면 오늘의 핵심 흐름을 자세히 요약해서 보내드려요.")

    lines.append("")
    lines.append("출근길 화이팅! 🚇")

    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # 텔레그램 메시지 길이 제한(4096자) 대응: 필요시 분할 전송
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)] or [text]
    for chunk in chunks:
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
