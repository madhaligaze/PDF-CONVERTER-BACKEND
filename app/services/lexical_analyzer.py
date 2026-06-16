"""
Lexical analyzer for column names.
Maps Russian/English banking keywords → formula recommendations.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.statement import ColumnRecommendation


@dataclass
class _Rule:
    patterns: list[str]    # lowercase regex fragments
    formula: str
    explanation: str
    confidence: float
    category: str = "formula"


# Each rule: match ANY of its patterns (case-insensitive, partial match)
_RULES: list[_Rule] = [
    # VAT / НДС — rate is extracted from the name; no default hardcoded
    # Generic НДС without a number → placeholder rate, user must fill in
    _Rule(
        patterns=[r"\bндс\b", r"\bnds\b", r"\bvat\b", r"налог на добавл"],
        formula="{amount} * RATE",  # replaced dynamically if number found in name
        explanation="НДС — укажите ставку в формуле (например {amount} * 0.16 для 16%)",
        confidence=0.78,
    ),
    # Commission / Комиссия — same: no hardcoded rate
    _Rule(
        patterns=[r"комисс", r"\bfee\b", r"commission"],
        formula="{amount} * RATE",
        explanation="Комиссия — укажите процент в формуле (например {amount} * 0.01 для 1%)",
        confidence=0.70,
    ),
    # Net / Нетто
    _Rule(
        patterns=[r"\bнетто\b", r"\bnet\b", r"чистый", r"итого"],
        formula="{income} - {expense}",
        explanation="Нетто = Приход − Расход",
        confidence=0.92,
    ),
    # Balance / Остаток
    _Rule(
        patterns=[r"остат", r"баланс", r"\bbalance\b", r"сальдо"],
        formula="running_sum",
        explanation="Нарастающий итог (остаток) по сумме",
        confidence=0.85,
        category="aggregate",
    ),
    # Income only
    _Rule(
        patterns=[r"приход", r"\bincome\b", r"\bcredit\b", r"поступ"],
        formula="IF({direction}==\"inflow\", {amount}, 0)",
        explanation="Только суммы поступлений (приходы)",
        confidence=0.90,
        category="formula",
    ),
    # Expense only
    _Rule(
        patterns=[r"расход", r"\bexpense\b", r"\bdebit\b", r"списан"],
        formula="IF({direction}==\"outflow\", {amount}, 0)",
        explanation="Только суммы списаний (расходы)",
        confidence=0.90,
        category="formula",
    ),
    # Currency conversion USD — live rate from NB RK substituted at analyze() time
    _Rule(
        patterns=[r"usd", r"доллар", r"\$"],
        formula="{amount} / FX_USD",
        explanation="Конвертация в USD по курсу НБ РК",
        confidence=0.75,
    ),
    # Currency conversion EUR — live rate from NB RK substituted at analyze() time
    _Rule(
        patterns=[r"\beur\b", r"евро", r"€"],
        formula="{amount} / FX_EUR",
        explanation="Конвертация в EUR по курсу НБ РК",
        confidence=0.75,
    ),
    # Absolute amount
    _Rule(
        patterns=[r"абс", r"\babs\b", r"модул", r"без знак"],
        formula="abs({net})",
        explanation="Абсолютное значение (без знака минус)",
        confidence=0.88,
    ),
    # Counterparty / контрагент
    _Rule(
        patterns=[r"контраг", r"получател", r"отправ", r"\bcounterparty\b", r"sender"],
        formula="{detail}",
        explanation="Поле контрагента/получателя из выписки",
        confidence=0.93,
        category="mapping",
    ),
    # Date
    _Rule(
        patterns=[r"\bдата\b", r"\bdate\b"],
        formula="{date}",
        explanation="Дата операции",
        confidence=0.97,
        category="mapping",
    ),
    # Description
    _Rule(
        patterns=[r"описани", r"назначени", r"\bdescription\b", r"детал"],
        formula="{detail}",
        explanation="Описание / назначение платежа",
        confidence=0.90,
        category="mapping",
    ),
    # Comment
    _Rule(
        patterns=[r"коммент", r"\bcomment\b", r"примечани"],
        formula="{comment}",
        explanation="Поле комментария",
        confidence=0.93,
        category="mapping",
    ),
    # Operation type
    _Rule(
        patterns=[r"операц", r"тип", r"\btype\b", r"\boperation\b"],
        formula="{operation}",
        explanation="Тип / вид операции",
        confidence=0.90,
        category="mapping",
    ),
    # Direction label
    _Rule(
        patterns=[r"направл", r"приход.расход", r"direction", r"знак"],
        formula='IF({direction}=="inflow", "Приход", "Расход")',
        explanation="Текстовая метка направления (Приход / Расход)",
        confidence=0.88,
        category="formula",
    ),
    # Percentage of amount with explicit number
    # e.g. "3%" "0.5%" — matched last as fallback
    _Rule(
        patterns=[r"(\d+(?:\.\d+)?)\s*%"],
        formula="",  # filled dynamically below
        explanation="",
        confidence=0.85,
    ),
]


def _extract_percent(name: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", name)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


def _resolve_fx_tokens(formula: str, explanation: str) -> tuple[str, str]:
    """Replace FX_USD / FX_EUR placeholders with the live NB RK rate."""
    from app.services.fx_rates_service import get_rate

    for code in ("USD", "EUR"):
        token = f"FX_{code}"
        if token not in formula:
            continue
        rate = get_rate(code)
        if rate:
            rate_str = f"{rate:.4f}".rstrip("0").rstrip(".")
            formula = formula.replace(token, rate_str)
            explanation = f"{explanation} ({code} ≈ {rate:.2f} ₸, НБ РК)"
        else:
            formula = formula.replace(token, "480" if code == "USD" else "520")
            explanation = f"{explanation} (резервный курс — НБ РК недоступен)"
    return formula, explanation


def analyze(column_name: str) -> list[ColumnRecommendation]:
    """Return recommendations sorted by confidence (descending)."""
    lower = column_name.lower().strip()
    results: list[ColumnRecommendation] = []

    for rule in _RULES:
        for pat in rule.patterns:
            if re.search(pat, lower):
                formula = rule.formula
                explanation = rule.explanation

                if "FX_" in formula:
                    formula, explanation = _resolve_fx_tokens(formula, explanation)

                # Dynamic percent rule (pattern contains explicit %)
                if formula == "" and "%" in pat:
                    pct = _extract_percent(lower)
                    if pct is not None:
                        factor = pct / 100.0
                        formula = f"{{amount}} * {factor}"
                        explanation = f"{pct}% от суммы операции"
                    else:
                        continue  # can't build formula without a number

                # RATE placeholder: try to extract percent from the column name itself
                if "RATE" in formula:
                    pct = _extract_percent(lower)
                    if pct is not None:
                        factor = pct / 100.0
                        factor_str = f"{factor:.4f}".rstrip("0").rstrip(".")
                        formula = formula.replace("RATE", factor_str)
                        explanation = explanation.split("—")[0].strip() + f" — ставка {pct}% из названия"
                        confidence_boost = 0.15  # higher confidence since rate is explicit
                    else:
                        # No number in name — leave RATE as a visible placeholder
                        # so the user knows they must fill it in
                        confidence_boost = 0.0

                    results.append(ColumnRecommendation(
                        formula=formula,
                        explanation=explanation,
                        confidence=min(1.0, rule.confidence + confidence_boost),
                        category=rule.category,
                        source="lexical",
                    ))
                    break

                results.append(ColumnRecommendation(
                    formula=formula,
                    explanation=explanation,
                    confidence=rule.confidence,
                    category=rule.category,
                    source="lexical",
                ))
                break  # one match per rule is enough

    # Deduplicate by formula
    seen: set[str] = set()
    unique: list[ColumnRecommendation] = []
    for r in results:
        if r.formula not in seen:
            seen.add(r.formula)
            unique.append(r)

    unique.sort(key=lambda r: r.confidence, reverse=True)
    return unique[:4]  # return top-4
