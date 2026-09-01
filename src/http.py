"""The HTTP layer, and the place every documented source hostility is handled.

Three real behaviours, all verified (plan §1):

  * federalregister.gov and ecfr.gov 302 datacenter User-Agents to
    unblock.federalregister.gov. The subtle part: FOLLOWING that redirect
    returns an HTML page that hashes stably and parses to zero agencies -- it
    looks like a successful, unchanged fetch. So a redirect off the API host is
    a hard failure, never a redirect to follow.
  * ecfr.gov returns HTTP 406 unless the request permits compression. Setting
    Accept-Encoding once on the shared session makes that structurally
    unrepeatable.
  * escs.opm.gov has returned 403 from Akamai. Retry with backoff; a 403 after
    retries is `failed`, never `unchanged`.

The blocks are IP-based, so they do NOT reproduce from a home connection but DO
reproduce on GitHub Actions runners. Test against the behaviour, not against
whether it happens to fire today.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import requests

DEFAULT_UA = (
    "fedstruct/0.1 (+https://github.com/adamkuzee/fedstruct; "
    "public-interest federal org-structure archive)"
)

# federalregister.gov and ecfr.gov reject generic clients by UA and by IP range.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Any redirect to one of these means we were blocked, not moved.
BLOCK_HOSTS = ("unblock.federalregister.gov", "unblock.ecfr.gov")

RETRY_STATUSES = (403, 429, 500, 502, 503, 504)
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2.0
TIMEOUT_SECONDS = 60


class SourceBlocked(RuntimeError):
    """We were bot-blocked. Distinct from 'the data is not there'.

    Never let this become an empty result set: an empty agency list that looks
    like a successful fetch is the worst failure this project can have, because
    it reads downstream as "the government abolished everything" (plan §11 R5).
    """


class FetchFailed(RuntimeError):
    """A transport or status failure after retries."""


@dataclass
class Response:
    url: str
    status: int
    content: bytes
    headers: dict
    from_cache: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8-sig", errors="replace")

    def json(self):
        import json

        return json.loads(self.text)


class Session:
    """A configured requests.Session plus the hostility handling.

    Injected rather than constructed at point of use, so tests can pass a fake
    and never touch the network.
    """

    def __init__(self, *, user_agent: str = BROWSER_UA, contact: str | None = None,
                 timeout: int = TIMEOUT_SECONDS, sleep=time.sleep):
        self._s = requests.Session()
        ua = user_agent
        if contact:
            ua = f"{ua} ({contact})"
        self._s.headers.update({
            "User-Agent": ua,
            # eCFR returns 406 without this. Set once, globally, so it cannot
            # be forgotten at a call site.
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json, text/csv, application/xml;q=0.9, */*;q=0.8",
        })
        self.timeout = timeout
        self.request_count = 0
        self._sleep = sleep

    def get(self, url: str, *, headers: dict | None = None,
            allow_redirects: bool = True) -> Response:
        last_err: Exception | None = None
        for attempt in range(MAX_RETRIES):
            if attempt:
                # Jittered exponential backoff. Jitter matters because several
                # sources sit behind the same CDN and would otherwise retry in
                # lockstep.
                delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                self._sleep(delay + random.uniform(0, delay * 0.3))
            try:
                self.request_count += 1
                r = self._s.get(url, headers=headers or {}, timeout=self.timeout,
                                allow_redirects=allow_redirects)
            except requests.RequestException as exc:
                last_err = exc
                continue

            self._assert_not_blocked(url, r)

            if r.status_code in RETRY_STATUSES:
                last_err = FetchFailed(f"{url} -> HTTP {r.status_code}")
                continue

            return Response(url=r.url, status=r.status_code, content=r.content,
                            headers=dict(r.headers))

        raise FetchFailed(f"{url} failed after {MAX_RETRIES} attempts: {last_err}")

    @staticmethod
    def _assert_not_blocked(url: str, r: requests.Response) -> None:
        """Detect the block that masquerades as success.

        Checked on the final URL and on every hop in the redirect chain,
        because requests follows redirects by default and the chain is the only
        place the evidence survives.
        """
        hops = [h.headers.get("Location", "") for h in r.history] + [r.url]
        for hop in hops:
            if any(h in hop for h in BLOCK_HOSTS):
                raise SourceBlocked(
                    f"{url} redirected to a block page ({hop}). This is a bot "
                    f"block, not a data change. Following it would yield an "
                    f"HTML page that hashes stably and parses to zero rows."
                )
        ctype = r.headers.get("Content-Type", "")
        # An API path answering with HTML is the other shape of the same block.
        if "text/html" in ctype and "/api/" in url:
            raise SourceBlocked(
                f"{url} returned Content-Type {ctype!r} from an API path — "
                f"almost certainly an interstitial block page, not data."
            )
