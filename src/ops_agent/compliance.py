"""Rule-based compliance checks for listing copy and image briefs."""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .config import default_lexicon_path

DEFAULT_LEXICON: dict = {
    "version": "2026-10-30",
    "absolute_terms": [
        {
            "term": "100%",
            "rule_id": "ABS-001",
            "message": "绝对化用语：100%",
            "suggestion": "改为具体测试条件，或使用“高概率”等可证伪表述",
        },
        {
            "term": "guaranteed",
            "rule_id": "ABS-002",
            "message": "承诺性用语：guaranteed",
            "suggestion": "删除承诺，改为描述产品特性",
        },
        {
            "term": "best seller",
            "rule_id": "ABS-003",
            "message": "绝对化用语：best seller",
            "suggestion": "改为“热门款”或给出具体榜单与时间",
        },
        {
            # Same claim, different inflection. The matcher folds the dashed and
            # spaced spellings together; the participle needs its own entry.
            "term": "bestselling",
            "rule_id": "ABS-003",
            "message": "绝对化用语：bestselling",
            "suggestion": "改为“热门款”或给出具体榜单与时间",
        },
        {
            "term": "no.1",
            "rule_id": "ABS-004",
            "message": "绝对化用语：No.1",
            "suggestion": "删除排名表述或补充可核验来源",
        },
        {
            "term": "cheapest",
            "rule_id": "ABS-005",
            "message": "绝对化用语：cheapest",
            "suggestion": "改为“性价比高”，避免价格绝对化",
        },
        {
            "term": "cure",
            "rule_id": "MED-001",
            "message": "医疗功效表述：cure",
            "suggestion": "家居用品不得宣称治疗功效",
        },
    ],
    "brand_terms": ["nike", "apple", "disney", "lego", "chanel", "rolex"],
    "copyright_flags": ["brand logo", "celebrity", "movie still", "cartoon character"],
    "prohibited_categories": [],
    "market_terms": {
        "DE": [
            {
                "term": "klimaneutral",
                "rule_id": "DE-001",
                "message": "德国市场：未经证实的“气候中性”表述",
                "suggestion": "删除该表述，或提供可核验的认证与计算依据",
            },
            {
                "term": "tüv-geprüft",
                "rule_id": "DE-002",
                "message": "德国市场：未经授权的 TÜV 认证表述",
                "suggestion": "删除，或提供证书编号与授权文件",
            },
            {
                "term": "garantiert",
                "rule_id": "DE-003",
                "message": "德国市场：绝对化承诺“garantiert”",
                "suggestion": "改为描述产品特性，避免承诺",
            },
        ],
        "UK": [
            {
                "term": "risk-free",
                "rule_id": "UK-001",
                "message": "英国市场：绝对化“risk-free”表述",
                "suggestion": "改为具体退换货政策，避免零风险承诺",
            },
            {
                "term": "no.1",
                "rule_id": "UK-002",
                "message": "英国市场：排名绝对化“No.1”",
                "suggestion": "删除排名，或补充可核验来源与时间",
            },
        ],
    },
}


@dataclass(frozen=True)
class Violation:
    rule_id: str
    severity: str
    message: str
    suggestion: str
    # The lexicon term that matched. An approver needs to see what was found,
    # not just which rule fired, and the audit trail expects a quote.
    quote: str = ""


@dataclass
class ComplianceReport:
    passed: bool
    violations: list[Violation] = field(default_factory=list)
    lexicon_version: str = ""

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "violations": [
                {
                    "rule_id": item.rule_id,
                    "severity": item.severity,
                    "message": item.message,
                    "suggestion": item.suggestion,
                    "quote": item.quote,
                }
                for item in self.violations
            ],
            "lexicon_version": self.lexicon_version,
        }


# Characters that carry no meaning but split a word in two as far as a naive
# matcher is concerned: soft hyphen (U+00AD) is the classic one, plus the
# zero-width / bidi / word-joiner family. A brand word wrapped around one of
# these reads identically to a human and completely differently to ``\b``.
_INVISIBLE = dict.fromkeys(
    map(
        ord,
        "\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b\u180c\u180d\u180e"
        "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
        "\u2060\u2061\u2062\u2063\u2064\u206a\u206b\u206c\u206d\u206e\u206f"
        "\ufeff\u3164\uffa0",
    ),
    None,
)

# Latin lookalikes from other scripts. ``nіke`` with a Cyrillic ``і`` renders as
# "nike" on screen; folding it keeps the deny list honest without needing a
# full Unicode confusables table.
_CONFUSABLES = str.maketrans(
    {
        # Cyrillic
        "а": "a", "в": "b", "г": "r", "е": "e", "к": "k", "м": "m", "н": "h",
        "о": "o", "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "ѕ": "s",
        "і": "i", "ї": "i", "ј": "j", "һ": "h", "ӏ": "l", "ԁ": "d", "ԛ": "q",
        "ԝ": "w", "ɡ": "g", "ү": "y", "ѵ": "v",
        # Greek
        "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o",
        "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "ϲ": "c", "ϳ": "j",
        # Misc Latin variants
        "ı": "i", "ȷ": "j", "ɑ": "a", "ɡ": "g", "ʟ": "l",
    }
)

# "100 percent" and "100 pct" are the same claim as "100%". Normalising them to
# the symbol lets one lexicon entry cover every spelling, and keeps ``%``
# significant so that "100 units" never matches "100%".
_PERCENT_WORD = re.compile(r"\b(?:per\s*cent|percent|pct)\b")


class ComplianceChecker:
    def __init__(self, lexicon_path: Path | None = None, lexicon: dict | None = None) -> None:
        # Keys an older lexicon file never had, backfilled from the baseline.
        self.lexicon_filled: list[str] = []
        if lexicon is not None:
            self.lexicon = lexicon
        else:
            self.lexicon = load_lexicon(lexicon_path, filled=self.lexicon_filled)

    @staticmethod
    def _normalize(text: str) -> str:
        """Fold away everything a seller can use to disguise a denied term.

        Order matters: NFKC first (so full-width and ligatures collapse), then
        casefold, then drop invisible characters, then fold lookalikes, then
        rewrite "percent" as "%", then squeeze the spaces around it.
        """

        folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
        folded = folded.translate(_INVISIBLE).translate(_CONFUSABLES)
        folded = _PERCENT_WORD.sub("%", folded)
        folded = re.sub(r"\s*%\s*", "%", folded)
        return re.sub(r"\s+", " ", folded).strip()

    @classmethod
    def _variants(cls, text: str):
        """The normalized form of ``text``; kept as a generator for callers."""
        yield cls._normalize(text)

    @classmethod
    def _term_pattern(cls, term: str) -> str | None:
        """Turn a lexicon term into a separator-tolerant regex.

        Every alphanumeric character (and ``%``) becomes a hard match, and any
        run of non-word characters is allowed between them. One pattern then
        covers ``best seller`` / ``best-seller`` / ``bestseller``, ``no.1`` /
        ``no1`` / ``no 1``, ``risk-free`` / ``risk free``, and letter-spaced
        ``n i k e``. Boundaries are only added on alphanumeric ends, because a
        trailing ``\\b`` after ``%`` can never match.
        """

        normalized = cls._normalize(term)
        if not normalized:
            return None
        core = [char for char in normalized if char.isalnum() or char == "%"]
        if not core:
            return None
        body = r"[\W_]*".join(re.escape(char) for char in core)
        if core[0].isalnum():
            body = r"(?<![0-9a-z])" + body
        if core[-1].isalnum():
            body = body + r"(?![0-9a-z])"
        return body

    @classmethod
    def _contains_in(cls, normalized: str, term: str) -> bool:
        """Match a term against text that has already been normalized."""

        pattern = cls._term_pattern(term)
        if pattern is None:
            return False
        return re.search(pattern, normalized) is not None

    @classmethod
    def _contains_term(cls, text: str, term: str) -> bool:
        """True when ``term`` appears in ``text`` despite separators or lookalikes."""

        return any(cls._contains_in(candidate, term) for candidate in cls._variants(text))

    def check(self, text: str, image_brief: str, category: str, market: str) -> ComplianceReport:
        # Copy and brief are one claim: a brand logo in the design brief is the
        # same violation as a brand word in the title, and the brief used to be
        # invisible to every rule except the copyright flags.
        combined = f"{text or ''} {image_brief or ''}"
        # Normalize once: ~20 terms are checked against the same text.
        normalized = self._normalize(combined)
        violations: list[Violation] = []

        for rule in self.lexicon.get("absolute_terms", []):
            term = str(rule["term"])
            if self._contains_in(normalized, term):
                violations.append(
                    Violation(
                        rule_id=rule["rule_id"],
                        severity="block",
                        message=rule["message"],
                        suggestion=rule["suggestion"],
                        quote=str(term),
                    )
                )

        market_key = str(market or "US").upper()
        for rule in self.lexicon.get("market_terms", {}).get(market_key, []):
            if self._contains_in(normalized, str(rule["term"])):
                violations.append(
                    Violation(
                        rule_id=rule["rule_id"],
                        severity="block",
                        message=rule["message"],
                        suggestion=rule["suggestion"],
                        quote=str(rule["term"]),
                    )
                )

        for brand in self.lexicon.get("brand_terms", []):
            if self._contains_in(normalized, str(brand)):
                violations.append(
                    Violation(
                        rule_id="BRD-001",
                        severity="block",
                        message=f"未授权品牌词：{brand}",
                        suggestion="删除品牌词，或补充品牌授权凭证",
                        quote=str(brand),
                    )
                )

        for flag in self.lexicon.get("copyright_flags", []):
            normalized_flag = unicodedata.normalize("NFKC", str(flag)).lower().strip()
            if normalized_flag and normalized_flag in normalized:
                violations.append(
                    Violation(
                        rule_id="CPY-001",
                        severity="block",
                        message=f"版权风险：{flag}",
                        suggestion="改用 AI 生成、CC0 或自有素材",
                        quote=str(flag),
                    )
                )

        if str(category or "").strip().casefold() in {
            str(item).strip().casefold() for item in self.lexicon.get("prohibited_categories", [])
        }:
            violations.append(
                Violation(
                    rule_id="CAT-001",
                    severity="block",
                    message=f"禁售类目：{category}",
                    suggestion="从店铺下架该 SKU",
                    quote=str(category),
                )
            )

        return ComplianceReport(
            passed=not any(item.severity == "block" for item in violations),
            violations=violations,
            lexicon_version=str(self.lexicon.get("version", "")),
        )


LEXICON_CHECK_KEYS: tuple[str, ...] = (
    "absolute_terms",
    "market_terms",
    "brand_terms",
    "copyright_flags",
    "prohibited_categories",
)


def merge_lexicon(disk: dict | None, filled: list[str] | None = None) -> dict:
    """Layer the on-disk word list over the baseline, never underneath it.

    A lexicon written by an older release has no ``market_terms`` key at all,
    which silently stopped every DE/UK market rule from firing on two thirds of
    the catalogue. Missing keys are therefore restored from ``DEFAULT_LEXICON``;
    a key that is present but empty stays empty, because that is an explicit
    operator decision rather than a stale file.
    """
    merged = copy.deepcopy(DEFAULT_LEXICON)
    if isinstance(disk, dict):
        merged.update(disk)
        if filled is not None:
            filled.extend(key for key in LEXICON_CHECK_KEYS if key not in disk)
    merged["version"] = str(merged.get("version") or DEFAULT_LEXICON["version"])
    return merged


def load_lexicon(path: Path | None = None, filled: list[str] | None = None) -> dict:
    """Read the operator word list, falling back to the baseline on any fault.

    ``path=None`` means "wherever the deployment keeps its lexicon", so the
    agents and the CLI cannot drift onto two different word lists. A missing,
    unreadable or malformed file degrades to the baseline instead of to an empty
    lexicon: compliance fails closed.
    """
    target = Path(path) if path is not None else default_lexicon_path()
    try:
        disk = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return merge_lexicon(None)
    return merge_lexicon(disk, filled=filled)


def ensure_lexicon(path: Path) -> list[str]:
    """Write the merged word list to disk and say which keys it had to backfill.

    The file is the operator-facing copy, so it has to contain everything the
    checker actually enforces. Returns the keys that were missing from an older
    file and got restored from the baseline.
    """
    path = Path(path)
    filled: list[str] = []
    lexicon = load_lexicon(path, filled=filled)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = None
    # Rewrite only when the enforced rules actually change, so a hand-formatted
    # operator file is not reformatted on every single review run.
    if current != lexicon:
        path.write_text(json.dumps(lexicon, ensure_ascii=False, indent=2), encoding="utf-8")
    return filled
