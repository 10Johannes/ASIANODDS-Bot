from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from openpyxl import Workbook

# Preferred column order; any extra fields returned by the API will be appended alphabetically.
DEFAULT_COLUMNS: List[str] = [
    "betId",
    "betStatus",
    "betType",
    "sportId",
    "sportName",
    "leagueId",
    "leagueName",
    "eventId",
    "eventStartTime",
    "team",
    "team1",
    "team2",
    "handicap",
    "price",
    "risk",
    "win",
    "placedDate",
    "updatedDate",
    "uniqueRequestId",
    "channel",
]


def _resolve_columns(bets: Iterable[Dict[str, Any]]) -> List[str]:
    """Build a stable list of columns covering both defaults and any dynamic fields."""
    columns = list(DEFAULT_COLUMNS)
    extras: List[str] = []
    for bet in bets:
        for key in bet.keys():
            if key not in columns and key not in extras:
                extras.append(key)
    extras.sort()
    columns.extend(extras)
    return columns


def _normalize_cell(value: Any) -> Any:
    """Flatten nested structures for spreadsheet display."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return value


def export_bets_to_excel(bets: List[Dict[str, Any]], file_path: str) -> str:
    """Create an Excel workbook from AsianOdds bet data and return the written path."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "Wager History"

    if not bets:
        ws.append(["No bets returned for the requested period."])
    else:
        columns = _resolve_columns(bets)
        ws.append(columns)
        winloss_values: List[float] = []
        for bet in bets:
            row = []
            for column in columns:
                value = bet.get(column, "")
                row.append(_normalize_cell(value))
                if column == "winLoss":
                    try:
                        winloss_values.append(float(value))
                    except (TypeError, ValueError):
                        winloss_values.append(0.0)
            ws.append(row)

        winloss_idx = columns.index("winLoss") if "winLoss" in columns else -1
        if winloss_idx >= 0 and winloss_values:
            total_loss = sum(v for v in winloss_values if v < 0)
            total_win = sum(v for v in winloss_values if v > 0)
            total_sum = sum(winloss_values)

            ws.append([])
            for label, value in (
                ("Total Loss", total_loss),
                ("Total Win", total_win),
                ("Total Sum", total_sum),
            ):
                summary_row = [""] * len(columns)
                summary_row[0] = label
                summary_row[winloss_idx] = round(value, 2)
                ws.append(summary_row)

    wb.save(path)
    return str(path)

