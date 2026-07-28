import os
import unittest
from unittest.mock import patch

import main


class ResearchLinkTests(unittest.TestCase):
    def test_normalizes_relative_company_link_into_research_path(self):
        raw = "company_read.naver?nid=94143&page=1&searchType=itemCode&itemCode=000660"

        self.assertEqual(
            main.normalize_research_link(raw),
            "https://finance.naver.com/research/company_read.naver"
            "?nid=94143&page=1&searchType=itemCode&itemCode=000660",
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


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.hynix_reports = [
            {
                "title": "Agent AI Ecosystem 중심에 서다",
                "org": "테스트증권",
                "date": "26.07.02",
                "link": "https://example.com/hynix",
            }
        ]
        self.industry_reports = [
            {
                "title": "국내 AI 데이터센터, 출발합니다",
                "org": "테스트증권",
                "date": "26.07.27",
                "link": "https://example.com/industry",
            }
        ]

    def test_summary_prompt_requests_a_substantive_summary_only(self):
        prompt = main.build_summary_prompt(self.hynix_reports, self.industry_reports)

        self.assertIn("900~1,200자", prompt)
        self.assertIn("10~14개의 완결된 문장", prompt)
        self.assertIn("4개의 짧은 문단", prompt)
        self.assertIn('{"overview":', prompt)
        self.assertNotIn('"highlights"', prompt)

    def test_markdown_is_removed_from_model_summary(self):
        text = "# 오늘의 산업 동향\n\n**핵심 메시지:** 시장의 효율성이 중요합니다.\n- 면접 관점"

        self.assertEqual(
            main._clean_summary_text(text),
            "오늘의 산업 동향\n\n핵심 메시지: 시장의 효율성이 중요합니다.\n면접 관점",
        )

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}, clear=False)
    @patch("main.summarize_with_claude")
    def test_message_contains_only_one_rich_summary_section(self, summarize):
        summarize.return_value = {"overview": "첫 문단입니다.\n\n둘째 문단입니다."}

        message = main.build_message(
            self.hynix_reports,
            "recent",
            self.industry_reports,
            "recent",
        )

        self.assertIn("🗣 오늘의 핵심 요약", message)
        self.assertIn("첫 문단입니다.\n\n둘째 문단입니다.", message)
        self.assertNotIn("오늘의 핵심 리포트", message)


if __name__ == "__main__":
    unittest.main()
