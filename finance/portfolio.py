"""Turn raw holdings into the few things worth showing: asset mix, top holdings, concentration.

Asset class is inferred from the symbol and description (funds and ETFs say what they hold).
It's a heuristic - good enough for a household view, and anything unclear lands in "Stocks".
"""
from __future__ import annotations

import re

import pandas as pd

ASSET_CLASSES = ["Stocks", "Bonds", "Cash"]
CONCENTRATION_LIMIT = 0.10   # a single company above 10% of investments is worth a mention

_CASH = re.compile(r"\b(CASH|MONEY MARKET|MONEY FUND|DEPOSIT|SWEEP|TREASURY BILL|T-BILL|CORE POSITION)\b", re.I)
_BOND = re.compile(r"\b(BOND|BND|BD|FIXED INCOME|TREASURY|TREAS|AGGREGATE|MUNI|MUNICIPAL|MUN|INCOME FD|TIPS|CORP BD)\b", re.I)
_FUND = re.compile(r"\b(ETF|FUND|FD|INDEX|TRUST|PORTFOLIO|ADMIRAL|INVESTOR SHS|SHARES)\b", re.I)
CASH_SYMBOLS = {"CASH", "SPAXX", "FDRXX", "FZFXX", "VMFXX", "VMRXX", "SWVXX", "SNVXX", "TTTXX", "MLCASH"}
BOND_SYMBOLS = {"BND", "AGG", "BNDX", "VGIT", "VGSH", "VGLT", "TLT", "IEF", "SHY", "SCHZ", "SCHR", "VCIT", "VCSH",
                "MUB", "TIP", "VTIP", "LQD", "HYG", "JNK", "FXNAX", "VBTLX", "VBMFX", "BIV", "BSV", "BLV", "SGOV",
                "BIL", "GOVT", "IUSB", "FBND", "CMF", "PWZ", "VTEB", "SUB", "TFI", "NYF"}


def asset_class(symbol: str, description: str = "") -> str:
    sym, desc = (symbol or "").upper().strip(), description or ""
    if sym in CASH_SYMBOLS or _CASH.search(desc) or sym.endswith("XX") and len(sym) == 5:
        return "Cash"
    if sym in BOND_SYMBOLS or _BOND.search(desc):
        return "Bonds"
    return "Stocks"


def is_single_company(symbol: str, description: str = "") -> bool:
    """True for an individual stock (concentration risk), False for funds/ETFs/cash."""
    return asset_class(symbol, description) == "Stocks" and not _FUND.search(description or "")


def summarize(holdings: pd.DataFrame) -> dict:
    """{'total', 'by_class': {class: value}, 'top': DataFrame, 'concentrated': [(symbol, share)]}"""
    if holdings.empty:
        return {"total": 0.0, "by_class": {}, "top": holdings, "concentrated": [], "cost_basis": None, "gain": None}
    h = holdings.copy()
    h["class"] = [asset_class(s, d) for s, d in zip(h["symbol"], h["description"])]
    total = float(h["value"].sum())
    by_class = {c: float(h.loc[h["class"] == c, "value"].sum()) for c in ASSET_CLASSES}
    # the same stock held in two accounts counts once
    if "cost_basis" not in h:
        h["cost_basis"] = None
    per_symbol = (h.groupby(["symbol"], as_index=False)
                   .agg(description=("description", "first"), value=("value", "sum"),
                        cost_basis=("cost_basis", lambda c: c.sum(min_count=1)),
                        accounts=("account", lambda s: ", ".join(sorted(set(s)))), asset_class=("class", "first"))
                   .sort_values("value", ascending=False))
    per_symbol["gain"] = per_symbol["value"] - per_symbol["cost_basis"]
    known = h["cost_basis"].notna()
    cost = float(h.loc[known, "cost_basis"].sum()) if known.any() else None
    gain = float(h.loc[known, "value"].sum()) - cost if cost is not None else None
    per_symbol["share"] = per_symbol["value"] / total if total else 0.0
    concentrated = [(r.symbol, r.share) for r in per_symbol.itertuples()
                    if r.share > CONCENTRATION_LIMIT and is_single_company(r.symbol, r.description)]
    return {"total": total, "by_class": by_class, "top": per_symbol.head(10), "concentrated": concentrated,
            "cost_basis": cost, "gain": gain}
