"""Bundle the production skill and its references without private job data."""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path


def build() -> Path:
    project = Path(__file__).resolve().parents[1]
    plugin = project / "plugins/voicepeak-production"
    skill = plugin / "skills/voicepeak-production"
    references = skill / "references"
    references.mkdir(parents=True, exist_ok=True)
    description = "ローカルVOICEPEAKで原稿やPPTXノートから日本語ナレーションと静止動画を制作する。"
    interface = {
        "displayName": "VOICEPEAK音声制作",
        "shortDescription": "原稿とPPTXノートからナレーションを制作",
        "longDescription": description
        + " 読み・辞書・聴取結果・再開を管理する。このMacのvp-managerとVOICEPEAKが必要。",
        "developerName": "rioriost",
        "category": "Productivity",
        "capabilities": ["Read", "Write"],
        "defaultPrompt": [
            "このPPTXのノートから、読みを調整して確認用の音声付き動画を作ってください。",
            "このテキストの読みを確認し、VOICEPEAKでナレーションを作ってください。",
        ],
    }
    assert len(interface["shortDescription"]) <= 30
    manifest = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": "voicepeak-production",
        "version": tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"],
        "license": "MIT",
        "description": description,
        "author": {"name": "rioriost"},
        "extensions": {"com.openai": {"interface": interface}},
    }
    (plugin / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    compatibility = {key: manifest[key] for key in ("name", "version", "description", "author", "license")}
    compatibility.update(skills="./skills/", interface=interface)
    (plugin / ".codex-plugin").mkdir(exist_ok=True)
    (plugin / ".codex-plugin/plugin.json").write_text(
        json.dumps(compatibility, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    instructions = (project / ".agents/skills/voicepeak-production/SKILL.md").read_text(encoding="utf-8")
    for name in ("decisions", "references"):
        instructions = instructions.replace(f"../../../docs/{name}.md", f"references/{name}.md")
        content = (project / f"docs/{name}.md").read_text(encoding="utf-8")
        # Runtime implementation lives in PROJECT_ROOT, outside the plugin cache.
        for module in ("decisions", "lexicon"):
            content = content.replace(
                f"[`{module}.py`](../src/vp_manager/{module}.py)",
                f"`vp_manager.{module}`（本体の実装モジュール）",
            )
        content = content.replace("uv --directory /absolute/vp-manager run vp-manager", '"${VP_MANAGER[@]}"')
        content = content.replace("uv run vp-manager", '"${VP_MANAGER[@]}"')
        (references / f"{name}.md").write_text(content, encoding="utf-8")
    (skill / "SKILL.md").write_text(instructions, encoding="utf-8")
    shutil.copyfile(project / "LICENSE", plugin / "LICENSE")
    shutil.copyfile(project / "LICENSE", skill / "LICENSE")
    return plugin


if __name__ == "__main__":
    print(build())
