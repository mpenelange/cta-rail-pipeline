MAX_CTA_BYTES = 4 * 1024 * 1024
MAX_RIDERSHIP_BYTES = 8 * 1024 * 1024
MAX_OPENAI_BYTES = 1024 * 1024
MAX_RAW_SNAPSHOT_BYTES = MAX_CTA_BYTES
MAX_TEXT = 8000
MAX_ID = 256
MAX_LIST_ITEMS = 100
MAX_LIST_TEXT = 512
MAX_ERROR = 1000
MAX_API_QUERY = 512


def read_bounded(response, limit, label):
    if isinstance(response, bytes):
        data = response
    else:
        try:
            data = response.read(limit + 1)
        finally:
            close = getattr(response, "close", None)
            if close: close()
    if len(data) > limit:
        raise ValueError(f"{label} response too large (limit {limit} bytes)")
    return data


def bounded_text(value, limit=MAX_TEXT):
    return str(value)[:limit]


def bounded_strings(values, item_limit=MAX_LIST_TEXT, count=MAX_LIST_ITEMS):
    return [bounded_text(value, item_limit) for value in list(values)[:count]]
