"""Выпадающие списки книги Google при переносе в «Таблицы».

Почему набор существует. В книгах BBC («Журнал ГК BBC») на строках стоят
списки, и человек выбирает значение, а не печатает его. В нашем зеркале их не
было: правило проверки данных читалось только ради флажков. Колонка приезжала
обычным текстом — и любая правка в зеркале шла мимо словаря книги.

Сеть здесь не нужна: справочник-ссылка разрешается подменной функцией. Это же и
проверяется — что разбор вкладки не ходит в Google сам.
"""
from __future__ import annotations

from app.webexcel.univer import convert_tab


def cell(value: str = "", condition: dict | None = None) -> dict:
    out: dict = {"effectiveValue": {"stringValue": value}} if value else {}
    if condition is not None:
        out["dataValidation"] = {"condition": condition}
    return out


def tab(rows: list[list[dict]], *, title: str = "Журнал") -> dict:
    return {
        "spreadsheet_title": "Книга",
        "spreadsheet_locale": "ru_RU",
        "sheet": {
            "properties": {"sheetId": 1, "title": title, "gridProperties": {}},
            "data": [{"rowData": [{"values": row} for row in rows]}],
        },
    }


def test_spisok_znacheniyami_perenositsya():
    condition = {
        "type": "ONE_OF_LIST",
        "values": [{"userEnteredValue": "Оплачено"}, {"userEnteredValue": "Ожидание"}],
    }
    result = convert_tab(tab([[cell("Статус")], [cell("Оплачено", condition)]]))

    assert len(result["lists"]) == 1
    assert result["lists"][0]["values"] == ["Оплачено", "Ожидание"]
    assert result["lists"][0]["ranges"] == [
        {"startRow": 1, "endRow": 1, "startColumn": 0, "endColumn": 0}
    ]


def test_spisok_ssylkoy_na_spravochnik_razreshaetsya():
    """Так сделаны списки в книгах BBC: `='Справочник'!$I$2:$I`."""
    condition = {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": "='Справочник'!$I$2:$I"}]}
    asked: list[str] = []

    def resolve(ref: str) -> list[str]:
        asked.append(ref)
        return ["Аренда", "Зарплата", ""]

    result = convert_tab(
        tab([[cell("Статья")], [cell("Аренда", condition)], [cell("", condition)]]),
        resolve,
    )

    assert asked == ["='Справочник'!$I$2:$I"]  # один диапазон — одно чтение
    assert result["lists"][0]["values"] == ["Аренда", "Зарплата"]  # пустые не берём


def test_odin_spravochnik_chitaetsya_odin_raz_na_sotni_yacheek():
    """Иначе перенос книги выбрал бы квоту Google на первой же колонке."""
    condition = {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": "='Справочник'!$A$1:$A"}]}
    calls = 0

    def resolve(ref: str) -> list[str]:
        nonlocal calls
        calls += 1
        return ["да", "нет"]

    rows = [[cell("x", condition) for _ in range(5)] for _ in range(20)]
    result = convert_tab(tab(rows), resolve)

    assert calls == 1
    assert len(result["lists"]) == 1
    assert len(result["lists"][0]["ranges"]) == 5  # по диапазону на колонку


def test_nechitaemyy_spravochnik_ne_lomaet_vkladku():
    """Список — удобство. Отказ открыть книгу из-за него был бы несоразмерен."""
    condition = {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": "='Нет такой'!$A$1:$A"}]}

    def resolve(ref: str) -> list[str]:
        raise RuntimeError("вкладка не найдена")

    result = convert_tab(tab([[cell("Статья")], [cell("Аренда", condition)]]), resolve)

    assert result["lists"] == []
    assert result["sheet"]["cellData"]["1"]["0"]["v"] == "Аренда"


def test_bez_razreshitelya_ssylki_prosto_ne_dayut_spiska():
    """Разбор вкладки не ходит в Google сам — сеть приносят снаружи."""
    condition = {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": "='Справочник'!$A$1:$A"}]}
    result = convert_tab(tab([[cell("Аренда", condition)]]))

    assert result["lists"] == []


def test_flazhki_ostalis_flazhkami():
    """Проверка на то, что новый разбор не съел старый случай."""
    condition = {"type": "BOOLEAN"}
    rows = [[{"effectiveValue": {"boolValue": True}, "dataValidation": {"condition": condition}}]]
    result = convert_tab(tab(rows))

    assert result["checkboxes"] == [
        {"startRow": 0, "endRow": 0, "startColumn": 0, "endColumn": 0}
    ]
    assert result["lists"] == []
    # Флажок Univer рисуется по единице, а не по слову TRUE.
    assert result["sheet"]["cellData"]["0"]["0"]["v"] == 1
