"""Deterministic query-intent classifier tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


class TestClassify(unittest.TestCase):
    def test_chat_greetings(self):
        from query_intent import classify
        for query in [
            "안녕하세요",
            "굿모닝!",
            "오늘 날씨 어때",
            "오늘 점심 뭐 먹지",
            "고마워요",
            "잘자",
        ]:
            self.assertEqual(classify(query).intent, "chat")

    def test_chat_short_fallback(self):
        from query_intent import classify
        result = classify("ㅎㅇ")
        self.assertEqual(result.intent, "chat")
        self.assertIn("short-fallback", result.matched)

    def test_meta(self):
        from query_intent import classify
        for query in [
            "너는 어떤 모델이야?",
            "context 얼마 남았어",
            "토큰 얼마 사용했어?",
            "현재 세션 정보",
            "claude code 버전이 뭐야",
        ]:
            self.assertEqual(classify(query).intent, "meta")

    def test_code(self):
        from query_intent import classify
        for query in [
            "이 함수 고쳐줘",
            "테스트 돌려봐",
            "src/memory_indexer.py 의 _collect_md_files 변경",
            "이 버그 fix 해",
            "commit 해줘",
            "PR 만들어줘",
            "타입 체크 돌려봐",
        ]:
            self.assertEqual(classify(query).intent, "code")

    def test_recall(self):
        from query_intent import classify
        for query in [
            "예전에 했던 얘기 뭐였지",
            "지난번에 어떤 모델 썼더라",
            "이전에 합의했던 거 기억나",
            "그때 만든 거 뭐였어?",
            "옛날에 어떻게 했지",
        ]:
            self.assertEqual(classify(query).intent, "recall")

    def test_unknown(self):
        from query_intent import classify
        for query in [
            "MindVault Sprint 진행 상황",
            "Arctic-ko 임베딩 분포",
            "임베딩 서버 응답 분석",
        ]:
            self.assertEqual(classify(query).intent, "unknown")

    def test_priority_recall_over_code(self):
        from query_intent import classify
        self.assertEqual(classify("예전에 이 함수 고쳤었지").intent, "recall")

    def test_priority_code_over_meta(self):
        from query_intent import classify
        self.assertEqual(
            classify("claude code 라는 모델로 이 함수 고쳐").intent,
            "code",
        )

    def test_no_llm_fallback_symbols(self):
        import query_intent
        self.assertFalse(hasattr(query_intent, "classify_with_gemma"))
        self.assertFalse(hasattr(query_intent, "gemma_intent_enabled"))


class TestShouldSkipRecall(unittest.TestCase):
    def test_skip_chat_meta(self):
        from query_intent import IntentResult, should_skip_recall
        self.assertTrue(should_skip_recall(IntentResult("chat", 0.8, [])))
        self.assertTrue(should_skip_recall(IntentResult("meta", 0.9, [])))

    def test_keep_others(self):
        from query_intent import IntentResult, should_skip_recall
        for intent in ("recall", "code", "unknown"):
            self.assertFalse(should_skip_recall(IntentResult(intent, 0.5, [])))
