"""
SK Report Bot
매일 아침 SK하이닉스(반도체/메모리) + IT서비스/AI(SK AX 관련 업종) 증권사 리포트를 모아
텔레그램으로 보내는 스크립트.

GitHub Actions 등 일반 인터넷 접속이 가능한 환경에서 실행하는 것을 전제로 합니다.

환경변수:
  TELEGRAM_BOT_TOKEN   (필수) 텔레그램 봇 토큰
  TELEGRAM_CHAT_ID     (필수) 메시지를 받을 chat id
  ANTHROPIC_API_KEY    (선택) 있으면 상위 3개 리포트를 실제로 읽고 쉬운 말로 요약 추가
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
PDF_URL_RE = re.compile(r"https?://[^\s\"'<>]+\.pdf", re.IGNORECASE)


def normalize_research_link(raw_href: str):
    """
    네이버 리서치 상세 링크를 /research/<read_type>.naver?nid=..&page=.. 형태로 정규화.
    목록 페이지 앵커에 붙어오는 searchType/itemCode 같은 부가 파라미터를 그대로 옮기면
    상세페이지가 정상적으로 안 열리고 메인/목록 화면으로 리다이렉트되는 경우가 있어
    nid/page만 남긴다.
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

            # 첨부 PDF 링크 찾기: href만이 아니라 행 전체 HTML(onclick 등 포함)을
            # 문자열로 훑어서 실제 pdf 주소 패턴을 직접 찾는다. 네이버가 href가 아니라
            # 자바스크립트 클릭 핸들러 안에 pdf 주소를 넣어두는 경우가 있기 때문.
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


def _clean(text: str, max_chars=None) -> str:
    """모델이 실수로 붙인 마크다운 표식을 제거하고, 필요하면 길이를 제한."""
    text = (text or "").replace("\r\n", "\n")
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
PDF_TEXT_MAX_CHARS = 6000
PDF_MAX_BYTES = 15 * 1024 * 1024
PDF_MIN_LETTER_CHARS = 120  # 이보다 글자가 적으면 차트/이미지 위주 PDF로 간주


def primary_link(report):
    """모바일에서도 리다이렉트 없이 바로 열리는 PDF 직접 링크를 우선한다."""
    return report.get("pdf") or report["link"]


def extract_pdf_text(pdf_url: str):
    """
    PDF를 내려받아 본문 텍스트를 추출.
    글자 수가 너무 적으면(차트/스캔 위주) None을 반환해서, 이후 단계에서
    Claude가 PDF를 이미지로 직접 읽도록(visual) 넘긴다.
    """
    try:
        import pypdf

        resp = requests.get(pdf_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        content = resp.content
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
            raise ValueError(f"의미 있는 글자가 {letter_count}자로 부족한 차트·이미지형 PDF")
        return text[:PDF_TEXT_MAX_CHARS]
    except Exception as e:
        print(f"[WARN] PDF 텍스트 추출 실패 ({pdf_url}): {e}", file=sys.stderr)
        return None


def select_top_candidates(hynix_reports, industry_reports, n=3):
    """
    하이닉스/산업동향 양쪽에서 최소 1개씩은 포함되도록 우선 선정하고,
    남은 자리는 PDF 유무와 최근성을 기준으로 채운다.
    """
    def best(pool):
        if not pool:
            return None
        return next((r for r in pool if r.get("pdf")), pool[0])

    selected = [r for r in (best(hynix_reports), best(industry_reports)) if r is not None][:n]
    remaining = [r for r in (hynix_reports + industry_reports) if r not in selected]
    remaining.sort(
        key=lambda r: (bool(r.get("pdf")), parse_date(r.get("date", "")) or datetime.date.min),
        reverse=True,
    )
    selected.extend(remaining[: max(0, n - len(selected))])
    return selected[:n]


def build_summary_prompt(materials):
    """리포트별 실제 본문(또는 본문 추출 실패 안내)을 포함한 요약 프롬프트."""
    blocks = []
    for i, m in enumerate(materials, start=1):
        r = m["report"]
        header = f"[리포트 {i}] {r['title']} ({r['org']}, {r['date']})"
        if m["body"]:
            blocks.append(f"{header}\n본문:\n{m['body']}")
        elif m["source_mode"] == "visual_pdf":
            blocks.append(f"{header}\n(텍스트 추출은 안 됐지만 PDF 원문이 아래 첨부되어 있음 - 직접 읽고 요약할 것)")
        else:
            blocks.append(f"{header}\n(본문 확인 불가 - 제목/증권사 정보만으로 합리적으로 추정)")

    return (
        "너는 SK그룹(특히 SK하이닉스, SK AX) 취업·이직·면접을 준비하는 사람에게 "
        "출근길 지하철에서 1~2분 안에 훑어볼 아침 브리핑을 써주는 코치야. "
        "PDF를 직접 열어볼 필요 없이 핵심만 딱 눈에 들어오게 정리해줘. "
        "단순히 정보를 나열하지 말고, 이 사람이 면접에서 실제로 써먹을 수 있게 "
        "'그래서 이게 왜 중요한지'까지 연결해줘.\n\n"
        + "\n\n---\n\n".join(blocks)
        + "\n\n아래 JSON 형식으로만 응답해. 다른 설명이나 코드블록 없이 JSON 객체 하나만:\n"
        "{\n"
        '  "overview": "오늘 전체 흐름을 한 문장으로 (짧고 임팩트있게)",\n'
        '  "items": [\n'
        "    {\n"
        '      "title": "위 [리포트 N]의 제목을 정확히 그대로 복사",\n'
        '      "key_points": ["실제 본문에 나온 핵심 내용을 짧은 문장으로", "최대 3개까지"],\n'
        '      "concept": {"term": "본문에 나온 용어 중 취준생이 모를 만한 것 하나", "explain": "그 용어를 한 문장으로 쉽게 풀이"},\n'
        '      "interview_note": "이 내용을 면접에서 어떻게 언급하면 좋을지, 실제로 말하듯 1문장으로 (예: \'~라고 답하면서 ~를 강조할 수 있어요\')"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "items는 입력된 리포트 개수만큼 순서대로. "
        "concept은 마땅한 용어가 없으면 그 리포트의 concept 필드를 통째로 생략해도 됨. "
        "interview_note는 매 리포트마다 반드시 채우고, 뻔한 말 말고 이 리포트 내용과 구체적으로 연결해서 써. "
        "본문에 없는 수치나 사실은 단정하지 말고, 마크다운 문법(**, #, - 등)은 절대 쓰지 마."
    )


def build_claude_content(materials, prompt_text):
    """차트/이미지 위주라 텍스트 추출이 안 된 PDF는 문서 블록으로 직접 첨부해 Claude가 읽게 한다."""
    content = []
    for i, m in enumerate(materials, start=1):
        if m["source_mode"] != "visual_pdf":
            continue
        r = m["report"]
        content.append({
            "type": "document",
            "source": {"type": "url", "url": r["pdf"]},
            "title": f"리포트 {i}: {r['title']}",
        })
    content.append({"type": "text", "text": prompt_text})
    return content


def summarize_with_claude(hynix_reports, industry_reports):
    """
    ANTHROPIC_API_KEY가 있으면 상위 3개 리포트의 실제 본문을 읽어서
    { "overview": "...", "items": [{"title","key_points":[...],"concept":{...}?}] } 를 생성.
    키가 없거나 후보가 없으면 None.
    """
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
    for r in candidates:
        body = extract_pdf_text(r["pdf"]) if r.get("pdf") else None
        source_mode = "text" if body else ("visual_pdf" if r.get("pdf") else "unavailable")
        materials.append({"report": r, "body": body, "source_mode": source_mode})

    prompt_text = build_summary_prompt(materials)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4000,
            messages=[{"role": "user", "content": build_claude_content(materials, prompt_text)}],
        )
        if message.stop_reason == "max_tokens":
            print("[WARN] Claude 응답이 max_tokens에서 잘렸을 수 있음", file=sys.stderr)
        data = json.loads(_strip_code_fence(message.content[0].text))

        link_by_title = {m["report"]["title"]: primary_link(m["report"]) for m in materials}
        items = []
        for it in data.get("items", [])[:len(candidates)]:
            title = _clean(it.get("title", ""), 200)
            key_points = [_clean(p, 220) for p in it.get("key_points", []) if _clean(p, 220)][:3]
            if not title or not key_points:
                continue
            concept = it.get("concept") or None
            if concept:
                term = _clean(concept.get("term", ""), 80)
                explain = _clean(concept.get("explain", ""), 200)
                concept = {"term": term, "explain": explain} if term and explain else None
            interview_note = _clean(it.get("interview_note", ""), 220) or None
            items.append({
                "title": title,
                "key_points": key_points,
                "concept": concept,
                "interview_note": interview_note,
                "link": link_by_title.get(title),
            })

        return {"overview": _clean(data.get("overview", ""), 300) or None, "items": items}
    except Exception as e:
        print(f"[WARN] Claude 요약 생성 실패: {e}", file=sys.stderr)
        return None


def _report_links(title_line, reports, tier):
    """전체 리포트 목록: 제목 + (PDF 원문 열기 / 상세 보기) 링크."""
    lines = [title_line]
    note = FALLBACK_NOTES.get(tier)
    if note:
        lines.append(html.escape(note))
    if not reports:
        lines.append("· 가져온 리포트 없음 (사이트 구조 변경 등으로 파싱 실패 가능성)")
        return lines
    for r in reports:
        link = html.escape(primary_link(r), quote=True)
        label = "PDF 원문 열기" if r.get("pdf") else "상세 보기"
        lines.append(f'• {html.escape(r["title"])} (<a href="{link}">{label}</a>)')
    return lines


def build_message(hynix_reports, hynix_tier, industry_reports, industry_tier):
    today = datetime.date.today().strftime("%Y.%m.%d")
    lines = [f"<b>📊 SK 산업동향 브리핑 ({today})</b>"]

    summary = summarize_with_claude(hynix_reports, industry_reports)

    if summary and summary.get("overview"):
        lines.append(html.escape(summary["overview"]))

    lines.append("")

    if summary and summary.get("items"):
        lines.append("<b>🔎 오늘의 핵심 리포트</b>")
        lines.append("")
        for i, it in enumerate(summary["items"]):
            num = NUMBER_EMOJI[i] if i < len(NUMBER_EMOJI) else f"{i + 1}."
            title_escaped = html.escape(it["title"])
            title_html = (
                f'<a href="{html.escape(it["link"], quote=True)}"><b>{title_escaped}</b></a>'
                if it.get("link") else f"<b>{title_escaped}</b>"
            )
            lines.append(f"{num} {title_html}")
            for kp in it["key_points"]:
                lines.append(f"  · {html.escape(kp)}")
            if it.get("concept"):
                term = html.escape(it["concept"]["term"])
                explain = html.escape(it["concept"]["explain"])
                lines.append(f"  💡 <b>{term}</b>: {explain}")
            if it.get("interview_note"):
                lines.append(f"  🎯 <b>면접 연결</b>: {html.escape(it['interview_note'])}")
            lines.append("")
    elif not os.environ.get("ANTHROPIC_API_KEY"):
        lines.append("※ ANTHROPIC_API_KEY를 등록하면 오늘의 핵심 리포트를 실제로 읽고 요약해드려요.")
        lines.append("")

    lines += _report_links(f"<b>🔧 SK하이닉스 리포트 전체 ({len(hynix_reports)}건)</b>", hynix_reports, hynix_tier)
    lines.append("")
    lines += _report_links(
        f"<b>💡 반도체·IT서비스/AI 산업동향 전체 ({len(industry_reports)}건)</b>",
        industry_reports,
        industry_tier,
    )

    lines.append("")
    lines.append("출근길 화이팅! 🚇")

    return "\n".join(lines)


def split_telegram_message(text: str, limit=3800):
    """HTML 태그 중간이 잘리지 않도록 빈 줄/줄 단위로 메시지를 나눈다."""
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
