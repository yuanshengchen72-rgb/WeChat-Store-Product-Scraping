from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ProductCard:
    product_id: str
    keyword: str = ""
    title: str = ""
    main_image_url: str = ""
    price_fen: int | None = None
    price_text: str = ""
    sales: int | None = None
    sales_text: str = ""
    sales_exact: bool = True
    shop_name: str = ""
    source: str = ""
    captured_at: str = ""

    def __post_init__(self) -> None:
        self.product_id = str(self.product_id or "").strip()
        if not self.captured_at:
            self.captured_at = datetime.now().isoformat(timespec="seconds")

    @property
    def display_price(self) -> str:
        if self.price_fen is not None:
            return f"¥{self.price_fen / 100:.2f}"
        return self.price_text

    @property
    def display_sales(self) -> str:
        if self.sales_text:
            return self.sales_text
        if self.sales is None:
            return ""
        suffix = "" if self.sales_exact else "（约）"
        return f"{self.sales}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["display_price"] = self.display_price
        data["display_sales"] = self.display_sales
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProductCard":
        fields = {
            "product_id",
            "keyword",
            "title",
            "main_image_url",
            "price_fen",
            "price_text",
            "sales",
            "sales_text",
            "sales_exact",
            "shop_name",
            "source",
            "captured_at",
        }
        return cls(**{key: value for key, value in data.items() if key in fields})
