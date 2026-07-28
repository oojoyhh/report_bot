import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main


class ResearchLinkTests(unittest.TestCase):
    def test_normalizes_relative_company_link_into_research_path(self):
        raw = "company_read.naver?nid=94143&page=1&searchType=itemCode&itemCode=000660"

        self.assertEqual(
            main.normalize_research_link(raw),
            "https://finance.naver.com/research/company_read.naver?nid=94143&page=1",
        )

    def test_repairs_root_level_absolute_link(self):
        raw = "https://finance.naver.com/company_read.naver?nid=94143&page=1"

        self.assertEqual(
            main.normalize_research_link(raw),
            "https://finance.naver.com/research/company_read.naver?nid=94143&page=1",
        )

    def test_parser_uses_clickable_research_detail_url(self):
        html_text = """
        <table>
          <tr>
            <td>SK하이닉스</td>
            <td><a href="company_read.naver?nid=94143&amp;page=1">시선을 약간만 아래로</a></td>
            <td>테스트증권</td>
            <td><a href="https://stock.pstatic.net/test-report.pdf">PDF</a></td>
            <td>26.07.14</td>
          </tr>
        </table>
        """

        reports = main.parse_research_table(
            html_text,
            "https://finance.naver.com/research/company_list.naver",
        )

        self.assertEqual(len(reports), 1)
        self.assertEqual(
            reports[0]["link"],
            "https://finance.naver.com/research/company_read.naver?nid=94143&page=1",
        )
        self.assertEqual(
            reports[0]["pdf"],
            "https://stock.pstatic.net/test-report.pdf",
        )


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.hynix_reports = [
            {
                "title": "Agent AI Ecosystem 중심에 서다",
                "org": "테스트증권",
                "date": "26.07.02",
                "link": "https://example.com/hynix",
                "pdf": "https://example.com/hynix.pdf",
            }
        ]
        self.industry_reports = [
            {
                "title": "국내 AI 데이터센터, 출발합니다",
                "org": "테스트증권",
                "date": "26.07.27",
                "link": "https://example.com/industry",
                "pdf": "https://example.com/industry.pdf",
            }
        ]

    def test_top_candidates_include_both_job_tracks(self):
        candidates = main.select_top_candidates(
            self.hynix_reports,
            self.industry_reports,
        )

        self.assertEqual({candidate["track"] for candidate in candidates}, {"hynix", "ax"})

    def test_top_candidates_skip_newer_report_without_pdf(self):
        industry_reports = [
            {
                "title": "최신이지만 PDF 없음",
                "org": "테스트증권",
                "date": "26.07.28",
                "link": "https://example.com/no-pdf",
                "pdf": None,
            },
            *self.industry_reports,
        ]

        candidates = main.select_top_candidates(
            self.hynix_reports,
            industry_reports,
        )

        ax_candidate = next(item for item in candidates if item["track"] == "ax")
        self.assertEqual(ax_candidate["title"], self.industry_reports[0]["title"])
        self.assertIsNotNone(ax_candidate["pdf"])

    def test_briefing_prompt_has_two_distinct_job_tracks(self):
        materials = [
            {
                "report": {**self.hynix_reports[0], "track": "hynix"},
                "body": "HBM 생산과 수율에 관한 실제 본문",
                "source_mode": "text",
            },
            {
                "report": {**self.industry_reports[0], "track": "ax"},
                "body": "기업 AI와 클라우드에 관한 실제 본문",
                "source_mode": "text",
            },
        ]

        prompt = main.build_briefing_prompt(materials)

        self.assertIn("SK하이닉스 양산관리", prompt)
        self.assertIn("SK AX", prompt)
        self.assertIn('"daily_concepts"', prompt)
        self.assertIn('"interviews"', prompt)
        self.assertIn('"quizzes"', prompt)
        self.assertIn("PDF에 나온 사실", prompt)
        self.assertIn("본문에 없는 수치나 사실은 만들지 말고", prompt)

    def test_visual_pdf_is_attached_before_prompt(self):
        materials = [{
            "report": {**self.hynix_reports[0], "track": "hynix"},
            "body": None,
            "source_mode": "visual_pdf",
        }]

        content = main.build_claude_content(materials)

        self.assertEqual(content[0]["type"], "document")
        self.assertEqual(content[0]["source"]["type"], "url")
        self.assertEqual(
            content[0]["source"]["url"],
            self.hynix_reports[0]["pdf"],
        )
        self.assertEqual(content[-1]["type"], "text")
        self.assertIn("PDF의 텍스트·표·차트를 직접 읽어", content[-1]["text"])

    def test_markdown_is_removed_from_model_summary(self):
        text = "# 오늘의 산업 동향\n\n**핵심 메시지:** 시장의 효율성이 중요합니다.\n- 면접 관점"

        self.assertEqual(
            main._clean(text),
            "오늘의 산업 동향\n\n핵심 메시지: 시장의 효율성이 중요합니다.\n면접 관점",
        )

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}, clear=False)
    @patch("main.extract_pdf_text", side_effect=[None, "기업 AI 실제 본문 " * 30])
    @patch("anthropic.Anthropic")
    def test_visual_pdf_evidence_survives_response_parsing(
        self,
        anthropic_client,
        extract_pdf_text,
    ):
        response = {
            "overview": "오늘의 흐름",
            "items": [
                {
                    "report_number": 1,
                    "beginner_summary": "차트형 하이닉스 PDF 해석",
                    "evidence": ["PDF 차트에서 확인한 사실"],
                    "role_impact": {
                        "areas": ["수율·품질"],
                        "explain": "양산관리 관점의 해석",
                    },
                },
                {
                    "report_number": 2,
                    "beginner_summary": "AI 리포트 해석",
                    "evidence": ["본문에서 확인한 사실"],
                    "role_impact": {
                        "areas": ["AI·데이터"],
                        "explain": "SK AX 관점의 해석",
                    },
                },
            ],
            "daily_concepts": {},
            "interviews": {},
            "quizzes": {},
        }
        anthropic_client.return_value.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(response, ensure_ascii=False))]
        )

        summary = main.summarize_with_claude(
            self.hynix_reports,
            self.industry_reports,
        )

        self.assertEqual(summary["items"][0]["source_mode"], "visual_pdf")
        self.assertEqual(
            summary["items"][0]["evidence"],
            ["PDF 차트에서 확인한 사실"],
        )
        sent_content = (
            anthropic_client.return_value.messages.create.call_args
            .kwargs["messages"][0]["content"]
        )
        self.assertEqual(sent_content[0]["type"], "document")
        self.assertEqual(extract_pdf_text.call_count, 2)

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}, clear=False)
    @patch("main.summarize_with_claude")
    def test_message_contains_both_job_tracks_and_visible_pdf_links(self, summarize):
        summarize.return_value = {
            "overview": "두 산업의 오늘 흐름입니다.",
            "items": [
                {
                    "report_number": 1,
                    "title": self.hynix_reports[0]["title"],
                    "track": "hynix",
                    "beginner_summary": "반도체 초보자용 해석입니다.",
                    "evidence": ["PDF에서 확인한 사실입니다."],
                    "role_impact": {
                        "areas": ["수율·품질"],
                        "explain": "양산관리 관점의 해석입니다.",
                    },
                    "source_mode": "text",
                    "link": self.hynix_reports[0]["pdf"],
                },
                {
                    "report_number": 2,
                    "title": self.industry_reports[0]["title"],
                    "track": "ax",
                    "beginner_summary": "AI 초보자용 해석입니다.",
                    "evidence": ["PDF에서 확인한 AI 사실입니다."],
                    "role_impact": {
                        "areas": ["AI·데이터"],
                        "explain": "SK AX 관점의 해석입니다.",
                    },
                    "source_mode": "text",
                    "link": self.industry_reports[0]["pdf"],
                },
            ],
            "daily_concepts": {
                "hynix": {
                    "term": "수율",
                    "meaning": "정상품의 비율",
                    "analogy": "붕어빵 비유",
                    "why_it_matters": "생산성과 원가에 연결되기 때문",
                },
                "ax": {
                    "term": "AIOps",
                    "meaning": "AI를 활용한 IT 운영",
                    "analogy": "자동 관제실 비유",
                    "why_it_matters": "서비스를 안정적으로 운영하기 때문",
                },
            },
            "interviews": {
                "hynix": {
                    "question": "수율이 떨어지면 무엇을 확인할까요?",
                    "thinking_steps": ["데이터를 확인한다.", "관련 부서와 원인을 찾는다."],
                    "sample_answer": "수율 데이터를 먼저 확인하겠습니다.",
                },
                "ax": {
                    "question": "고객에게 AI를 어떻게 적용할까요?",
                    "thinking_steps": ["고객 문제를 정의한다.", "데이터를 확인한다."],
                    "sample_answer": "고객 문제와 데이터를 먼저 확인하겠습니다.",
                },
            },
            "quizzes": {
                "hynix": {"question": "수율이란?", "answer": "정상품 비율"},
                "ax": {"question": "AIOps란?", "answer": "AI 기반 IT 운영"},
            },
        }

        message = main.build_message(
            self.hynix_reports,
            "recent",
            self.industry_reports,
            "recent",
        )

        self.assertIn("SK하이닉스 양산관리", message)
        self.assertIn("SK AX", message)
        self.assertIn("수율", message)
        self.assertIn("AIOps", message)
        self.assertIn("PDF 원문 열기", message)
        self.assertIn("https://example.com/hynix.pdf", message)
        self.assertIn("https://example.com/industry.pdf", message)

    def test_telegram_split_keeps_chunks_under_limit(self):
        text = "\n\n".join(f"<b>구역 {i}</b>\n" + ("내용" * 300) for i in range(12))
        chunks = main.split_telegram_message(text, limit=1000)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
