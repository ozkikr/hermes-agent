from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "update_local_patches_release.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "update_local_patches_release", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_github_repo_accepts_ssh_and_https():
    module = _load_module()

    assert module.parse_github_repo("git@github.com:NousResearch/hermes-agent.git") == (
        "NousResearch",
        "hermes-agent",
    )
    assert module.parse_github_repo("https://github.com/NousResearch/hermes-agent") == (
        "NousResearch",
        "hermes-agent",
    )


def test_build_backup_branch_name_sanitizes_tag_and_appends_suffix():
    module = _load_module()

    name = module.build_backup_branch_name(
        "local-patches",
        "release/v2026.4.8",
        today=date(2026, 4, 12),
        existing_refs={
            "backup/local-patches-pre-release-v2026.4.8-20260412",
        },
    )

    assert name == "backup/local-patches-pre-release-v2026.4.8-20260412-2"


def test_build_backup_branch_name_uses_first_available_name():
    module = _load_module()

    name = module.build_backup_branch_name(
        "feature/patches",
        "v2026.4.8",
        today=date(2026, 4, 12),
        existing_refs=set(),
    )

    assert name == "backup/feature-patches-pre-v2026.4.8-20260412"
