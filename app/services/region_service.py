import re


REGION_GROUPS = {
    "capital": {
        "label": "수도권",
        "aliases": ["서울", "서울시", "서울특별시", "경기", "경기도", "인천", "인천시", "인천광역시"],
    },
    "gangwon": {
        "label": "강원",
        "aliases": ["강원", "강원도", "강원특별자치도"],
    },
    "chungcheong": {
        "label": "충청",
        "aliases": ["충북", "충청북도", "충남", "충청남도", "대전", "대전시", "대전광역시", "세종", "세종시", "세종특별자치시"],
    },
    "honam": {
        "label": "호남",
        "aliases": ["전북", "전라북도", "전북특별자치도", "전남", "전라남도", "광주", "광주시", "광주광역시"],
    },
    "yeongnam": {
        "label": "영남",
        "aliases": [
            "경북",
            "경상북도",
            "경남",
            "경상남도",
            "대구",
            "대구시",
            "대구광역시",
            "부산",
            "부산시",
            "부산광역시",
            "울산",
            "울산시",
            "울산광역시",
        ],
    },
    "jeju": {
        "label": "제주",
        "aliases": ["제주", "제주도", "제주특별자치도"],
    },
}


REGION_ALIASES = [
    ("서울", ["서울특별시", "서울시", "서울"]),
    ("경기", ["경기도", "경기"]),
    ("인천", ["인천광역시", "인천시", "인천"]),
    ("강원", ["강원특별자치도", "강원도", "강원"]),
    ("충북", ["충청북도", "충북"]),
    ("충남", ["충청남도", "충남"]),
    ("대전", ["대전광역시", "대전시", "대전"]),
    ("세종", ["세종특별자치시", "세종시", "세종"]),
    ("전북", ["전북특별자치도", "전라북도", "전북"]),
    ("전남", ["전라남도", "전남"]),
    ("광주", ["광주광역시", "광주시", "광주"]),
    ("경북", ["경상북도", "경북"]),
    ("경남", ["경상남도", "경남"]),
    ("대구", ["대구광역시", "대구시", "대구"]),
    ("부산", ["부산광역시", "부산시", "부산"]),
    ("울산", ["울산광역시", "울산시", "울산"]),
    ("제주", ["제주특별자치도", "제주도", "제주"]),
]


def region_group_options() -> list[dict[str, str]]:
    return [{"value": key, "label": value["label"]} for key, value in REGION_GROUPS.items()]


def region_group_aliases(region_group: str | None) -> list[str]:
    if not region_group:
        return []
    return REGION_GROUPS.get(region_group, {}).get("aliases", [])


def region_group_label(region_group: str | None) -> str:
    if not region_group:
        return "전국"
    return REGION_GROUPS.get(region_group, {}).get("label", region_group)


def extract_regions(*values: str | None) -> list[str]:
    text = " ".join(value or "" for value in values)
    found = []
    for canonical, aliases in REGION_ALIASES:
        if any(_contains_alias(text, alias) for alias in aliases):
            found.append(canonical)
    return found


def _contains_alias(text: str, alias: str) -> bool:
    if len(alias) <= 2:
        pattern = rf"(?<![가-힣]){re.escape(alias)}(?=$|[^가-힣]|시|군|구|도|특별|광역)"
        return re.search(pattern, text) is not None
    return alias in text
