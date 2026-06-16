# -*- coding: utf-8 -*-
"""Tests for the smart formula/template engine: text categorisation, live
recompute, hint parsing, column-signature matching and FX-rate parsing."""
from __future__ import annotations

from app.schemas.statement import PreviewColumn, PreviewVariant
from app.services import diff_analyzer as da
from app.services import formula_engine as fe
from app.services import fx_rates_service as fx
from app.services import signature_util as sig
from app.services.variant_service import recompute_formula_columns


# ── formula_engine ──────────────────────────────────────────────────────────────

def test_contains_is_none_safe_and_case_insensitive():
    rows = [
        {"operation": "FACEBK *A2 Покупка", "expense": 100},
        {"operation": None, "expense": 50},
    ]
    out = fe.evaluate_column('IF(CONTAINS(operation, "facebk"), "ad", "")', rows)
    assert out[0].value == "ad"
    assert out[1].value == ""  # None operation must not raise
    assert all(r.error is None for r in out)


def test_running_sum_is_running_balance():
    # running_sum = cumulative (income - expense) incl. current row (running balance),
    # independent of the formula's own output (no exponential compounding).
    rows = [{"income": 10}, {"income": 20}, {"income": 30}]
    out = fe.evaluate_column("running_sum", rows)
    assert [r.value for r in out] == [10, 30, 60]
    # mixed flows: balance after each row
    rows2 = [{"income": 100}, {"expense": 40}, {"income": 10}]
    out2 = fe.evaluate_column("running_sum", rows2)
    assert [r.value for r in out2] == [100, 60, 70]
    assert fe.validate_formula("running_sum")[0] is True


def test_validate_accepts_arbitrary_column_refs():
    # Formulas may reference real variant keys not in AVAILABLE_FIELDS.
    ok, err = fe.validate_formula('IF(CONTAINS({details_operation}, "kaspi"), 1, 0)')
    assert ok and err is None, err
    ok2, _ = fe.validate_formula("{some_custom_col} * 2 + {another}")
    assert ok2


def test_text_formula_validates():
    ok, err = fe.validate_formula('IF(CONTAINS(operation, "FACEBK"), "x", "")')
    assert ok and err is None


# ── diff_analyzer: text categorisation ──────────────────────────────────────────

def _facebook_case():
    orig_cols = [{"key": "detail", "label": "Детали"}, {"key": "expense", "label": "Расход"}]
    orig_rows = [
        {"detail": "FACEBK *A2 Покупка", "operation": "Покупка", "expense": 29694},
        {"detail": "FACEBK *ZZ Покупка", "operation": "Покупка", "expense": 1500},
        {"detail": "Magnum Покупка", "operation": "Покупка", "expense": 5000},
        {"detail": "На карту Kaspi", "operation": "Перевод", "expense": 2000},
    ]
    edit_cols = orig_cols + [{"key": "comment", "label": "Комментарий"}]
    edit_rows = [
        {**r, "comment": "Реклама Facebook" if "FACEBK" in r["detail"] else ""}
        for r in orig_rows
    ]
    return orig_cols, orig_rows, edit_cols, edit_rows


def test_text_categorisation_detected_for_new_column():
    resp = da.analyze_diff(*_facebook_case())
    formulas = [f for f in resp.findings if f.type == "formula_detected" and f.column_key == "comment"]
    assert formulas, "should infer a rule for the new comment column"
    assert "CONTAINS" in formulas[0].detected_formula
    assert "facebk" in formulas[0].detected_formula.lower()


def test_inferred_text_formula_round_trips():
    resp = da.analyze_diff(*_facebook_case())
    formula = next(f.detected_formula for f in resp.findings if f.type == "formula_detected")
    _, orig_rows, _, _ = _facebook_case()
    out = fe.evaluate_column(formula, orig_rows)
    assert out[0].value == "Реклама Facebook"
    assert out[2].value == ""  # Magnum row not labelled


def test_unchanged_text_column_is_not_categorised():
    # date column is text but untouched → must NOT become a categorisation rule.
    orig_cols = [{"key": "date", "label": "Дата"}, {"key": "detail", "label": "Детали"}]
    orig_rows = [
        {"date": "12.06.26", "detail": "FACEBK Покупка"},
        {"date": "11.06.26", "detail": "Magnum Покупка"},
        {"date": "10.06.26", "detail": "Kaspi Перевод"},
        {"date": "09.06.26", "detail": "Halyk Перевод"},
    ]
    resp = da.analyze_diff(orig_cols, orig_rows, orig_cols, [dict(r) for r in orig_rows])
    assert not [f for f in resp.findings if f.type == "formula_detected"]


# ── diff_analyzer: hints ─────────────────────────────────────────────────────────

def test_rate_hint_does_not_divide_by_100():
    findings = [da.DiffFinding(type="column_added", column_key="c", confidence=1.0, explanation_ru="x")]
    out = da.apply_hint("конвертация по курсу 480", findings)
    assert out[0].detected_formula == "{amount} / 480"


def test_percent_hint_divides_by_100():
    findings = [da.DiffFinding(type="column_added", column_key="c", confidence=1.0, explanation_ru="x")]
    out = da.apply_hint("ндс 12%", findings)
    assert "0.12" in out[0].detected_formula


def test_text_category_hint():
    findings = [da.DiffFinding(type="column_added", column_key="c", confidence=1.0, explanation_ru="x")]
    out = da.apply_hint("FACEBK -> Реклама Facebook", findings)
    assert out[0].type == "formula_detected"
    assert "CONTAINS" in out[0].detected_formula


# ── recompute_formula_columns ────────────────────────────────────────────────────

def test_recompute_preserves_manual_cells_and_computes_formula():
    variant = PreviewVariant(
        key="classic", name="Classic", description="",
        columns=[PreviewColumn(key="detail", label="Детали"), PreviewColumn(key="expense", label="Расход")],
        rows=[
            {"detail": "FACEBK Покупка", "expense": 100, "note": "ручная заметка"},
            {"detail": "Magnum Покупка", "expense": 200, "note": "ручная заметка"},
        ],
    )
    custom_columns = [
        {"key": "detail", "label": "Детали", "kind": "text"},
        {"key": "expense", "label": "Расход", "kind": "number"},
        {"key": "note", "label": "Заметка", "kind": "text"},  # manual, no formula
        {"key": "tax", "label": "НДС", "kind": "number", "formula": "{expense} * 0.12"},
    ]
    out = recompute_formula_columns(variant, custom_columns)
    assert out.rows[0]["tax"] == 12.0
    assert out.rows[1]["tax"] == 24.0
    assert out.rows[0]["note"] == "ручная заметка"  # manual cell untouched


# ── signature matching ───────────────────────────────────────────────────────────

def test_signature_and_jaccard():
    s = sig.signature_from_columns([{"label": "Дата"}, {"label": "Расход"}, {"key": "comment", "label": "Комментарий"}])
    assert s == ["дата", "расход", "комментарий"]
    score = sig.jaccard(["дата", "расход"], ["дата", "расход", "комментарий"])
    assert round(score, 3) == 0.667
    assert score >= 0  # below threshold handled by caller


# ── FX parsing ───────────────────────────────────────────────────────────────────

def test_web_excel_infers_text_and_numeric_rules():
    from app.services.web_excel_formula_translator import infer_column_rules

    source_rows = [
        {"detail": "FACEBK *A2 Покупка", "operation": "Покупка", "expense": 100},
        {"detail": "FACEBK *ZZ Покупка", "operation": "Покупка", "expense": 200},
        {"detail": "Magnum Покупка", "operation": "Покупка", "expense": 300},
        {"detail": "Kaspi Перевод", "operation": "Перевод", "expense": 400},
    ]
    columns = [
        {"key": "detail", "label": "Детали", "kind": "text"},
        {"key": "comment", "label": "Комментарий", "kind": "text"},
        {"key": "vat", "label": "НДС", "kind": "number"},
    ]
    rows = [
        {**r,
         "comment": "Реклама Facebook" if "FACEBK" in r["detail"] else "",
         "vat": round(r["expense"] * 0.12, 4)}
        for r in source_rows
    ]
    rules = infer_column_rules(columns, rows, source_rows)
    assert "CONTAINS" in rules["comment"]
    assert "vat" in rules and "0.12" in rules["vat"].replace(" ", "")


def test_web_excel_a1_translation():
    from app.services.web_excel_formula_translator import _translate_a1
    columns = [
        {"key": "income", "label": "Приход"},
        {"key": "expense", "label": "Расход"},
        {"key": "net", "label": "Нетто"},
    ]
    assert _translate_a1("=A2-B2", columns) == "{income}-{expense}"
    assert _translate_a1("=B2*0.12", columns) == "{expense}*0.12"


def test_fx_rss_parsing_handles_comma_and_nominal():
    xml = (
        "<rss><channel>"
        "<item><title>USD</title><description>478,12</description><quant>1</quant></item>"
        "<item><title>RUB</title><description>61,2</description><quant>10</quant></item>"
        "</channel></rss>"
    )
    rates = fx._parse_rss(xml)
    assert rates["USD"] == 478.12
    assert round(rates["RUB"], 2) == 6.12  # 61.2 / 10
