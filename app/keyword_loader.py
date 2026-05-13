import json
from pathlib import Path


DEFAULT_KEYWORDS = {
    "breaking": ["속보", "긴급", "재난", "화재", "사망", "북한", "미사일"],
    "broadcast": ["영상", "CCTV", "목격", "현장", "브리핑"],
    "verification": ["SNS", "허위", "조작", "논란", "확인"],
}


def load_keyword_groups(path: Path = Path("config/keywords.json")) -> dict[str, list[str]]:
    if not path.exists():
        return DEFAULT_KEYWORDS
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {str(name): [str(keyword) for keyword in keywords] for name, keywords in payload.items()}

