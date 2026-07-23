"""Sprint NEXT-14a/b — recall boost (retry union + tail window + ALWAYS_FIRE).

검증 대상:
- _retries: MV3_EXTRACTOR_GEMMA_RETRIES env (default 2, graceful invalid)
- _tail_turns: MV3_EXTRACTOR_TAIL_TURNS env (default 80, min 10)
- _always_fire: MV3_EXTRACTOR_ALWAYS_FIRE env (default off)
- _union_by_title: title 기준 dedup + 첫 등장 우선
- extract_from_jsonl retry: 0건 → retry → hit → union 반환
- extract_from_jsonl retry exhausted: 모두 0 → 빈 list
- extract_from_jsonl always_fire bypass: trigger False 인데 Gemma 호출
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

for _mod in ("memory_extractor",):
    sys.modules.pop(_mod, None)


class TestEnvHelpers(unittest.TestCase):
    def test_retries_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MV3_EXTRACTOR_GEMMA_RETRIES", None)
            sys.modules.pop("memory_extractor", None)
            from memory_extractor import _retries
            self.assertEqual(_retries(), 2)

    def test_retries_override(self):
        with patch.dict(os.environ, {"MV3_EXTRACTOR_GEMMA_RETRIES": "5"}):
            sys.modules.pop("memory_extractor", None)
            from memory_extractor import _retries
            self.assertEqual(_retries(), 5)

    def test_retries_invalid_graceful(self):
        with patch.dict(os.environ, {"MV3_EXTRACTOR_GEMMA_RETRIES": "garbage"}):
            sys.modules.pop("memory_extractor", None)
            from memory_extractor import _retries
            self.assertEqual(_retries(), 2)

    def test_retries_negative_clamped(self):
        with patch.dict(os.environ, {"MV3_EXTRACTOR_GEMMA_RETRIES": "-3"}):
            sys.modules.pop("memory_extractor", None)
            from memory_extractor import _retries
            self.assertEqual(_retries(), 0)

    def test_tail_turns_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MV3_EXTRACTOR_TAIL_TURNS", None)
            sys.modules.pop("memory_extractor", None)
            from memory_extractor import _tail_turns
            self.assertEqual(_tail_turns(), 80)

    def test_tail_turns_min_clamped(self):
        with patch.dict(os.environ, {"MV3_EXTRACTOR_TAIL_TURNS": "3"}):
            sys.modules.pop("memory_extractor", None)
            from memory_extractor import _tail_turns
            self.assertEqual(_tail_turns(), 10)

    def test_always_fire_default_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MV3_EXTRACTOR_ALWAYS_FIRE", None)
            sys.modules.pop("memory_extractor", None)
            from memory_extractor import _always_fire
            self.assertFalse(_always_fire())

    def test_always_fire_on(self):
        with patch.dict(os.environ, {"MV3_EXTRACTOR_ALWAYS_FIRE": "1"}):
            sys.modules.pop("memory_extractor", None)
            from memory_extractor import _always_fire
            self.assertTrue(_always_fire())


class TestUnionByTitle(unittest.TestCase):
    def test_dedup_first_wins(self):
        sys.modules.pop("memory_extractor", None)
        from memory_extractor import _union_by_title
        a = [{"title": "X", "body": "first"}]
        b = [{"title": "X", "body": "second"}, {"title": "Y", "body": "new"}]
        merged = _union_by_title(a, b)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["body"], "first", "첫 등장 우선")
        self.assertEqual(merged[1]["title"], "Y")

    def test_empty_lists(self):
        from memory_extractor import _union_by_title
        self.assertEqual(_union_by_title([], [], []), [])

    def test_skips_titleless(self):
        from memory_extractor import _union_by_title
        merged = _union_by_title([{"title": "", "body": "x"}, {"body": "y"}])
        self.assertEqual(merged, [])


def _make_jsonl(tmp: Path, sid: str = "test-sid") -> Path:
    """trigger 통과하는 최소 jsonl 만듦."""
    path = tmp / f"{sid}.jsonl"
    lines = [
        {"type": "user", "message": {"content": "이거 해줘"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "launchctl load -w foo.plist"}},
                ]
            },
        },
        {"type": "user", "message": {"content": "영구화 적용"}},
    ]
    path.write_text("\n".join(json.dumps(l) for l in lines))
    return path


class TestExtractRetry(unittest.TestCase):
    def _setup_module(self, env=None):
        env = env or {}
        with patch.dict(os.environ, env):
            sys.modules.pop("memory_extractor", None)
            import memory_extractor as me
            return me

    def test_first_hit_continues_one_more_for_union(self):
        """첫 attempt hit → 한 번 더 시도 (union 보강), 그 후 break."""
        call_count = {"n": 0}
        side_effects = [
            '[{"type":"procedural","title":"A","body":"a","reason":"r","evidence":"e"}]',
            '[{"type":"procedural","title":"B","body":"b","reason":"r","evidence":"e"}]',
            '[{"type":"procedural","title":"C","body":"c","reason":"r","evidence":"e"}]',
        ]

        def fake_gemma(prompt, **kw):
            n = call_count["n"]
            call_count["n"] += 1
            return side_effects[n] if n < len(side_effects) else None

        with tempfile.TemporaryDirectory() as tmp:
            jsonl = _make_jsonl(Path(tmp))
            with patch.dict(os.environ, {"MV3_EXTRACTOR_GEMMA_RETRIES": "2"}):
                sys.modules.pop("memory_extractor", None)
                import memory_extractor as me
                with patch.object(me, "call_gemma", side_effect=fake_gemma):
                    out = me.extract_from_jsonl(jsonl)
            # attempt 1 hit + attempt 2 hit → union [A, B], break before 3
            self.assertEqual(call_count["n"], 2)
            titles = {c["title"] for c in out}
            self.assertEqual(titles, {"A", "B"})

    def test_zero_then_hit_retries_until_hit(self):
        """첫 attempt 0건 → retry → hit. 누적 호출 = attempts max."""
        call_count = {"n": 0}
        side_effects = ["[]", "[]",
                        '[{"type":"procedural","title":"L","body":"l","reason":"r","evidence":"e"}]']

        def fake_gemma(prompt, **kw):
            n = call_count["n"]
            call_count["n"] += 1
            return side_effects[n] if n < len(side_effects) else None

        with tempfile.TemporaryDirectory() as tmp:
            jsonl = _make_jsonl(Path(tmp))
            with patch.dict(os.environ, {"MV3_EXTRACTOR_GEMMA_RETRIES": "2"}):
                sys.modules.pop("memory_extractor", None)
                import memory_extractor as me
                with patch.object(me, "call_gemma", side_effect=fake_gemma):
                    out = me.extract_from_jsonl(jsonl)
            # 0+0+hit. 3번째 호출에서 hit → 그게 first hit이므로 한 번 더?
            # i=2 일 때 i+1 < attempts (3 < 3 False) → break. 정확히 3 호출.
            self.assertEqual(call_count["n"], 3)
            self.assertEqual([c["title"] for c in out], ["L"])

    def test_all_zero_returns_empty(self):
        call_count = {"n": 0}

        def fake_gemma(prompt, **kw):
            call_count["n"] += 1
            return "[]"

        with tempfile.TemporaryDirectory() as tmp:
            jsonl = _make_jsonl(Path(tmp))
            with patch.dict(os.environ, {"MV3_EXTRACTOR_GEMMA_RETRIES": "2"}):
                sys.modules.pop("memory_extractor", None)
                import memory_extractor as me
                with patch.object(me, "call_gemma", side_effect=fake_gemma):
                    out = me.extract_from_jsonl(jsonl)
            self.assertEqual(call_count["n"], 3, "최초 + retry 2 = 3 호출")
            self.assertEqual(out, [])

    def test_retries_zero_disables(self):
        call_count = {"n": 0}

        def fake_gemma(prompt, **kw):
            call_count["n"] += 1
            return "[]"

        with tempfile.TemporaryDirectory() as tmp:
            jsonl = _make_jsonl(Path(tmp))
            with patch.dict(os.environ, {"MV3_EXTRACTOR_GEMMA_RETRIES": "0"}):
                sys.modules.pop("memory_extractor", None)
                import memory_extractor as me
                with patch.object(me, "call_gemma", side_effect=fake_gemma):
                    out = me.extract_from_jsonl(jsonl)
            self.assertEqual(call_count["n"], 1, "retries=0 → 호출 1회만")
            self.assertEqual(out, [])


class TestAlwaysFire(unittest.TestCase):
    def _no_trigger_jsonl(self, tmp: Path) -> Path:
        """trigger 통과 못 하는 jsonl (잡담)."""
        path = tmp / "no-trigger.jsonl"
        lines = [
            {"type": "user", "message": {"content": "안녕"}},
            {"type": "assistant", "message": {"content": "안녕!"}},
            {"type": "user", "message": {"content": "잘 지내?"}},
        ]
        path.write_text("\n".join(json.dumps(l) for l in lines))
        return path

    def test_no_trigger_default_returns_empty(self):
        call_count = {"n": 0}

        def fake_gemma(prompt, **kw):
            call_count["n"] += 1
            return '[{"type":"procedural","title":"X","body":"x","reason":"r","evidence":"e"}]'

        with tempfile.TemporaryDirectory() as tmp:
            jsonl = self._no_trigger_jsonl(Path(tmp))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MV3_EXTRACTOR_ALWAYS_FIRE", None)
                sys.modules.pop("memory_extractor", None)
                import memory_extractor as me
                with patch.object(me, "call_gemma", side_effect=fake_gemma):
                    out = me.extract_from_jsonl(jsonl)
            self.assertEqual(call_count["n"], 0, "trigger 미발화 → Gemma 호출 안 함")
            self.assertEqual(out, [])

    def test_always_fire_bypasses_trigger(self):
        call_count = {"n": 0}

        def fake_gemma(prompt, **kw):
            call_count["n"] += 1
            return '[{"type":"procedural","title":"Y","body":"y","reason":"r","evidence":"e"}]'

        with tempfile.TemporaryDirectory() as tmp:
            jsonl = self._no_trigger_jsonl(Path(tmp))
            with patch.dict(os.environ, {
                "MV3_EXTRACTOR_ALWAYS_FIRE": "1",
                "MV3_EXTRACTOR_GEMMA_RETRIES": "0",
            }):
                sys.modules.pop("memory_extractor", None)
                import memory_extractor as me
                with patch.object(me, "call_gemma", side_effect=fake_gemma):
                    out = me.extract_from_jsonl(jsonl)
            self.assertGreaterEqual(call_count["n"], 1, "ALWAYS_FIRE → Gemma 호출")
            self.assertEqual([c["title"] for c in out], ["Y"])


if __name__ == "__main__":
    unittest.main()
