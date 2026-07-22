"""배포용 Codex 자산(codex/)의 개인 환경 참조 차단 가드.

v4.0.0 push 전 점검에서 close-session SKILL 이 사용자 개인 인프라(Graphify,
Obsidian vault)를 참조하던 결함 발견 — 신규 사용자에게 존재하지 않는 시스템.
배포 자산은 MindVault 코어 외 어떤 개인 도구도 가정하지 않는다.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED = sorted((REPO_ROOT / "codex").rglob("*.md"))

FORBIDDEN = [
    "yonghaekim",
    "dr.ocean",
    "knowledge-hub",
    "Graphify",
    "graphify",
    "Obsidian",
    "obsidian",
]


def test_codex_assets_exist():
    assert SHIPPED, "codex/ 배포 자산이 없음"


def test_codex_assets_no_personal_infra():
    for f in SHIPPED:
        body = f.read_text(encoding="utf-8")
        for word in FORBIDDEN:
            assert word not in body, f"{f.name}: 개인 환경 참조 '{word}' 잔존"
