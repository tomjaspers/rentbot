import random
import time

import httpx

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


class Http:
    def __init__(self, timeout: float = 20, detail_delay: tuple[float, float] = (2, 5)):
        self.client = httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True)
        self.detail_delay = detail_delay

    def get(self, url: str, params: dict | None = None, retries: int = 2) -> str:
        for attempt in range(retries + 1):
            try:
                r = self.client.get(url, params=params)
                r.raise_for_status()
                return r.text
            except httpx.HTTPError as e:
                permanent = isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500 \
                    and e.response.status_code != 429  # e.g. 404, or 405 when a site blocks us
                if attempt == retries or permanent:
                    raise
                time.sleep(5 * 2**attempt)
        raise AssertionError("unreachable")

    def pause(self) -> None:
        time.sleep(random.uniform(*self.detail_delay))
