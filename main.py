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
import datetime
from urllib.parse import urljoin

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


def parse_research_table(html_text: str, base_url: str):
    """네이버금융 research 목록 테이블(class='type_1')을 파싱."""
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.find("table", class_="type_1")
    if table is None:
        return []

    reports = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        title_tag = tds[0].find("a")
        if title_tag is None:
            continue
        title = title_tag.get_text(strip=True)
        if not title:
            continue
        link = urljoin(base_url, title_tag.get("href", ""))
        org = tds[1].get_text(strip=True)
        pdf_tag = tds[2].find("a")
        pdf_link = urljoin(base_url, pdf_tag.get("href", "")) if pdf_tag else None
        date_str = tds[3].get_text(strip=True)
        reports.append({
            "title": title,
            "org": org,
            "date": date_str,
            "link": link,
            "pdf": pdf_link,
        })
    return reports


def fetch_hynix_reports():
    url = f"{NAVER_BASE}/research/company_list.naver?searchType=itemCode&itemCode={HYNIX_CODE}"
    try:
        html_text = fetch_html(url)
    except Exception as e:
        print(f"[WARN] SK하이닉스 리포트 조회 실패: {e}", file=sys.stderr)
        return []
    reports = parse_research_table(html_text, NAVER_BASE)
    return [r for r in reports if is_recent(r["date"])]


def fetch_industry_reports():
    url = f"{NAVER_BASE}/research/industry_list.naver"
    try:
        html_text = fetch_html(url)
    except Exception as e:
        print(f"[WARN] 산업분석 리포트 조회 실패: {e}", file=sys.stderr)
        return []
    reports = parse_research_table(html_text, NAVER_BASE)
    matched = []
    for r in reports:
        if not is_recent(r["date"]):
            continue
        if any(kw in r["title"] for kw in INDUSTRY_KEYWORDS):
            matched.append(r)
    return matched


def summarize_with_claude(hynix_reports, industry_reports):
    """ANTHROPIC_API_KEY가 있으면 제목들을 재료로 쉬운 설명을 생성. 실패하면 None."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        print("[WARN] anthropic 패키지가 설치되어 있지 않아 요약을 건너뜁니다.", file=sys.stderr)
        return None

    titles = [r["title"] for r in hynix_reports + industry_reports]
    if not titles:
        return None

    prompt = (
        "너는 SK그룹(특히 SK하이닉스, SK AX) 취업/이직/면접을 준비하는 사람에게 "
        "매일 아침 산업 동향을 쉽게 설명해주는 코치야.\n"
        "아래는 오늘 나온 반도체·메모리 및 IT서비스·AI 관련 증권사/산업 리포트 제목 목록이야.\n\n"
        + "\n".join(f"- {t}" for t in titles)
        + "\n\n이 제목들만 보고 오늘의 핵심 흐름을 취준생이 이해하기 쉽게 3~4문장으로 설명해줘. "
        "전문용어는 풀어서 설명하고, 면접에서 언급하면 좋을 포인트가 있으면 마지막에 한 줄 덧붙여줘."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception as e:
        print(f"[WARN] Claude 요약 생성 실패: {e}", file=sys.stderr)
        return None


def build_message(hynix_reports, industry_reports):
    today = datetime.date.today().strftime("%Y.%m.%d")
    lines = [f"<b>📊 SK 산업동향 브리핑 ({today})</b>", ""]

    summary = summarize_with_claude(hynix_reports, industry_reports)
    if summary:
        lines.append("<b>🗣 오늘의 요약</b>")
        lines.append(summary)
        lines.append("")

    lines.append(f"<b>🔧 SK하이닉스 관련 리포트 ({len(hynix_reports)}건)</b>")
    if hynix_reports:
        for r in hynix_reports:
            lines.append(f'• <a href="{r["link"]}">{r["title"]}</a> — {r["org"]} ({r["date"]})')
    else:
        lines.append("· 최근 신규 리포트 없음")
    lines.append("")

    lines.append(f"<b>💡 반도체·IT서비스/AI 산업동향 ({len(industry_reports)}건)</b>")
    if industry_reports:
        for r in industry_reports:
            lines.append(f'• <a href="{r["link"]}">{r["title"]}</a> — {r["org"]} ({r["date"]})')
    else:
        lines.append("· 최근 신규 리포트 없음")

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

    hynix_reports = fetch_hynix_reports()
    industry_reports = fetch_industry_reports()

    message = build_message(hynix_reports, industry_reports)

    if dry_run:
        print(message)
        return

    send_telegram(token, chat_id, message)
    print("전송 완료.")


if __name__ == "__main__":
    main()
