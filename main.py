"""
SK Report Bot
매일 아침 SK하이닉스(반도체/메모리) + IT서비스/AI(SK AX 관련 업종) 증권사 리포트를 모아
텔레그램으로 보내는 스크립트.

GitHub Actions 등 일반 인터넷 접속이 가능한 환경에서 실행하는 것을 전제로 합니다.

환경변수:
  TELEGRAM_BOT_TOKEN   (필수) 텔레그램 봇 토큰
  TELEGRAM_CHAT_ID     (필수) 메시지를 받을 chat id
  ANTHROPIC_API_KEY    (선택) 있으면 오늘의 핵심 리포트 3개를 뽑아 쉬운 말로 설명 추가
  DRY_RUN              "1"이면 텔레그램 전송 없이 콘솔에만 출력
"""

import os
import re
import sys
import json
import html
import datetime
from urllib.parse import urljoin, urlsplit, parse_qs

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
MAX_ITEMS = 5  # 섹션당 최대 표시 건수


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = "euc-kr"
    return resp.text


def parse_date(date_str: str):
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
READ_LINK_RE = re.compile(r"(company_read|industry_read|market_read|debenture_read)\.naver")
# 행(row)의 HTML 전체에서 pdf 파일 직링크를 통째로 찾기 위한 패턴 (따옴표/공백/> 전까지)
PDF_URL_RE = re.compile(r"https?://[^\s\"'<>]+\.pdf", re.IGNORECASE)


def normalize_research_link(raw_href: str):
    """
    네이버 리서치 상세 링크를 /research/<read_type>.naver?nid=..&page=.. 형태로 정규화.

    목록 페이지 앵커에는 searchType/itemCode 같은 '목록 화면 문맥용' 파라미터가
    같이 붙어오는데, 이걸 그대로 상세페이지 주소에 옮기면 정상적으로 안 열리고
    메인 화면으로 리다이렉트되는 경우가 있어서 nid/page만 남긴다.
    """
    match = READ_LINK_RE.search(raw_href or "")
    if not match:
        return None
    qs = parse_qs(urlsplit(raw_href).query)
    nid = qs.get("nid", [None])[0]
    if not nid:
        return None
    page = qs.get("page", ["1"])[0]
    return f"{NAVER_BASE}/research/{match.group(1)}.naver?nid={nid}&page={page}"


def parse_research_table(html_text: str, base_url: str):
    """
    네이버금융 research 목록 페이지를 파싱. 컬럼 위치에 의존하지 않고
    각 행(tr)에서 제목 링크(_read.naver), 날짜(YY.MM.DD), 첨부 pdf, 증권사를 찾아 조립.
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

            title_tag = None
            for a in tr.find_all("a"):
                if READ_LINK_RE.search(a.get("href", "")):
                    title_tag = a
                    break
            if title_tag is None:
                continue
            title = title_tag.get_text(strip=True)
            if not title:
                continue

            link = normalize_research_link(title_tag.get("href", ""))
            if not link:
                continue

            row_text = tr.get_text(" ", strip=True)
            date_match = DATE_RE.search(row_text)
            date_str = date_match.group(0) if date_match else ""

            # 첨부 PDF 링크 찾기: href 속성만 보지 않고, 이 행의 HTML 전체(onclick, data-* 등
            # 어디에 박혀있든)를 문자열로 훑어서 실제 pdf 파일 주소 패턴을 직접 찾는다.
            # (네이버는 href가 아니라 자바스크립트 클릭 핸들러 안에 pdf 주소를 넣어두는 경우가 있음)
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
        print(f"[DEBUG] 하이닉스: 최근 리포트 없음 → 최신 {MAX_ITEMS}건으로 대체", file=sys.stderr)
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
        print(f"[DEBUG] 산업분석: 키워드+최근 {len(tier1)}건 (키워드매칭 {len(keyword_matched)}건 / 전체 {len(reports)}건)", file=sys.stderr)
        return tier1[:MAX_ITEMS], "recent"

    if keyword_matched:
        print(f"[DEBUG] 산업분석: 최근 것 없어 키워드매칭 {len(keyword_matched)}건(날짜무관)으로 대체", file=sys.stderr)
        return keyword_matched[:MAX_ITEMS], "keyword_any_date"

    if reports:
        print(f"[DEBUG] 산업분석: 키워드매칭 0건 → 최신 {MAX_ITEMS}건으로 대체", file=sys.stderr)
        return reports[:MAX_ITEMS], "fallback_latest"

    print("[DEBUG] 산업분석: 파싱된 리포트 자체가 0건", file=sys.stderr)
    return [], "none"


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    return text


def _clean(text: str) -> str:
    """모델이 실수로 붙인 마크다운 표식을 제거."""
    text = (text or "").replace("\r\n", "\n")
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = text.replace("**", "")
    return text.strip()


def summarize_with_claude(hynix_reports, industry_reports):
    """
    ANTHROPIC_API_KEY가 있으면
    { "overview": "한 줄 총평", "highlights": [{"title","detail"}, ...] } 를 생성.
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

    all_reports = hynix_reports + industry_reports
    if not all_reports:
        return None

    listing = "\n".join(f"- {r['title']} ({r['org']}, {r['date']})" for r in all_reports)

    prompt = (
        "너는 SK그룹(특히 SK하이닉스, SK AX) 취업·이직·면접을 준비하는 사람에게 "
        "출근길에 1분 안에 읽을 아침 브리핑을 써주는 코치야. 아침이라 길게 읽을 여유가 없으니 "
        "핵심만 빠르게 눈에 들어오게 써야 해.\n\n"
        "아래는 오늘 나온 반도체·메모리 및 IT서비스·AI 관련 증권사/산업 리포트 목록이야 "
        "(제목 (증권사, 날짜) 형태):\n\n"
        f"{listing}\n\n"
        "아래 JSON 형식으로만 응답해. 다른 설명이나 코드블록 없이 JSON 객체 하나만:\n"
        "{\n"
        '  "overview": "오늘 흐름을 한 문장으로 (짧고 임팩트있게)",\n'
        '  "highlights": [\n'
        '    {"title": "위 목록의 제목을 정확히 그대로 복사", "detail": "이 리포트가 다루는 핵심과 SK와의 연결점을 딱 2문장으로, 면접에서 쓸 수 있는 포인트 포함"}\n'
        "  ]\n"
        "}\n"
        "highlights는 가장 중요한 순서로 정확히 3개(재료가 3개 미만이면 있는 만큼만). "
        "마크다운 문법(**, #, - 등)은 절대 쓰지 말고 순수 텍스트로만, 문장은 최대한 짧고 명확하게."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        data = json.loads(_strip_code_fence(message.content[0].text))

        link_by_title = {r["title"]: primary_link(r) for r in all_reports}
        highlights = []
        for h in data.get("highlights", [])[:3]:
            title = _clean(h.get("title", ""))
            detail = _clean(h.get("detail", ""))
            if not title or not detail:
                continue
            highlights.append({"title": title, "detail": detail, "link": link_by_title.get(title)})

        return {"overview": _clean(data.get("overview", "")) or None, "highlights": highlights}
    except Exception as e:
        print(f"[WARN] Claude 요약 생성 실패: {e}", file=sys.stderr)
        return None


NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def primary_link(r):
    """
    모바일에서 finance.naver.com 상세페이지 링크를 열면 네이버가 자체적으로
    m.stock.naver.com의 목록 화면으로 리다이렉트시켜버려 원하는 리포트가 안 열리는
    경우가 있다. stock.pstatic.net에 직접 호스팅된 PDF는 그런 리다이렉트 없이
    바로 열리므로, PDF가 있으면 PDF를 우선 링크로 쓴다.
    """
    return r.get("pdf") or r["link"]


def _link_list(reports, tier):
    """전체 리포트를 링크만 있는 간단한 형태로 렌더링."""
    lines = []
    note = FALLBACK_NOTES.get(tier)
    if note:
        lines.append(html.escape(note))
    if reports:
        for r in reports:
            lines.append(f'• <a href="{html.escape(primary_link(r), quote=True)}">{html.escape(r["title"])}</a>')
    else:
        lines.append("· 가져온 리포트 없음 (사이트 구조 변경 등으로 파싱 실패 가능성)")
    return lines


def build_message(hynix_reports, hynix_tier, industry_reports, industry_tier):
    today = datetime.date.today().strftime("%Y.%m.%d")
    lines = [f"<b>📊 SK 산업동향 브리핑 ({today})</b>"]

    summary = summarize_with_claude(hynix_reports, industry_reports)

    if summary and summary.get("overview"):
        lines.append(html.escape(summary["overview"]))

    lines.append("")

    if summary and summary.get("highlights"):
        lines.append("<b>🔎 오늘의 핵심 3</b>")
        for i, h in enumerate(summary["highlights"]):
            num = NUMBER_EMOJI[i] if i < len(NUMBER_EMOJI) else f"{i + 1}."
            title_escaped = html.escape(h["title"])
            title_html = (
                f'<a href="{html.escape(h["link"], quote=True)}"><b>{title_escaped}</b></a>'
                if h.get("link") else f"<b>{title_escaped}</b>"
            )
            lines.append(f"{num} {title_html}")
            lines.append(html.escape(h["detail"]))
            lines.append("")
    elif not os.environ.get("ANTHROPIC_API_KEY"):
        lines.append("※ ANTHROPIC_API_KEY를 등록하면 오늘의 핵심 리포트 3개를 뽑아 쉽게 설명해드려요.")
        lines.append("")

    lines.append(f"<b>🔧 SK하이닉스 리포트 전체 ({len(hynix_reports)}건)</b>")
    lines += _link_list(hynix_reports, hynix_tier)
    lines.append("")
    lines.append(f"<b>💡 반도체·IT서비스/AI 산업동향 전체 ({len(industry_reports)}건)</b>")
    lines += _link_list(industry_reports, industry_tier)

    lines.append("")
    lines.append("출근길 화이팅! 🚇")

    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
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
