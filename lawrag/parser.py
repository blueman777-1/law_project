"""lawService.do XML → 조문 → 청크 → 검색 토큰.

청크 단위를 조(條)로 잡는 이유: 고정 길이로 자르면 "제8조 제2항"이 두 청크로
갈려 인용이 불가능해진다. 900자를 넘으면 항(項) 경계에서만 자른다.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

# 항번호로 쓰이는 원문자. 청크는 이 문자로 시작하는 지점에서만 잘린다.
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

_WORD = re.compile(r"[0-9A-Za-z가-힣]+")
_HANGUL = re.compile(r"[가-힣]+")


@dataclass(frozen=True)
class Law:
    law_id: str
    name: str
    law_type: str
    dept: str
    promulgated: str  # YYYYMMDD
    enforced: str  # YYYYMMDD


@dataclass(frozen=True)
class Article:
    no: int
    branch: int  # 가지번호. 제18조의2 면 2, 없으면 0
    title: str
    body: str
    enforced: str
    sha256: str

    @staticmethod
    def hash_body(body: str) -> str:
        """개정 감지용. 본문이 그대로면 재임베딩을 건너뛴다."""
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @property
    def label(self) -> str:
        return f"제{self.no}조의{self.branch}" if self.branch else f"제{self.no}조"


@dataclass(frozen=True)
class Chunk:
    header: str
    text: str
    part: int
    n_parts: int


def _text(el: ET.Element | None, tag: str) -> str:
    if el is None:
        return ""
    found = el.find(tag)
    return (found.text or "").strip() if found is not None else ""


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _blocks(unit: ET.Element) -> list[str]:
    """조문을 '자를 수 있는 최소 단위'들로 쪼갠다.

    blocks[0] 은 조문내용(제목 줄 또는 본문 인라인), 그 뒤는 항 하나씩.
    항내용에는 이미 항번호(①)가 포함돼 있으므로 따로 붙이지 않는다.
    항번호가 비어 있고 호만 있는 조문(예: 제2조 정의)도 있다.
    """
    out = [_text(unit, "조문내용")]
    for para in unit.findall("항"):
        lines = [_text(para, "항내용")]
        lines += [_text(ho, "호내용") for ho in para.findall("호")]
        block = "\n".join(line for line in lines if line)
        if block:
            out.append(block)
    return [b for b in out if b]


def parse_law(xml: bytes) -> tuple[Law, list[Article]]:
    root = ET.fromstring(xml)
    basic = root.find("기본정보")
    law = Law(
        law_id=_text(basic, "법령ID"),
        name=_text(basic, "법령명_한글"),
        law_type=_text(basic, "법종구분"),
        dept=_text(basic, "소관부처"),
        promulgated=_text(basic, "공포일자"),
        enforced=_text(basic, "시행일자"),
    )

    articles: list[Article] = []
    for unit in root.findall(".//조문단위"):
        # 조문여부='전문' 은 조문이 아니라 장 제목("제1장 총칙")이다. 걸러내지 않으면
        # 청크에 알맹이 없는 헤더가 섞여 검색 품질이 떨어진다.
        if _text(unit, "조문여부") != "조문":
            continue
        body = "\n".join(_blocks(unit))
        articles.append(
            Article(
                no=_int(_text(unit, "조문번호")),
                branch=_int(_text(unit, "조문가지번호")),
                title=_text(unit, "조문제목"),
                body=body,
                enforced=_text(unit, "조문시행일자") or law.enforced,
                sha256=Article.hash_body(body),
            )
        )
    return law, articles


def chunk_article(law_name: str, article: Article, max_chars: int = 900) -> list[Chunk]:
    """조 단위 청킹. 900자를 넘으면 항 경계에서만 자른다.

    항 하나가 통째로 max_chars 를 넘어도 쪼개지 않는다 — 인용 가능성이
    청크 크기 균일함보다 중요하다.
    """
    header = f"{law_name} {article.label}({article.title})" if article.title else f"{law_name} {article.label}"
    blocks = _split_blocks(article.body)

    parts: list[str] = []
    current: list[str] = []
    for block in blocks:
        splittable = bool(current) and block.startswith(tuple(CIRCLED))
        if splittable and len("\n".join(current)) + len(block) > max_chars:
            parts.append("\n".join(current))
            current = [block]
        else:
            current.append(block)
    if current:
        parts.append("\n".join(current))

    return [
        Chunk(header=header, text=f"{header}\n{part}", part=i, n_parts=len(parts))
        for i, part in enumerate(parts)
    ]


def _split_blocks(body: str) -> list[str]:
    """본문을 항 경계로 되돌린다. 항번호로 시작하는 줄이 새 블록의 시작."""
    blocks: list[str] = []
    for line in body.split("\n"):
        if blocks and not line.startswith(tuple(CIRCLED)):
            blocks[-1] += "\n" + line
        else:
            blocks.append(line)
    return blocks


def bigram_tokens(text: str) -> str:
    """한국어 키워드 검색용 토큰열 → to_tsvector('simple') 에 넣는다.

    PostgreSQL 기본 FTS 는 한국어를 공백 단위로만 끊어서 '납세의무자' 안의
    '납세'를 못 찾는다. mecab-ko 의존성을 피하려고 문자 bigram 을 쓴다.
    단어 원형도 함께 넣는 이유는 '제3조' 같은 식별자를 정확히 잡기 위해서다
    (벡터 검색이 가장 약한 지점).
    """
    out: list[str] = []
    for word in _WORD.findall(text):
        out.append(word)
        for run in _HANGUL.findall(word):
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return " ".join(out)
