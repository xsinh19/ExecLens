"""Fill-price model for marketable child orders on Level-1 data.

Without a real order book, a child order of ``q`` shares is filled at the
touch plus a temporary impact that grows with its size relative to recent
market volume::

    base price  = ask (BUY) | bid (SELL)          of the latest fresh quote
    ratio       = q / max(V_recent, V_min)
    impact_bps  = min(coefficient_bps * ratio ** exponent, max_impact_bps)
    fill price  = base * (1 + side_sign * impact_bps / 10,000)

``V_recent`` is the market volume traded in the ``reference_window_seconds``
up to and including the decision time, so the model never reads future
volume. With the default exponent 0.5 the cost is concave in size (the
"square-root" shape often used in practice): splitting an order into smaller
clips lowers the per-share impact.

The model is deterministic, has no permanent component (the synthetic market
does not react to simulated fills) and is **not** a calibrated estimate of
real market impact; it only makes size and liquidity matter in comparisons.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from execlens.data.schemas import BPS, Side
from execlens.simulation.base import MarketSnapshot


class ImpactModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    coefficient_bps: float = Field(
        default=25.0, ge=0, description="Impact (bps) when a clip equals recent volume."
    )
    exponent: float = Field(default=0.5, gt=0, le=2)
    reference_window_seconds: float = Field(default=300.0, gt=0)
    min_reference_volume: int = Field(
        default=100, ge=1, description="Floor on recent volume to keep the ratio finite."
    )
    max_impact_bps: float = Field(default=500.0, gt=0)

    def impact_bps(self, clip_quantity: int, recent_volume: int) -> float:
        if clip_quantity <= 0:
            return 0.0
        ratio = clip_quantity / max(recent_volume, self.min_reference_volume)
        return float(min(self.coefficient_bps * ratio**self.exponent, self.max_impact_bps))

    def execution_price(self, side: Side, clip_quantity: int, snapshot: MarketSnapshot) -> float:
        """Fill price of a marketable clip at ``snapshot``: the touch plus impact."""
        base = snapshot.ask if side is Side.BUY else snapshot.bid
        if base is None:
            raise ValueError("cannot price a fill without a quote")
        impact = self.impact_bps(clip_quantity, snapshot.trailing_volume)
        return base * (1.0 + side.sign * impact / BPS)
