from __future__ import annotations

from collections import defaultdict

from app.schemas.statement import (
    ParsedStatement,
    PreviewColumn,
    PreviewVariant,
    TransformationTemplate,
)
from app.services import formula_engine
# kaspi_business_statement_service imported lazily inside _build_kaspi_business_variants

PRIMARY_GROUP = "primary"
KASPI_BUSINESS_GROUP = "kaspi_business_plus"


def build_variants(statement: ParsedStatement) -> list[PreviewVariant]:
    transactions = statement.transactions
    parser_key = statement.metadata.parser_key

    if parser_key == "kaspi_gold_statement":
        return _build_kaspi_gold_variants(transactions)
    if parser_key == "kaspi_business_statement":
        return _build_kaspi_business_variants(transactions)
    if parser_key == "halyk_fiz_statement":
        return _build_halyk_fiz_variants(transactions)

    variants = _build_primary_variants(transactions)
    if any(t.category for t in transactions):
        variants.append(_build_ai_variant(transactions))
    return variants


def apply_template_to_variant(
    variant: PreviewVariant,
    template: TransformationTemplate,
) -> PreviewVariant:
    configured_columns = [column for column in template.columns if column.enabled]
    column_order = [column.key for column in configured_columns]

    # Pre-compute formula results for formula columns (full column pass for running_sum)
    formula_columns = {
        col.key: col.formula
        for col in configured_columns
        if col.formula and col.formula.strip()
    }
    formula_results: dict[str, list[formula_engine.FormulaResult]] = {}
    for col_key, formula in formula_columns.items():
        formula_results[col_key] = formula_engine.evaluate_column(formula, [
            {**row, "direction": row.get("direction", "")}
            for row in variant.rows
        ])

    rows = []
    for i, row in enumerate(variant.rows):
        shaped_row = {}
        for key in column_order:
            if key in formula_results:
                res = formula_results[key][i]
                shaped_row[key] = res.value if res.error is None else None
            else:
                shaped_row[key] = row.get(key)
        if "direction" in row:
            shaped_row["direction"] = row["direction"]
        rows.append(shaped_row)

    return PreviewVariant(
        key=f"template::{template.template_id}",
        name=template.name,
        description=template.description or variant.description,
        columns=[
            PreviewColumn(key=column.key, label=column.label, kind=column.kind)
            for column in configured_columns
        ],
        rows=rows,
        group=variant.group,
        template_id=template.template_id,
        base_variant_key=template.base_variant_key,
    )


def recompute_formula_columns(
    variant: PreviewVariant,
    custom_columns: list[dict],
) -> PreviewVariant:
    """Re-evaluate formula columns over the variant's OWN rows.

    Using the variant rows (not statement.transactions by index) keeps grouped/FX
    variants aligned, and lets this run even when the editor already supplied
    custom_rows — formula columns are the source of truth, manual cells are kept.
    """
    formula_cols = [
        (c["key"], c["formula"])
        for c in custom_columns
        if c.get("formula") and str(c["formula"]).strip()
    ]
    if not formula_cols:
        return variant

    rows = [dict(row) for row in variant.rows]
    for col_key, formula in formula_cols:
        # Evaluate against the running row state so a later formula column can read
        # an earlier one's computed value. Always set the key (None on error) so the
        # column is never silently absent from a row.
        results = formula_engine.evaluate_column(formula, rows)
        for i, res in enumerate(results):
            if i < len(rows):
                rows[i][col_key] = res.value if res.error is None else None
    return variant.model_copy(update={"rows": rows})


def build_template_seed(variant: PreviewVariant) -> list[dict[str, str | bool]]:
    return [
        {
            "key": column.key,
            "label": column.label,
            "kind": column.kind,
            "enabled": True,
        }
        for column in variant.columns
    ]


def _build_kaspi_gold_variants(transactions) -> list[PreviewVariant]:
    return [
        PreviewVariant(
            key="classic_financier",
            name="\u041a\u043b\u0430\u0441\u0441\u0438\u043a\u0430",
            description="\u0414\u0430\u0442\u0430, \u043f\u0440\u0438\u0445\u043e\u0434, \u0440\u0430\u0441\u0445\u043e\u0434 \u0438 \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u044f / \u0434\u0435\u0442\u0430\u043b\u0438 \u0432 \u043e\u0434\u043d\u043e\u043c \u0441\u0442\u043e\u043b\u0431\u0446\u0435.",
            columns=[
                PreviewColumn(key="date", label="\u0414\u0430\u0442\u0430"),
                PreviewColumn(key="income", label="\u041f\u0440\u0438\u0445\u043e\u0434, \u20b8", kind="currency"),
                PreviewColumn(key="expense", label="\u0420\u0430\u0441\u0445\u043e\u0434, \u20b8", kind="currency"),
                PreviewColumn(key="details_operation", label="\u0414\u0435\u0442\u0430\u043b\u0438 / \u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f"),
            ],
            rows=[
                {
                    "date": row.date,
                    "income": row.income,
                    "expense": row.expense,
                    "details_operation": row.details_operation,
                    "direction": row.direction,
                }
                for row in transactions
            ],
            group=PRIMARY_GROUP,
        ),
        PreviewVariant(
            key="operation_split",
            name="\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u0439",
            description=(
                "\u041e\u0442\u0434\u0435\u043b\u044c\u043d\u044b\u0439 \u0432\u0438\u0434 "
                "\u0434\u043b\u044f \u0444\u0438\u0437\u043b\u0438\u0446: \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u044f "
                "\u0438 \u0434\u0435\u0442\u0430\u043b\u0438 \u0432 \u0440\u0430\u0437\u043d\u044b\u0445 "
                "\u043a\u043e\u043b\u043e\u043d\u043a\u0430\u0445 \u0431\u0435\u0437 \u043a\u043e\u043b\u043e\u043d\u043a\u0438 "
                "\u043d\u0435\u0442\u0442\u043e."
            ),
            columns=[
                PreviewColumn(key="date", label="\u0414\u0430\u0442\u0430"),
                PreviewColumn(key="operation", label="\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f"),
                PreviewColumn(key="detail", label="\u0414\u0435\u0442\u0430\u043b\u0438"),
                PreviewColumn(key="income", label="\u041f\u0440\u0438\u0445\u043e\u0434, \u20b8", kind="currency"),
                PreviewColumn(key="expense", label="\u0420\u0430\u0441\u0445\u043e\u0434, \u20b8", kind="currency"),
            ],
            rows=[
                {
                    "date": row.date,
                    "operation": row.operation,
                    "detail": row.detail,
                    "income": row.income,
                    "expense": row.expense,
                    "direction": row.direction,
                }
                for row in transactions
            ],
            group=PRIMARY_GROUP,
        ),
    ]


def _build_primary_variants(transactions) -> list[PreviewVariant]:
    variants: list[PreviewVariant] = [
        PreviewVariant(
            key="classic_financier",
            name="\u0424\u0438\u043d\u0430\u043d\u0441\u0438\u0441\u0442 \u041a\u043b\u0430\u0441\u0441\u0438\u043a\u0430",
            description=(
                "\u0411\u0430\u0437\u043e\u0432\u0430\u044f \u043b\u0435\u043d\u0442\u0430: "
                "\u0434\u0430\u0442\u0430, \u043f\u0440\u0438\u0445\u043e\u0434, "
                "\u0440\u0430\u0441\u0445\u043e\u0434 \u0438 \u043e\u0431\u044a\u0435\u0434\u0438\u043d\u0435\u043d\u043d\u0430\u044f "
                "\u0441\u0442\u0440\u043e\u043a\u0430 \u0434\u0435\u0442\u0430\u043b\u0435\u0439."
            ),
            columns=[
                PreviewColumn(key="date", label="\u0414\u0430\u0442\u0430"),
                PreviewColumn(
                    key="income",
                    label="\u041f\u0440\u0438\u0445\u043e\u0434, \u20b8",
                    kind="currency",
                ),
                PreviewColumn(
                    key="expense",
                    label="\u0420\u0430\u0441\u0445\u043e\u0434, \u20b8",
                    kind="currency",
                ),
                PreviewColumn(
                    key="details_operation",
                    label="\u0414\u0435\u0442\u0430\u043b\u0438 / \u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f",
                ),
            ],
            rows=[
                {
                    "date": row.date,
                    "income": row.income,
                    "expense": row.expense,
                    "details_operation": row.details_operation,
                    "direction": row.direction,
                }
                for row in transactions
            ],
            group=PRIMARY_GROUP,
        ),
        PreviewVariant(
            key="operation_split",
            name="\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f \u041e\u0442\u0434\u0435\u043b\u044c\u043d\u043e",
            description=(
                "\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f \u0438 \u0434\u0435\u0442\u0430\u043b\u0438 "
                "\u0432 \u0440\u0430\u0437\u043d\u044b\u0445 \u043a\u043e\u043b\u043e\u043d\u043a\u0430\u0445."
            ),
            columns=[
                PreviewColumn(key="date", label="\u0414\u0430\u0442\u0430"),
                PreviewColumn(key="operation", label="\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f"),
                PreviewColumn(key="detail", label="\u0414\u0435\u0442\u0430\u043b\u0438"),
                PreviewColumn(
                    key="income",
                    label="\u041f\u0440\u0438\u0445\u043e\u0434, \u20b8",
                    kind="currency",
                ),
                PreviewColumn(
                    key="expense",
                    label="\u0420\u0430\u0441\u0445\u043e\u0434, \u20b8",
                    kind="currency",
                ),
                PreviewColumn(key="amount", label="\u041d\u0435\u0442\u0442\u043e, \u20b8", kind="currency"),
            ],
            rows=[
                {
                    "date": row.date,
                    "operation": row.operation,
                    "detail": row.detail,
                    "income": row.income,
                    "expense": row.expense,
                    "amount": row.amount,
                    "direction": row.direction,
                }
                for row in transactions
            ],
            group=PRIMARY_GROUP,
        ),
        PreviewVariant(
            key="counterparty_first",
            name="\u041a\u043e\u043d\u0442\u0440\u0430\u0433\u0435\u043d\u0442 \u0412\u043f\u0435\u0440\u0435\u0434",
            description=(
                "\u041f\u0440\u0438\u043e\u0440\u0438\u0442\u0435\u0442 \u043d\u0430 "
                "\u043a\u043e\u043d\u0442\u0440\u0430\u0433\u0435\u043d\u0442\u0435 \u0438 "
                "\u043d\u0430\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0438 \u043f\u043e\u0442\u043e\u043a\u0430."
            ),
            columns=[
                PreviewColumn(key="date", label="\u0414\u0430\u0442\u0430"),
                PreviewColumn(
                    key="detail",
                    label="\u041a\u043e\u043d\u0442\u0440\u0430\u0433\u0435\u043d\u0442 / \u041c\u0435\u0440\u0447\u0430\u043d\u0442",
                ),
                PreviewColumn(key="operation", label="\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f"),
                PreviewColumn(
                    key="direction_label",
                    label="\u041d\u0430\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435",
                ),
                PreviewColumn(key="amount_abs", label="\u0421\u0443\u043c\u043c\u0430, \u20b8", kind="currency"),
            ],
            rows=[
                {
                    "date": row.date,
                    "detail": row.detail,
                    "operation": row.operation,
                    "direction_label": _direction_label(row.direction),
                    "amount_abs": row.income if row.income is not None else row.expense,
                    "direction": row.direction,
                }
                for row in transactions
            ],
            group=PRIMARY_GROUP,
        ),
        PreviewVariant(
            key="daily_summary",
            name="\u0421\u0432\u043e\u0434\u043a\u0430 \u041f\u043e \u0414\u043d\u044f\u043c",
            description=(
                "\u0413\u0440\u0443\u043f\u043f\u0438\u0440\u043e\u0432\u043a\u0430 \u043f\u043e "
                "\u0434\u0430\u0442\u0430\u043c \u0434\u043b\u044f \u043a\u0430\u0440\u0442\u0438\u043d\u044b cash-flow."
            ),
            columns=[
                PreviewColumn(key="date", label="\u0414\u0430\u0442\u0430"),
                PreviewColumn(
                    key="income_total",
                    label="\u041f\u0440\u0438\u0445\u043e\u0434 \u0437\u0430 \u0434\u0435\u043d\u044c, \u20b8",
                    kind="currency",
                ),
                PreviewColumn(
                    key="expense_total",
                    label="\u0420\u0430\u0441\u0445\u043e\u0434 \u0437\u0430 \u0434\u0435\u043d\u044c, \u20b8",
                    kind="currency",
                ),
                PreviewColumn(key="net_total", label="\u041d\u0435\u0442\u0442\u043e, \u20b8", kind="currency"),
                PreviewColumn(
                    key="operations_count",
                    label="\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u0439",
                ),
            ],
            rows=_build_daily_rows(transactions),
            group=PRIMARY_GROUP,
        ),
        PreviewVariant(
            key="audit_flags",
            name="\u0410\u0443\u0434\u0438\u0442 \u0418 \u0424\u043b\u0430\u0433\u0438",
            description=(
                "\u041b\u0435\u043d\u0442\u0430 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438 "
                "\u0441\u043f\u043e\u0440\u043d\u044b\u0445 \u0438 \u0440\u0438\u0441\u043a\u043e\u0432\u044b\u0445 "
                "\u0441\u0442\u0440\u043e\u043a."
            ),
            columns=[
                PreviewColumn(key="date", label="\u0414\u0430\u0442\u0430"),
                PreviewColumn(
                    key="direction_label",
                    label="\u041d\u0430\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435",
                ),
                PreviewColumn(key="amount_abs", label="\u0421\u0443\u043c\u043c\u0430, \u20b8", kind="currency"),
                PreviewColumn(key="operation", label="\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f"),
                PreviewColumn(key="detail", label="\u0414\u0435\u0442\u0430\u043b\u0438"),
                PreviewColumn(key="flags_label", label="\u0424\u043b\u0430\u0433\u0438"),
                PreviewColumn(key="note", label="\u041f\u0440\u0438\u043c\u0435\u0447\u0430\u043d\u0438\u0435"),
            ],
            rows=[
                {
                    "date": row.date,
                    "direction_label": _direction_label(row.direction),
                    "amount_abs": row.income if row.income is not None else row.expense,
                    "operation": row.operation,
                    "detail": row.detail,
                    "flags_label": ", ".join(row.flags) if row.flags else "OK",
                    "note": row.note,
                    "direction": row.direction,
                }
                for row in transactions
            ],
            group=PRIMARY_GROUP,
        ),
    ]
    return variants



def _build_kaspi_business_variants(transactions) -> list[PreviewVariant]:
    return [
        PreviewVariant(
            key="business_compact_classic",
            name="\u041a\u0430\u0441\u043f\u0438 \u042e\u0440 \u0441\u0447\u0435\u0442",
            description="\u041e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 \u0444\u043e\u0440\u043c\u0430\u0442 \u0434\u043b\u044f \u0431\u0443\u0445\u0433\u0430\u043b\u0442\u0435\u0440\u0438\u0438.",
            columns=[
                PreviewColumn(key="date", label="\u0414\u0430\u0442\u0430 \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u0438"),
                PreviewColumn(key="income", label="\u041f\u0440\u0438\u0445\u043e\u0434, \u20b8", kind="currency"),
                PreviewColumn(key="expense", label="\u0420\u0430\u0441\u0445\u043e\u0434, \u20b8", kind="currency"),
                PreviewColumn(key="detail", label="\u041a\u043e\u043d\u0442\u0440\u0430\u0433\u0435\u043d\u0442"),
                PreviewColumn(key="comment", label="\u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439"),
            ],
            rows=[
                {
                    "date": row.date,
                    "income": row.income,
                    "expense": row.expense,
                    "detail": row.detail,
                    "comment": row.comment,
                    "direction": row.direction,
                }
                for row in transactions
            ],
            group=KASPI_BUSINESS_GROUP,
        ),
    ]


def _build_halyk_fiz_variants(transactions) -> list[PreviewVariant]:
    return [
        PreviewVariant(
            key="halyk_fiz_classic",
            name="Halyk Statement",
            description="Clean Halyk statement export with foreign-currency conversions shown as KZT settlement rows.",
            columns=[
                PreviewColumn(key="processing_date", label="\u0414\u0430\u0442\u0430 \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438"),
                PreviewColumn(key="currency_op", label="\u0412\u0430\u043b\u044e\u0442\u0430 \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u0438"),
                PreviewColumn(key="detail", label="\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u0438"),
                PreviewColumn(
                    key="income",
                    label="\u041f\u0440\u0438\u0445\u043e\u0434 \u0432 \u0432\u0430\u043b\u044e\u0442\u0435 \u0441\u0447\u0435\u0442\u0430",
                    kind="currency",
                ),
                PreviewColumn(
                    key="expense",
                    label="\u0420\u0430\u0441\u0445\u043e\u0434 \u0432 \u0432\u0430\u043b\u044e\u0442\u0435 \u0441\u0447\u0435\u0442\u0430",
                    kind="currency",
                ),
                PreviewColumn(key="comment", label="\u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439"),
            ],
            rows=_build_halyk_fiz_rows(transactions),
            group=PRIMARY_GROUP,
        ),
    ]


def _parse_halyk_date_key(date_str: str):
    from datetime import datetime
    for fmt in ("%d.%m.%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    from datetime import datetime as dt
    return dt.min


def _merge_halyk_usd_to_kzt(transactions) -> list:
    """
    Match Автоконвертация расход-KZT rows to their USD purchases (FIFO by date).
    Replace USD expense with proportional KZT equivalent. Drop all autoconv rows.
    """
    kzt_conversions = []  # (date_str, usd_amount, kzt_cost)
    regular = []

    for tx in transactions:
        if tx.operation == "autoconv":
            if tx.expense is not None and tx.expense > 0:
                try:
                    usd_amount = abs(float(tx.note.split("fx:")[1])) if tx.note and "fx:" in tx.note else 0.0
                except (ValueError, IndexError):
                    usd_amount = 0.0
                if usd_amount > 0:
                    kzt_conversions.append((tx.date, usd_amount, tx.expense))
        else:
            regular.append(tx)

    if not kzt_conversions:
        return regular

    result = list(regular)
    pending = [
        (i, tx) for i, tx in enumerate(result)
        if tx.currency_op in ("USD", "EUR") and tx.direction == "outflow"
    ]

    # Sort by real calendar date (DD.MM.YY strings don't sort lexicographically)
    for conv_date, conv_usd, conv_kzt in sorted(kzt_conversions, key=lambda x: _parse_halyk_date_key(x[0])):
        conv_dt = _parse_halyk_date_key(conv_date)
        matched = []
        running = 0.0
        for pos, (ri, tx) in enumerate(pending):
            # Only consider purchases on or before conversion date
            if _parse_halyk_date_key(tx.date) > conv_dt:
                continue
            usd = tx.expense or 0.0
            if running + usd <= conv_usd + 0.01:
                matched.append((pos, ri, tx, usd))
                running += usd
                if abs(running - conv_usd) < 0.01:
                    break

        if not matched or running < 0.005:
            continue

        for _, ri, tx, usd in matched:
            kzt = round(conv_kzt * usd / running, 2)
            result[ri] = tx.model_copy(update={"expense": kzt, "amount": -kzt})

        matched_positions = {pos for pos, _, _, _ in matched}
        pending = [(ri, tx) for pos, (ri, tx) in enumerate(pending) if pos not in matched_positions]

    return result


def _build_halyk_fiz_rows(transactions) -> list[dict[str, object | None]]:
    rows: list[dict[str, object | None]] = []
    purchase_groups, negative_groups, positive_groups = _plan_halyk_fx_groups(transactions)

    for source_row_number, tx in enumerate(transactions, start=1):
        if tx.operation != "autoconv":
            group = purchase_groups.get(source_row_number)
            row = _build_halyk_native_row(tx, source_row_number)
            if group is not None and _halyk_group_needs_summary(group):
                row["comment"] = _append_halyk_summary_comment(row.get("comment"), group["summary"])
                if _halyk_group_summary_in_aux(
                    group_size=len(group["matched"]),
                    compact_total=_halyk_positive_before_negative(
                        group["positive_source_row_number"],
                        group["negative_source_row_number"],
                    ),
                ):
                    row["aux_summary"] = group["summary"]
            rows.append(row)
            continue

        if tx.income is not None:
            group = positive_groups.get(source_row_number)
            if group is None:
                rows.append(_build_halyk_native_row(tx, source_row_number))
                continue
            rows.extend(_build_halyk_fx_currency_rows(group, tx, source_row_number))
            continue

        if tx.expense is None:
            rows.append(_build_halyk_native_row(tx, source_row_number))
            continue

        group = negative_groups.get(source_row_number)
        if group is None:
            rows.append(_build_halyk_native_row(tx, source_row_number))
            continue
        rows.extend(
            _build_halyk_fx_kzt_rows(
                matched=group["matched"],
                allocations=group["allocations"],
                autoconv_tx=tx,
                source_row_number=source_row_number,
                summary=group["summary"],
                summary_enabled=_halyk_group_needs_summary(group),
                compact_total=_halyk_positive_before_negative(
                    group["positive_source_row_number"],
                    group["negative_source_row_number"],
                ),
            )
        )

    return rows


def _plan_halyk_fx_groups(transactions):
    foreign_candidates = [
        {
            "source_row_number": source_row_number,
            "transaction": tx,
        }
        for source_row_number, tx in enumerate(transactions, start=1)
        if _is_halyk_foreign_purchase(tx)
    ]
    foreign_candidates.sort(
        key=lambda item: (
            _parse_halyk_date_key(item["transaction"].date),
            item["source_row_number"],
        )
    )

    positive_queues: defaultdict[tuple[str, float], list[tuple[int, object]]] = defaultdict(list)
    for source_row_number, tx in enumerate(transactions, start=1):
        if tx.operation == "autoconv" and tx.income is not None:
            key = (tx.currency_op or "", round(_extract_halyk_fx_total(tx), 2))
            positive_queues[key].append((source_row_number, tx))

    purchase_groups: dict[int, dict[str, object]] = {}
    negative_groups: dict[int, dict[str, object]] = {}
    positive_groups: dict[int, dict[str, object]] = {}

    for source_row_number, tx in enumerate(transactions, start=1):
        if tx.operation != "autoconv" or tx.expense is None:
            continue

        fx_total = _extract_halyk_fx_total(tx)
        if fx_total <= 0:
            continue

        matched = _match_halyk_foreign_rows(foreign_candidates, tx, fx_total)
        if not matched:
            continue

        key = (tx.currency_op or "", round(fx_total, 2))
        group = {
            "matched": matched,
            "allocations": _allocate_halyk_kzt_amounts(tx.expense, matched),
            "summary": _build_halyk_fx_summary(tx.currency_op or "", fx_total, tx.expense),
            "currency": tx.currency_op or "",
            "negative_tx": tx,
            "negative_source_row_number": source_row_number,
            "positive_tx": None,
            "positive_source_row_number": None,
        }
        negative_groups[source_row_number] = group
        for item in matched:
            purchase_groups[item["source_row_number"]] = group

        positive_rows = positive_queues[key]
        if positive_rows:
            positive_source_row_number, positive_tx = positive_rows.pop(0)
            group["positive_tx"] = positive_tx
            group["positive_source_row_number"] = positive_source_row_number
            positive_groups[positive_source_row_number] = group

    return purchase_groups, negative_groups, positive_groups


def _build_halyk_native_row(tx, source_row_number: int) -> dict[str, object | None]:
    return {
        "processing_date": tx.processing_date or tx.date,
        "currency_op": _halyk_display_currency(tx),
        "detail": _clean_halyk_detail(tx.detail),
        "income": tx.income,
        "expense": tx.expense,
        "comment": _normalize_halyk_comment(tx.comment),
        "aux_summary": None,
        "aux_detail": _clean_halyk_detail(tx.detail),
        "direction": tx.direction,
        "_source_row_number": source_row_number,
        "_provenance": tx.source,
    }


def _is_halyk_foreign_purchase(tx) -> bool:
    return tx.operation != "autoconv" and tx.direction == "outflow" and (tx.currency_op or "") != "KZT"


def _halyk_display_currency(tx) -> str | None:
    if tx.operation == "autoconv" and tx.expense is not None:
        return "KZT"
    return tx.currency_op


def _clean_halyk_detail(detail: str | None) -> str:
    value = " ".join((detail or "").split())
    prefix = "\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f \u043e\u043f\u043b\u0430\u0442\u044b \u0443 \u043a\u043e\u043c\u043c\u0435\u0440\u0441\u0430\u043d\u0442\u0430 "
    if value.startswith(prefix):
        return value[len(prefix):]
    return value


def _extract_halyk_fx_total(tx) -> float:
    if tx.note and tx.note.startswith("fx:"):
        try:
            return abs(float(tx.note.split(":", maxsplit=1)[1]))
        except ValueError:
            return 0.0
    if tx.income is not None:
        return abs(tx.income)
    if tx.expense is not None:
        return abs(tx.expense)
    return 0.0


def _match_halyk_foreign_rows(pending_foreign, autoconv_tx, fx_total: float) -> list[dict[str, object]]:
    matched: list[dict[str, object]] = []
    running = 0.0
    conv_date = _parse_halyk_date_key(autoconv_tx.date)
    currency = autoconv_tx.currency_op or ""

    for item in pending_foreign:
        tx = item["transaction"]
        if (tx.currency_op or "") != currency:
            continue
        if _parse_halyk_date_key(tx.date) > conv_date:
            continue
        amount = tx.expense or 0.0
        if running + amount <= fx_total + 0.01:
            matched.append(item)
            running += amount
            if abs(running - fx_total) < 0.01:
                break

    if not matched or running < 0.005:
        return []

    matched_ids = {id(item) for item in matched}
    pending_foreign[:] = [item for item in pending_foreign if id(item) not in matched_ids]
    return matched


def _allocate_halyk_kzt_amounts(total_kzt: float, matched: list[dict[str, object]]) -> list[float]:
    base_amounts = [(item["transaction"].expense or 0.0) for item in matched]
    total_foreign = sum(base_amounts)
    if total_foreign <= 0:
        return [round(total_kzt, 2)]

    allocations: list[float] = []
    allocated = 0.0
    for index, foreign_amount in enumerate(base_amounts, start=1):
        if index == len(base_amounts):
            amount = round(total_kzt - allocated, 2)
        else:
            amount = round(total_kzt * foreign_amount / total_foreign, 2)
            allocated += amount
        allocations.append(amount)
    return allocations


def _build_halyk_fx_summary(currency: str, fx_total: float, kzt_total: float) -> str:
    return (
        f"(\u041e\u0431\u0449\u0438\u0439 {_format_halyk_compact_amount(fx_total)} {currency.lower()} = "
        f"\u0432 kzt \u043e\u0431\u0449\u0438\u0439 {_format_halyk_amount(kzt_total)})"
    )


def _append_halyk_summary_comment(comment: object | None, summary: str) -> str:
    if comment:
        return f"{comment}\n{summary}"
    return summary


def _build_halyk_fx_kzt_rows(
    *,
    matched: list[dict[str, object]],
    allocations: list[float],
    autoconv_tx,
    source_row_number: int,
    summary: str,
    summary_enabled: bool,
    compact_total: bool,
) -> list[dict[str, object | None]]:
    rows: list[dict[str, object | None]] = []
    summary_in_aux = _halyk_group_summary_in_aux(
        group_size=len(matched),
        compact_total=compact_total,
    )
    for item, allocation in zip(matched, allocations, strict=False):
        purchase_tx = item["transaction"]
        rows.append(
            {
                "processing_date": autoconv_tx.processing_date or autoconv_tx.date,
                "currency_op": "KZT",
                "detail": _build_halyk_autoconv_detail(
                    purchase_tx=purchase_tx,
                    row_currency="KZT",
                    group_size=len(matched),
                    group_total=autoconv_tx.expense or 0.0,
                    compact_total=compact_total,
                ),
                "income": None,
                "expense": allocation,
                "comment": _halyk_build_comment(autoconv_tx.date, summary, summary_enabled),
                "aux_summary": summary if summary_in_aux else None,
                "aux_detail": _halyk_generated_aux_detail(
                    purchase_tx=purchase_tx,
                    index=len(rows),
                    total=len(matched),
                    row_currency="KZT",
                    group_size=len(matched),
                    compact_total=compact_total,
                ),
                "direction": "outflow",
                "_source_row_number": source_row_number,
                "_provenance": (
                    f"halyk_fx_kzt:{item['source_row_number']},{source_row_number}"
                ),
            }
        )
    return rows


def _build_halyk_fx_currency_rows(group: dict[str, object], autoconv_tx, source_row_number: int) -> list[dict[str, object | None]]:
    rows: list[dict[str, object | None]] = []
    currency = group["currency"] or "FX"
    matched_items = list(group["matched"])
    compact_total = _halyk_positive_before_negative(group["positive_source_row_number"], group["negative_source_row_number"])
    if compact_total:
        matched_items = list(reversed(matched_items))

    summary_in_aux = _halyk_group_summary_in_aux(
        group_size=len(group["matched"]),
        compact_total=compact_total,
    )
    for item in matched_items:
        purchase_tx = item["transaction"]
        rows.append(
            {
                "processing_date": autoconv_tx.processing_date or autoconv_tx.date,
                "currency_op": currency,
                "detail": _build_halyk_autoconv_detail(
                    purchase_tx=purchase_tx,
                    row_currency=currency,
                    group_size=len(group["matched"]),
                    group_total=_extract_halyk_fx_total(autoconv_tx),
                    compact_total=False,
                ),
                "income": purchase_tx.expense,
                "expense": None,
                "comment": _halyk_build_comment(
                    autoconv_tx.date,
                    group["summary"],
                    _halyk_group_needs_summary(group),
                ),
                "aux_summary": group["summary"] if summary_in_aux else None,
                "aux_detail": _halyk_generated_aux_detail(
                    purchase_tx=purchase_tx,
                    index=len(rows),
                    total=len(group["matched"]),
                    row_currency=currency,
                    group_size=len(group["matched"]),
                    compact_total=compact_total,
                ),
                "direction": "inflow",
                "_source_row_number": source_row_number,
                "_provenance": (
                    f"halyk_fx_currency:{item['source_row_number']},{source_row_number}"
                ),
            }
        )
    return rows


def _normalize_halyk_comment(comment: str | None) -> str | None:
    if comment is None:
        return None
    if comment.startswith("\u0414\u0430\u0442\u0430 \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u0438:"):
        return comment.replace("\u0414\u0430\u0442\u0430 \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u0438:", "\u0414\u0430\u0442\u0430 \u0442\u0440:", 1)
    return comment


def _format_halyk_amount(value: float) -> str:
    text = f"{round(value, 2):,.2f}"
    if text.endswith(".00"):
        text = text[:-3]
    elif text.endswith("0"):
        text = text[:-1]
    return text.replace(",", " ").replace(".", ",")


def _format_halyk_compact_amount(value: float) -> str:
    text = f"{round(value, 2):.2f}".replace(".", ",")
    if len(text) >= 4 and text[-3:-1] == ",0":
        return text[:-3] + "," + text[-1]
    if text.endswith(",00"):
        return text[:-3]
    if text.endswith("0"):
        return text[:-1]
    return text


def _halyk_group_needs_summary(group: dict[str, object]) -> bool:
    return len(group["matched"]) > 1


def _halyk_build_comment(date_str: str, summary: str, summary_enabled: bool) -> str:
    base = f"\u0414\u0430\u0442\u0430 \u0442\u0440: {date_str}"
    if not summary_enabled:
        return base
    return _append_halyk_summary_comment(base, summary)


def _build_halyk_autoconv_detail(
    *,
    purchase_tx,
    row_currency: str,
    group_size: int,
    group_total: float,
    compact_total: bool,
) -> str:
    base = f"{_clean_halyk_detail(purchase_tx.detail)} - (\u0410\u0432\u0442\u043e. \u041a\u043e\u043d\u0432)"
    if group_size <= 1:
        return base
    if row_currency == "KZT":
        total = _format_halyk_compact_amount(group_total) if compact_total else _format_halyk_amount(group_total)
    else:
        total = _format_halyk_compact_amount(group_total)
    return f"{base} - (\u043e\u0431\u0449\u0438\u0439 {total})"


def _halyk_positive_before_negative(
    positive_source_row_number: int | None,
    negative_source_row_number: int | None,
) -> bool:
    return (
        positive_source_row_number is not None
        and negative_source_row_number is not None
        and positive_source_row_number < negative_source_row_number
    )


def _halyk_group_summary_in_aux(*, group_size: int, compact_total: bool) -> bool:
    return group_size > 1 and not compact_total


def _halyk_generated_aux_detail(
    *,
    purchase_tx,
    index: int,
    total: int,
    row_currency: str,
    group_size: int,
    compact_total: bool,
) -> str | None:
    autoconv_text = "\u0410\u0432\u0442\u043e\u043a\u043e\u043d\u0432\u0435\u0440\u0442\u0430\u0446\u0438\u044f \u0434\u043e\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044c\u043d\u043e\u0439 \u0441\u0443\u043c\u043c\u044b \u043f\u043e \u043f\u0440\u043e\u0448\u0435\u0434\u0448\u0438\u043c \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u044f\u043c"
    if _halyk_group_summary_in_aux(group_size=group_size, compact_total=compact_total):
        return autoconv_text
    if group_size > 1 and compact_total:
        return autoconv_text if index == total - 1 else None
    return _clean_halyk_detail(purchase_tx.detail) if row_currency == "KZT" else autoconv_text


def _build_ai_variant(transactions) -> PreviewVariant:
    return PreviewVariant(
        key="ai_categories",
        name="AI \u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u0438",
        description=(
            "\u041b\u043e\u043a\u0430\u043b\u044c\u043d\u0430\u044f AI-\u0440\u0430\u0437\u043c\u0435\u0442\u043a\u0430 "
            "\u0442\u0440\u0430\u043d\u0437\u0430\u043a\u0446\u0438\u0439."
        ),
        columns=[
            PreviewColumn(key="date", label="\u0414\u0430\u0442\u0430"),
            PreviewColumn(key="category", label="AI \u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f"),
            PreviewColumn(key="detail", label="\u041a\u043e\u043d\u0442\u0440\u0430\u0433\u0435\u043d\u0442"),
            PreviewColumn(key="operation", label="\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f"),
            PreviewColumn(key="income", label="\u041f\u0440\u0438\u0445\u043e\u0434, \u20b8", kind="currency"),
            PreviewColumn(key="expense", label="\u0420\u0430\u0441\u0445\u043e\u0434, \u20b8", kind="currency"),
        ],
        rows=[
            {
                "date": row.date,
                "category": row.category or "\u041f\u0440\u043e\u0447\u0435\u0435",
                "detail": row.detail,
                "operation": row.operation,
                "income": row.income,
                "expense": row.expense,
                "direction": row.direction,
            }
            for row in transactions
        ],
        group=PRIMARY_GROUP,
    )


def _direction_label(direction: str) -> str:
    return (
        "\u041f\u0440\u0438\u0445\u043e\u0434"
        if direction == "inflow"
        else "\u0420\u0430\u0441\u0445\u043e\u0434"
    )


def _build_daily_rows(transactions):
    grouped = defaultdict(lambda: {"income_total": 0.0, "expense_total": 0.0, "operations_count": 0})
    for row in transactions:
        current = grouped[row.date]
        current["income_total"] += row.income or 0.0
        current["expense_total"] += row.expense or 0.0
        current["operations_count"] += 1

    rows = []
    for date, values in grouped.items():
        direction = "inflow" if values["income_total"] >= values["expense_total"] else "outflow"
        rows.append(
            {
                "date": date,
                "income_total": round(values["income_total"], 2),
                "expense_total": round(values["expense_total"], 2),
                "net_total": round(values["income_total"] - values["expense_total"], 2),
                "operations_count": values["operations_count"],
                "direction": direction,
            }
        )
    return rows
