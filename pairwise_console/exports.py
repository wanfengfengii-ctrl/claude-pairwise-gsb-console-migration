import io
import zipfile
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple
from xml.sax.saxutils import escape


DELIVERY_COLUMNS = (
    "项目编号", "Pair ID", "题目", "任务类型", "系统类型", "语言/框架", "出题难度", "实际复评难度",
    "难度复评依据", "题面", "main SHA",
    "A SessionID", "A PromptID", "A 提交", "A 提交永久链接", "A Docker 验收",
    "A 录像状态", "A 录像 SHA256", "B SessionID", "B PromptID", "B 提交",
    "B 提交永久链接", "B Docker 验收", "B 录像状态", "B 录像 SHA256",
    "GSB 结论", "A 评价", "B 评价", "审核人", "确认时间", "模型复检", "资料完整性",
    "正式提交状态", "完成时间",
)


def delivery_row(item: Dict[str, Any]) -> List[Any]:
    remote = str(item.get("remote_url") or "").removesuffix(".git")
    link = lambda arm: "%s/commit/%s" % (remote, item.get(arm + "_commit")) if remote and item.get(arm + "_commit") else ""
    return [
        item.get("project_number"), item.get("pair_id"), item.get("title"), item.get("task_type"),
        item.get("project_category"), item.get("stack"), item.get("original_difficulty") or item.get("difficulty"),
        item.get("assessed_difficulty") or item.get("difficulty"), item.get("difficulty_reason") or "",
        item.get("prompt"), item.get("main_sha"), item.get("a_session_id"),
        item.get("a_prompt_id"), item.get("a_commit"), link("a"), item.get("a_check_status"),
        item.get("a_recording_status"), item.get("a_recording_sha"), item.get("b_session_id"),
        item.get("b_prompt_id"), item.get("b_commit"), link("b"), item.get("b_check_status"),
        item.get("b_recording_status"), item.get("b_recording_sha"), item.get("verdict"),
        item.get("a_reason"), item.get("b_reason"), item.get("confirmed_by"), item.get("confirmed_at"),
        item.get("recheck_status") or "未复检", item.get("readiness"),
        item.get("submission_status") or "not_submitted", item.get("completed_at"),
    ]


def build_xlsx(rows: Iterable[Dict[str, Any]]) -> Tuple[bytes, str]:
    values = [list(DELIVERY_COLUMNS)] + [delivery_row(row) for row in rows]
    sheet_rows = []
    for row_index, row in enumerate(values, 1):
        cells = []
        for column_index, value in enumerate(row, 1):
            ref = _column_name(column_index) + str(row_index)
            text = "" if value is None else str(value)
            style = ' s="1"' if row_index == 1 else ""
            cells.append('<c r="%s" t="inlineStr"%s><is><t xml:space="preserve">%s</t></is></c>' % (
                ref, style, escape(text),
            ))
        sheet_rows.append('<row r="%d">%s</row>' % (row_index, "".join(cells)))
    final_column = _column_name(len(DELIVERY_COLUMNS))
    sheet = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
    <cols><col min="1" max="%d" width="22" customWidth="1"/></cols>
    <sheetData>%s</sheetData><autoFilter ref="A1:%s%d"/></worksheet>""" % (
        len(DELIVERY_COLUMNS), "".join(sheet_rows), final_column, len(values),
    )
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>""",
        "xl/workbook.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="A-B完成轮次" sheetId="1" r:id="rId1"/></sheets></workbook>""",
        "xl/_rels/workbook.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>""",
        "xl/styles.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Arial"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Arial"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF276749"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>""",
        "xl/worksheets/sheet1.xml": sheet,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content.encode("utf-8"))
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return output.getvalue(), "ab-gsb-completed-pairs-%s.xlsx" % stamp


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result
