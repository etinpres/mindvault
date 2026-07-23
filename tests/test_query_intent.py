"""Sprint 16 — Query Intent Classifier 단위 테스트."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


class TestClassify(unittest.TestCase):
    def test_chat_greetings(self):
        from query_intent import classify
        for q in [
            "안녕하세요",
            "굿모닝!",
            "오늘 날씨 어때",
            "오늘 점심 뭐 먹지",
            "고마워요",
            "잘자",
        ]:
            r = classify(q)
            self.assertEqual(r.intent, "chat", f"{q!r} → {r}")

    def test_chat_short_fallback(self):
        from query_intent import classify
        # 짧고 단어 적음 → chat fallback
        r = classify("ㅎㅇ")
        self.assertEqual(r.intent, "chat")
        self.assertIn("short-fallback", r.matched)

    def test_meta(self):
        from query_intent import classify
        for q in [
            "너는 어떤 모델이야?",
            "context 얼마 남았어",
            "토큰 얼마 사용했어?",
            "현재 세션 정보",
            "claude code 버전이 뭐야",
        ]:
            r = classify(q)
            self.assertEqual(r.intent, "meta", f"{q!r} → {r}")

    def test_code(self):
        from query_intent import classify
        for q in [
            "이 함수 고쳐줘",
            "테스트 돌려봐",
            "src/memory_indexer.py 의 _collect_md_files 변경",
            "이 버그 fix 해",
            "commit 해줘",
            "PR 만들어줘",
            "타입 체크 돌려봐",
        ]:
            r = classify(q)
            self.assertEqual(r.intent, "code", f"{q!r} → {r}")

    def test_recall(self):
        from query_intent import classify
        for q in [
            "예전에 했던 얘기 뭐였지",
            "지난번에 어떤 모델 썼더라",
            "이전에 합의했던 거 기억나",
            "그때 만든 거 뭐였어?",
            "옛날에 어떻게 했지",
        ]:
            r = classify(q)
            self.assertEqual(r.intent, "recall", f"{q!r} → {r}")

    def test_unknown(self):
        from query_intent import classify
        # 명확한 의도 카테고리 없음 — 도메인 query 등
        for q in [
            "MindVault Sprint 진행 상황",
            "Arctic-ko 임베딩 분포",
            "임베딩 서버 응답 분석",
        ]:
            r = classify(q)
            self.assertEqual(r.intent, "unknown", f"{q!r} → {r}")

    def test_priority_recall_over_code(self):
        """recall + code 키워드 동시 — recall 우선."""
        from query_intent import classify
        r = classify("예전에 이 함수 고쳤었지")
        self.assertEqual(r.intent, "recall")

    def test_priority_code_over_meta(self):
        """code + meta — code 우선 (작업 의도)."""
        from query_intent import classify
        r = classify("claude code 라는 모델로 이 함수 고쳐")
        # 'claude code' 가 meta 매칭이지만 '이 함수 고쳐' 가 code 매칭 → recall 없음 → code 우선
        self.assertEqual(r.intent, "code")


class TestShouldSkipRecall(unittest.TestCase):
    def test_skip_chat_meta(self):
        from query_intent import IntentResult, should_skip_recall
        self.assertTrue(
            should_skip_recall(IntentResult("chat", 0.8, []))
        )
        self.assertTrue(
            should_skip_recall(IntentResult("meta", 0.9, []))
        )

    def test_keep_others(self):
        from query_intent import IntentResult, should_skip_recall
        for intent in ("recall", "code", "unknown"):
            self.assertFalse(
                should_skip_recall(IntentResult(intent, 0.5, [])),
                f"잘못된 skip: {intent}",
            )


class TestGemmaIntent(unittest.TestCase):
    def setUp(self):
        # post-ship: classify_with_gemma 가 file cache 사용 → 테스트 간 격리.
        # 각 테스트는 임시 DB로 cache 분리.
        import tempfile
        from pathlib import Path
        import query_intent
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = query_intent._GEMMA_CACHE_DB
        self._orig_init = query_intent._gemma_cache_initialized
        query_intent._GEMMA_CACHE_DB = Path(self._tmp.name) / "intent_cache.db"
        query_intent._gemma_cache_initialized = False

    def tearDown(self):
        import query_intent
        query_intent._GEMMA_CACHE_DB = self._orig_db
        query_intent._gemma_cache_initialized = self._orig_init
        self._tmp.cleanup()

    """Sprint NEXT-3 — Gemma 보강 classifier."""

    def test_gemma_intent_env_off_default(self):
        # Round 3 B3: patch.dict 로 caller-set MV3_GEMMA_INTENT 값 보존.
        # 직접 pop 시 export 된 value 가 silent 삭제 leak.
        from query_intent import gemma_intent_enabled
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MV3_GEMMA_INTENT", None)
            self.assertFalse(gemma_intent_enabled())

    def test_gemma_intent_env_on(self):
        from query_intent import gemma_intent_enabled
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"MV3_GEMMA_INTENT": "1"}, clear=False):
            self.assertTrue(gemma_intent_enabled())

    def test_gemma_intent_env_other_values_off(self):
        from query_intent import gemma_intent_enabled
        import os
        from unittest.mock import patch
        # Round 3 B3: for-loop assert 실패 시 leak 차단 — patch.dict context.
        for v in ("0", "true", "yes", ""):
            with patch.dict(os.environ, {"MV3_GEMMA_INTENT": v}, clear=False):
                self.assertFalse(
                    gemma_intent_enabled(), f"value={v!r} should be off"
                )

    def test_normalize_gemma_label_valid(self):
        from query_intent import _normalize_gemma_label
        cases = [
            ("chat", "chat"),
            ("CHAT", "chat"),
            ("chat\n", "chat"),
            ("meta-conversation", "meta"),
            ("**code**", "code"),
            ("recall", "recall"),
            ("other", "other"),
        ]
        for raw, expect in cases:
            self.assertEqual(
                _normalize_gemma_label(raw),
                expect,
                f"raw={raw!r}",
            )

    def test_normalize_gemma_label_invalid(self):
        from query_intent import _normalize_gemma_label
        for raw in (None, "", "blah", "12345", "한글만"):
            self.assertIsNone(_normalize_gemma_label(raw), f"raw={raw!r}")

    def test_classify_with_gemma_too_long_returns_none(self):
        from query_intent import classify_with_gemma
        long_q = "x" * 100  # > GEMMA_INTENT_MAX_LEN (40)
        self.assertIsNone(classify_with_gemma(long_q))

    def test_classify_with_gemma_empty_returns_none(self):
        from query_intent import classify_with_gemma
        self.assertIsNone(classify_with_gemma(""))
        self.assertIsNone(classify_with_gemma("   "))

    def test_classify_with_gemma_chat_label(self):
        """Gemma 가 chat 반환 → IntentResult chat 으로 반환."""
        import query_intent
        from unittest.mock import patch
        with patch.object(query_intent, "_call_gemma_intent", return_value="chat"):
            r = query_intent.classify_with_gemma("뭐해")
        self.assertIsNotNone(r)
        self.assertEqual(r.intent, "chat")
        self.assertIn("gemma:chat", r.matched)

    def test_classify_with_gemma_meta_label(self):
        import query_intent
        from unittest.mock import patch
        with patch.object(query_intent, "_call_gemma_intent", return_value="meta"):
            r = query_intent.classify_with_gemma("너 누구야")
        self.assertIsNotNone(r)
        self.assertEqual(r.intent, "meta")

    def test_classify_with_gemma_other_label_returns_none(self):
        """other 는 unknown 과 동의 — None 반환해 rule-based 결과 유지."""
        import query_intent
        from unittest.mock import patch
        with patch.object(query_intent, "_call_gemma_intent", return_value="other"):
            r = query_intent.classify_with_gemma("뭔가 작업 지시")
        self.assertIsNone(r)

    def test_classify_with_gemma_gemma_failure_returns_none(self):
        """Gemma 서버 다운 등 None 반환 → rule-based 폴백."""
        import query_intent
        from unittest.mock import patch
        with patch.object(query_intent, "_call_gemma_intent", return_value=None):
            r = query_intent.classify_with_gemma("뭐해")
        self.assertIsNone(r)

    def test_classify_with_gemma_invalid_label_returns_none(self):
        import query_intent
        from unittest.mock import patch
        with patch.object(query_intent, "_call_gemma_intent", return_value="bogus"):
            r = query_intent.classify_with_gemma("뭐해")
        self.assertIsNone(r)

    def test_call_gemma_intent_propagates_non_network_exceptions(self):
        """Codex P2 fix: _call_gemma_intent 가 광범위 except Exception 안 쓰도록 좁혔다 —
        hook 의 SIGALRM _Timeout 같은 외부 Exception 은 통과해야 함."""
        import query_intent
        from unittest.mock import patch

        class _FakeHookTimeout(Exception):
            pass

        # urlopen 직전에 hook timeout 시뮬레이션. 좁은 except 만으로는 잡히지 않아야 한다.
        with patch.object(
            query_intent.urllib.request, "urlopen", side_effect=_FakeHookTimeout()
        ):
            with self.assertRaises(_FakeHookTimeout):
                query_intent._call_gemma_intent("test")
