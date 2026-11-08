"""Market-history return summaries using the existing provider service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from trading212_mcp.market.models import MarketError
from trading212_mcp.market.resolution import currency_unit
from trading212_mcp.market.service import period_start
from trading212_mcp.quantitative.analytics import risk_metrics

if TYPE_CHECKING:
    from trading212_mcp.market.service import MarketService


async def get_market_data(
    service: MarketService,
    tickers: list[str],
    lookback_years: int,
    include_fundamentals: bool,
    include_series: bool = False,
    base_currency: str | None = None,
) -> dict[str, Any]:
    end = datetime.now(timezone.utc).date()
    start = period_start(lookback_years, end)
    results: list[dict[str, Any]] = []
    for symbol in dict.fromkeys(tickers):
        result: dict[str, Any] = {
            "requested_symbol": symbol,
            "missing_data": [],
            "warnings": [],
        }
        try:
            company = await service.provider.profile(symbol)
            result["company"] = company.model_dump(mode="json")
            result["currency"] = base_currency or currency_unit(company.currency)[0]
            try:
                series, history, warnings = await service.converted_history(
                    company, start, end, result["currency"]
                )
                returns = series.pct_change(fill_method=None).dropna()
                result.update(
                    {
                        "source": history.source,
                        "retrieved_at": history.retrieved_at.isoformat(),
                        "price_basis": history.price_basis,
                        "observations": int(series.count()),
                        "start": cast(pd.Timestamp, series.first_valid_index())
                        .date()
                        .isoformat()
                        if series.notna().any()
                        else None,
                        "end": cast(pd.Timestamp, series.last_valid_index())
                        .date()
                        .isoformat()
                        if series.notna().any()
                        else None,
                        "warnings": warnings,
                        "return_summary": {
                            "cumulative_return": float(
                                series.dropna().iloc[-1] / series.dropna().iloc[0] - 1
                            )
                            if series.count() > 1
                            else None,
                            "mean_daily_return": float(returns.mean())
                            if len(returns)
                            else None,
                            "risk_metrics": risk_metrics(returns)
                            if len(returns) >= 2
                            else None,
                        },
                    }
                )
                if include_series:
                    result["series"] = [
                        {
                            "date": cast(pd.Timestamp, index).date().isoformat(),
                            "adjusted_close": float(value) if pd.notna(value) else None,
                            "return": float(returns.loc[cast(pd.Timestamp, index)])
                            if index in returns.index
                            else None,
                        }
                        for index, value in series.items()
                    ]
            except MarketError as exc:
                result["missing_data"].append("historical_prices")
                result["warnings"].append(str(exc))
            if include_fundamentals:
                try:
                    fundamentals = await service.provider.fundamentals(company)
                    result["fundamentals"] = fundamentals.model_dump(mode="json")
                    result["missing_data"].extend(
                        name
                        for name in type(fundamentals).model_fields
                        if getattr(fundamentals, name) is None
                    )
                except MarketError as exc:
                    result["missing_data"].append("fundamentals")
                    result["warnings"].append(str(exc))
        except MarketError as exc:
            result["missing_data"].append("company_or_provider_access")
            result["warnings"].append(str(exc))
        results.append(result)
    return {
        "source": service.provider.name,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "requested_period": {"start": start.isoformat(), "end": end.isoformat()},
        "assets": results,
    }
