from decimal import Decimal

from trading212_mcp.trading212.errors import Trading212Error
from trading212_mcp.trading212.models import (
    AllocationHolding,
    AllocationResult,
    PositionsResult,
)


def calculate_allocation(positions: PositionsResult, currency: str) -> AllocationResult:
    if any(p.walletImpact.currency != currency for p in positions.positions):
        raise Trading212Error(
            "Cannot calculate allocation across different wallet currencies."
        )
    total = sum((p.walletImpact.currentValue for p in positions.positions), Decimal(0))
    return AllocationResult(
        environment=positions.environment,
        retrieved_at=positions.retrieved_at,
        currency=currency,
        total_investment_value=total,
        holdings=[
            AllocationHolding(
                instrument=p.instrument,
                current_value=p.walletImpact.currentValue,
                weight_percent=(p.walletImpact.currentValue / total * 100)
                if total
                else None,
            )
            for p in sorted(
                positions.positions,
                key=lambda p: p.walletImpact.currentValue,
                reverse=True,
            )
        ],
    )
