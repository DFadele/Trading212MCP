from datetime import datetime
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

Money = Annotated[Decimal, Field(allow_inf_nan=False)]


class APIModel(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class Cash(APIModel):
    availableToTrade: Money
    inPies: Money
    reservedForOrders: Money


class Investments(APIModel):
    currentValue: Money
    realizedProfitLoss: Money
    totalCost: Money
    unrealizedProfitLoss: Money


class AccountSummary(APIModel):
    id: int
    currency: str = Field(min_length=3, max_length=3)
    cash: Cash
    investments: Investments
    totalValue: Money


class AccountSummaryResult(BaseModel):
    environment: Literal["demo", "live"]
    retrieved_at: datetime
    account: AccountSummary


class Instrument(APIModel):
    currency: str = Field(min_length=3, max_length=3)
    isin: str
    name: str
    ticker: str


class PositionWalletImpact(APIModel):
    currency: str = Field(min_length=3, max_length=3)
    currentValue: Money
    fxImpact: Money | None = None
    totalCost: Money
    unrealizedProfitLoss: Money


class Position(APIModel):
    averagePricePaid: Money
    createdAt: datetime
    currentPrice: Money
    instrument: Instrument
    quantity: Money
    quantityAvailableForTrading: Money
    quantityInPies: Money
    walletImpact: PositionWalletImpact


class PositionsResult(BaseModel):
    environment: Literal["demo", "live"]
    retrieved_at: datetime
    positions: list[Position]


class Order(APIModel):
    id: int
    createdAt: datetime
    currency: str
    instrument: Instrument
    type: str
    side: str
    status: str
    strategy: str
    initiatedFrom: str | None = None
    ticker: str | None = None
    extendedHours: bool | None = None
    timeInForce: str | None = None
    quantity: Money | None = None
    filledQuantity: Money | None = None
    value: Money | None = None
    filledValue: Money | None = None
    limitPrice: Money | None = None
    stopPrice: Money | None = None


class Tax(APIModel):
    chargedAt: datetime
    currency: str
    name: str
    quantity: Money


class FillWalletImpact(APIModel):
    currency: str
    fxRate: Money | None = None
    netValue: Money
    realisedProfitLoss: Money | None = None
    taxes: list[Tax] = Field(default_factory=list[Tax])


class Fill(APIModel):
    id: int
    filledAt: datetime
    price: Money
    quantity: Money
    type: str
    tradingMethod: str | None = None
    walletImpact: FillWalletImpact | None = None


class HistoricalOrder(APIModel):
    order: Order
    fill: Fill | None = None


class Dividend(APIModel):
    amount: Money
    currency: str
    paidOn: datetime
    quantity: Money
    reference: str
    type: str
    instrument: Instrument | None = None
    ticker: str | None = None
    tickerCurrency: str | None = None
    amountInEuro: Money | None = None
    grossAmountPerShare: Money | None = None


class Transaction(APIModel):
    amount: Money
    currency: str
    dateTime: datetime
    reference: str
    type: str


class PendingOrdersResult(BaseModel):
    environment: Literal["demo", "live"]
    retrieved_at: datetime
    orders: list[Order]


Item = TypeVar("Item")


class HistoryPage(APIModel, Generic[Item]):
    items: list[Item]
    nextPagePath: str | None


class HistoryResult(BaseModel, Generic[Item]):
    environment: Literal["demo", "live"]
    retrieved_at: datetime
    items: list[Item]
    next_cursor: str | None


class AllocationHolding(BaseModel):
    instrument: Instrument
    current_value: Money
    weight_percent: Money | None


class AllocationResult(BaseModel):
    environment: Literal["demo", "live"]
    retrieved_at: datetime
    currency: str
    basis: Literal["open_positions_excluding_cash"] = "open_positions_excluding_cash"
    total_investment_value: Money
    holdings: list[AllocationHolding]
