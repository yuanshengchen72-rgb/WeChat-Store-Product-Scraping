from __future__ import annotations

import ast
import html
import json
import re
from collections import deque
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .model import ProductCard


_ID_ALIASES = (
    "product_id",
    "productid",
    "out_product_id",
    "outproductid",
    "spu_id",
    "spuid",
)
_TITLE_ALIASES = (
    "title",
    "product_title",
    "producttitle",
    "product_name",
    "productname",
    "short_title",
    "shorttitle",
)
_IMAGE_ALIASES = (
    "headimg_url",
    "headimgurl",
    "head_img_url",
    "headimageurl",
    "cover_url",
    "coverurl",
    "img_url",
    "imgurl",
    "img_urls",
    "imgurls",
    "image_url",
    "imageurl",
    "image_urls",
    "imageurls",
    "thumb_url",
    "thumburl",
    "product_image_url",
    "productimageurl",
    "head_img",
    "headimg",
    "cover",
)
_PRICE_ALIASES = (
    "selling_price",
    "sellingprice",
    "sale_price",
    "saleprice",
    "min_price",
    "minprice",
    "discounted_price",
    "discountedprice",
    "price",
)
_SALES_ALIASES = (
    "sold",
    "sales",
    "sold_num",
    "soldnum",
    "sale_num",
    "salenum",
    "sales_num",
    "salesnum",
    "total_sold",
    "totalsold",
    "total_sold_num",
    "totalsoldnum",
    "sold_count",
    "soldcount",
    "sale_count",
    "salecount",
)
_SALES_TEXT_ALIASES = (
    "sales_wording",
    "saleswording",
    "sold_wording",
    "soldwording",
    "sales_text",
    "salestext",
    "sold_text",
    "soldtext",
)
_SHOP_NAME_ALIASES = (
    "shop_name",
    "shopname",
    "store_name",
    "storename",
    "platform_name",
    "platformname",
)

_PRODUCT_ID_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:product_?id|out_?product_?id|spu_?id)"
    r"\s*[:=]\s*[\"']?(0[xX][0-9a-fA-F]+|\d{10,18})"
)
_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.I)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+")
_LZ4_START_OFFSETS = (0x4E, 0x60)
_MAX_LZ4_OUTPUT = 16 * 1024 * 1024


def _normal_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def _normalize_pid(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip().strip("\"'")
    try:
        if text.lower().startswith("0x"):
            text = str(int(text, 16))
    except ValueError:
        return ""
    return text if text.isdigit() and 10 <= len(text) <= 18 else ""


def _decode_quoted(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        try:
            return str(json.loads(text)) if text[0] == '"' else str(ast.literal_eval(text))
        except (ValueError, TypeError, SyntaxError):
            return text[1:-1]
    return text


def _clean_url(value: Any) -> str:
    if isinstance(value, Mapping):
        value = _find_mapping_value(value, _IMAGE_ALIASES, max_depth=2)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        value = next((item for item in value if item), "")
        if isinstance(value, Mapping):
            value = _find_mapping_value(value, _IMAGE_ALIASES, max_depth=2)
    text = html.unescape(str(value or ""))
    text = text.replace(r"\/", "/").replace(r"\u0026", "&")
    match = _URL_RE.search(text)
    return match.group(0) if match else ""


def _price_to_fen(value: Any) -> tuple[int | None, str]:
    if value is None or isinstance(value, bool):
        return None, ""
    text = str(value).strip()
    if not text:
        return None, ""
    cleaned = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None, text
    number_text = match.group(0)
    try:
        number = Decimal(number_text)
    except InvalidOperation:
        return None, text
    is_yuan = any(mark in cleaned for mark in ("¥", "￥", "元")) or "." in number_text
    if is_yuan:
        number *= 100
    fen = int(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return (fen if fen >= 0 else None), text


def _sales_value(value: Any) -> tuple[int | None, str, bool]:
    if value is None or isinstance(value, bool):
        return None, "", True
    text = str(value).strip()
    if not text:
        return None, "", True
    match = re.search(r"(\d+(?:\.\d+)?)\s*([万千]?)", text.replace(",", ""))
    if not match:
        return None, text, False
    number = Decimal(match.group(1))
    unit = match.group(2)
    if unit == "万":
        number *= 10000
    elif unit == "千":
        number *= 1000
    count = int(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    exact = not unit and "+" not in text and "以上" not in text
    return count, text, exact


def _json_container(value: Any) -> Any:
    if isinstance(value, (Mapping, list, tuple)):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                decoded = json.loads(stripped)
            except (ValueError, TypeError):
                return None
            if isinstance(decoded, (Mapping, list, tuple)):
                return decoded
    return None


def _find_mapping_value(
    mapping: Mapping[str, Any], aliases: Iterable[str], max_depth: int = 3
) -> Any:
    wanted = {_normal_key(alias) for alias in aliases}
    queue: deque[tuple[Any, int]] = deque([(mapping, 0)])
    seen: set[int] = set()
    while queue:
        node, depth = queue.popleft()
        marker = id(node)
        if marker in seen:
            continue
        seen.add(marker)
        if isinstance(node, Mapping):
            for key, value in node.items():
                if _normal_key(key) in wanted and value not in (None, "", [], {}):
                    return value
            if depth < max_depth:
                for value in node.values():
                    nested = _json_container(value)
                    if nested is not None:
                        queue.append((nested, depth + 1))
        elif isinstance(node, (list, tuple)) and depth < max_depth:
            for value in node:
                nested = _json_container(value)
                if nested is not None:
                    queue.append((nested, depth + 1))
    return None


def _find_product_metric(mapping: Mapping[str, Any], aliases: Iterable[str]) -> Any:
    """Find a metric owned by this product, without entering shop-level stats."""
    direct = _find_mapping_value(mapping, aliases, max_depth=0)
    if direct is not None:
        return direct
    for key, value in mapping.items():
        if _normal_key(key) not in {"extradata", "productextra", "productext"}:
            continue
        nested = _json_container(value)
        if isinstance(nested, Mapping):
            metric = _find_mapping_value(nested, aliases, max_depth=0)
            if metric is not None:
                return metric
    return None


def _iter_mappings(
    value: Any, string_json_depth: int = 0
) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _iter_mappings(nested, string_json_depth)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _iter_mappings(nested, string_json_depth)
    elif isinstance(value, str) and string_json_depth < 4:
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                nested = json.loads(stripped)
            except (ValueError, TypeError):
                return
            if nested != value:
                yield from _iter_mappings(nested, string_json_depth + 1)


def _card_from_mapping(mapping: Mapping[str, Any], source: str) -> ProductCard | None:
    pid = _normalize_pid(_find_mapping_value(mapping, _ID_ALIASES, max_depth=1))
    if not pid:
        return None
    title = str(_find_mapping_value(mapping, _TITLE_ALIASES, max_depth=2) or "").strip()
    image = _clean_url(_find_mapping_value(mapping, _IMAGE_ALIASES, max_depth=3))
    price_raw = _find_mapping_value(mapping, _PRICE_ALIASES, max_depth=3)
    price_fen, price_text = _price_to_fen(price_raw)
    sales_raw = _find_product_metric(mapping, _SALES_ALIASES)
    sales_text_raw = _find_product_metric(mapping, _SALES_TEXT_ALIASES)
    if sales_raw is None and sales_text_raw is None:
        # Store-list responses omit product-level sales when the count is zero,
        # while still including shop_sales (the store-wide total) on each card.
        # Treat that omission as zero without ever copying shop_sales.sales.
        shop_sales = mapping.get("shop_sales")
        if isinstance(shop_sales, Mapping):
            sales_raw = 0
    shop_name = str(
        _find_mapping_value(mapping, _SHOP_NAME_ALIASES, max_depth=2) or ""
    ).strip()
    sales, sales_text, sales_exact = _sales_value(
        sales_raw if sales_raw not in (None, "") else sales_text_raw
    )
    if sales_text_raw:
        sales_text = str(sales_text_raw).strip()
        _, _, text_exact = _sales_value(sales_text)
        sales_exact = sales_exact and text_exact
    if not any((title, image, price_fen is not None, sales is not None)):
        return None
    return ProductCard(
        product_id=pid,
        title=title,
        main_image_url=image,
        price_fen=price_fen,
        price_text=price_text,
        sales=sales,
        sales_text=sales_text,
        sales_exact=sales_exact,
        shop_name=shop_name,
        source=source,
    )


def _balanced_end(text: str, start: int) -> int:
    pairs = {"[": "]", "{": "}"}
    opener = text[start]
    closer = pairs.get(opener)
    if not closer:
        return -1
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _top_level_objects(text: str) -> Iterator[str]:
    index = 0
    while index < len(text):
        if text[index] == "{":
            end = _balanced_end(text, index)
            if end >= 0:
                yield text[index : end + 1]
                index = end + 1
                continue
        index += 1


def _field_value(fragment: str, aliases: Iterable[str]) -> str:
    for alias in aliases:
        pattern = re.compile(
            rf"(?i)(?<![A-Za-z0-9_])[\"']?{re.escape(alias)}[\"']?\s*[:=]\s*"
        )
        match = pattern.search(fragment)
        if not match:
            continue
        index = match.end()
        while index < len(fragment) and fragment[index].isspace():
            index += 1
        if index >= len(fragment):
            continue
        if fragment[index] in "\"'":
            quote = fragment[index]
            end = index + 1
            escaped = False
            while end < len(fragment):
                char = fragment[end]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    return _decode_quoted(fragment[index : end + 1])
                end += 1
        end = index
        while end < len(fragment) and fragment[end] not in ",}\]\r\n":
            end += 1
        return _decode_quoted(fragment[index:end].strip())
    return ""


def _card_from_text(fragment: str, source: str) -> ProductCard | None:
    id_match = _PRODUCT_ID_RE.search(fragment)
    if not id_match:
        return None
    pid = _normalize_pid(id_match.group(1))
    if not pid:
        return None
    title = _field_value(fragment, _TITLE_ALIASES).strip()
    image = _clean_url(_field_value(fragment, _IMAGE_ALIASES))
    if not image:
        url_match = _URL_RE.search(fragment.replace(r"\/", "/"))
        image = url_match.group(0) if url_match else ""
    price_fen, price_text = _price_to_fen(_field_value(fragment, _PRICE_ALIASES))
    sales_raw = _field_value(fragment, _SALES_ALIASES)
    sales_text_raw = _field_value(fragment, _SALES_TEXT_ALIASES)
    shop_name = _field_value(fragment, _SHOP_NAME_ALIASES).strip()
    sales, sales_text, sales_exact = _sales_value(sales_raw or sales_text_raw)
    if sales_text_raw:
        sales_text = sales_text_raw
        _, _, text_exact = _sales_value(sales_text_raw)
        sales_exact = sales_exact and text_exact
    if not any((title, image, price_fen is not None, sales is not None)):
        return None
    return ProductCard(
        product_id=pid,
        title=title,
        main_image_url=image,
        price_fen=price_fen,
        price_text=price_text,
        sales=sales,
        sales_text=sales_text,
        sales_exact=sales_exact,
        shop_name=shop_name,
        source=source,
    )


def _lz4_block_decode(data: bytes) -> bytes:
    """Decode the raw LZ4 block used inside WeChat NoPB responses."""
    index = 0
    output = bytearray()

    def extended_length(length: int) -> int:
        nonlocal index
        if length != 15:
            return length
        while True:
            if index >= len(data):
                raise ValueError("truncated LZ4 length")
            extra = data[index]
            index += 1
            length += extra
            if extra != 255:
                return length

    while index < len(data):
        token = data[index]
        index += 1
        literal_length = extended_length(token >> 4)
        literal_end = index + literal_length
        if literal_end > len(data):
            raise ValueError("truncated LZ4 literal")
        if len(output) + literal_length > _MAX_LZ4_OUTPUT:
            raise ValueError("LZ4 output exceeds safety limit")
        output.extend(data[index:literal_end])
        index = literal_end
        if index == len(data):
            break
        if index + 2 > len(data):
            raise ValueError("truncated LZ4 offset")
        offset = data[index] | (data[index + 1] << 8)
        index += 2
        if offset == 0 or offset > len(output):
            raise ValueError("invalid LZ4 offset")
        match_length = extended_length(token & 0x0F) + 4
        if len(output) + match_length > _MAX_LZ4_OUTPUT:
            raise ValueError("LZ4 output exceeds safety limit")
        match_start = len(output) - offset
        for copied in range(match_length):
            output.append(output[match_start + copied])
    return bytes(output)


def _byte_candidates(payload: bytes) -> Iterator[bytes]:
    yield payload
    for offset in _LZ4_START_OFFSETS:
        if len(payload) <= offset:
            continue
        try:
            decoded = _lz4_block_decode(payload[offset:])
        except (IndexError, ValueError):
            continue
        if b"product_cards" in decoded:
            yield decoded
            break


def _decode_candidates(payload: bytes | str) -> Iterator[str]:
    if isinstance(payload, str):
        yield payload
        return
    seen: set[str] = set()
    for candidate in _byte_candidates(payload):
        for encoding in ("utf-8", "utf-16-le"):
            try:
                text = candidate.decode(encoding, errors="ignore")
            except (LookupError, UnicodeError):
                continue
            text = _CONTROL_RE.sub(" ", text)
            if text and text not in seen:
                seen.add(text)
                yield text


def _json_roots(text: str) -> Iterator[Any]:
    stripped = text.strip()
    candidates = [stripped]
    first_obj, last_obj = stripped.find("{"), stripped.rfind("}")
    first_list, last_list = stripped.find("["), stripped.rfind("]")
    if 0 <= first_obj < last_obj:
        candidates.append(stripped[first_obj : last_obj + 1])
    if 0 <= first_list < last_list:
        candidates.append(stripped[first_list : last_list + 1])
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            yield json.loads(candidate)
        except (ValueError, TypeError):
            continue

    # Mobile WeChat keeps mini-program bridge responses inside larger native
    # memory buffers.  The useful object often starts after a binary prefix,
    # e.g. {"respData":"{\"msg\":...}"}, and may also have unrelated bytes
    # after it.  raw_decode lets us recover that complete embedded object
    # without assuming it occupies the whole buffer.
    decoder = json.JSONDecoder()
    markers = (
        '{"respData"',
        '{"product_cards"',
        '[{"product_id"',
        '{"msg"',
    )
    starts: set[int] = set()
    for marker in markers:
        cursor = 0
        while True:
            start = text.find(marker, cursor)
            if start < 0:
                break
            cursor = start + len(marker)
            if start in starts:
                continue
            starts.add(start)
            try:
                root, end = decoder.raw_decode(text, start)
            except (ValueError, TypeError):
                continue
            candidate = text[start:end]
            if candidate in seen:
                continue
            seen.add(candidate)
            yield root


def _listing_fragments(text: str) -> Iterator[str]:
    lower = text.lower()
    found_listing = False
    cursor = 0
    while True:
        marker = lower.find("product_cards", cursor)
        if marker < 0:
            break
        found_listing = True
        opener_positions = [
            pos for pos in (text.find("[", marker), text.find("{", marker)) if pos >= 0
        ]
        if opener_positions:
            start = min(opener_positions)
            end = _balanced_end(text, start)
            if end >= 0:
                section = text[start + 1 : end] if text[start] == "[" else text[start : end + 1]
                objects = list(_top_level_objects(section))
                if objects:
                    yield from objects
                else:
                    yield section
                cursor = end + 1
                continue
        cursor = marker + len("product_cards")
    if found_listing:
        return
    matches = list(_PRODUCT_ID_RE.finditer(text))
    for index, match in enumerate(matches):
        left = max(0, match.start() - 4096)
        right_bound = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        right = min(len(text), max(match.end() + 4096, right_bound))
        brace = text.rfind("{", left, match.start())
        if brace >= left:
            end = _balanced_end(text, brace)
            if end >= match.end() and end <= right + 8192:
                yield text[brace : end + 1]
                continue
        yield text[left:right]


class ProductCardParser:
    """Parse JSON and WeChat's native product_cards text into lightweight rows."""

    def parse(self, payload: bytes | str, source: str = "listing") -> list[ProductCard]:
        merged: dict[str, ProductCard] = {}
        for text in _decode_candidates(payload):
            json_card_found = False
            for root in _json_roots(text):
                for mapping in _iter_mappings(root):
                    card = _card_from_mapping(mapping, source)
                    if card:
                        json_card_found = True
                        merged[card.product_id] = self._merge(merged.get(card.product_id), card)
            if not json_card_found:
                for fragment in _listing_fragments(text):
                    card = _card_from_text(fragment, source)
                    if card:
                        merged[card.product_id] = self._merge(merged.get(card.product_id), card)
        return list(merged.values())

    @staticmethod
    def _merge(old: ProductCard | None, new: ProductCard) -> ProductCard:
        if old is None:
            return new
        for field in (
            "title",
            "main_image_url",
            "price_text",
            "sales_text",
            "shop_name",
            "source",
        ):
            value = getattr(new, field)
            if value:
                setattr(old, field, value)
        if new.price_fen is not None:
            old.price_fen = new.price_fen
        if new.sales is not None:
            if old.sales is None or new.sales_exact or not old.sales_exact or new.sales >= old.sales:
                old.sales = new.sales
                old.sales_exact = new.sales_exact
        old.captured_at = new.captured_at
        return old
