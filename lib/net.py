import random
import time

from curl_cffi import requests

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
]
PROFILES = ["chrome146", "chrome145", "chrome136", "chrome131"]
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://www.naver.com/",
}
BLOCK_SIGNS = ["자동입력 방지문자", "비정상적인 트래픽", "비정상 트래픽", "보안문자를 입력", "사이트 접속이 일시적으로"]


class CircuitBreakerOpen(Exception):
    pass


class CrawlerError(Exception):
    pass


class SlowdownController:
    CONSECUTIVE_BLOCKS_THRESHOLD = 5
    CONSECUTIVE_SUCCESSES_FOR_ACCELERATION = 10
    ACCELERATION_FACTOR = 0.7
    ACCELERATION_FLOOR = 0.5

    def __init__(self, base=1.5, max_=60.0):
        self.base = base
        self.max_ = max_
        self.current_interval = base
        self.consecutive_blocks = 0
        self.consecutive_successes = 0
        self.adaptive_base = base

    def on_block_detected(self):
        self.consecutive_blocks += 1
        self.consecutive_successes = 0
        self.adaptive_base = self.base
        self.current_interval = min(self.max_, self.current_interval * 2)
        if self.consecutive_blocks >= self.CONSECUTIVE_BLOCKS_THRESHOLD:
            raise CircuitBreakerOpen(f"{self.consecutive_blocks} blocks in a row")

    def on_success(self):
        self.consecutive_blocks = 0
        self.current_interval = max(self.adaptive_base, self.current_interval * 0.5)
        self.consecutive_successes += 1
        if self.consecutive_successes >= self.CONSECUTIVE_SUCCESSES_FOR_ACCELERATION:
            self.adaptive_base = max(self.base * self.ACCELERATION_FLOOR, self.adaptive_base * self.ACCELERATION_FACTOR)
            self.consecutive_successes = 0

    def wait(self):
        if self.current_interval > self.adaptive_base:
            time.sleep(max(0.1, self.current_interval + random.uniform(-0.3, 0.3)))
        else:
            time.sleep(random.uniform(self.adaptive_base, self.adaptive_base * 1.5))


class Crawler:
    SEARCH_URL = "https://search.naver.com/search.naver"

    def __init__(self, slowdown=None):
        self.slowdown = slowdown or SlowdownController()
        self.session = requests.Session(impersonate=random.choice(PROFILES))

    def _headers(self):
        return {**HEADERS, "User-Agent": random.choice(UA)}

    def warmup(self):
        try:
            self.session.get("https://www.naver.com/", headers=self._headers(), timeout=10)
        except Exception:
            pass

    def fetch_search(self, keyword):
        self.slowdown.wait()
        try:
            r = self.session.get(self.SEARCH_URL, params={"query": keyword}, headers=self._headers(), timeout=15)
        except requests.RequestsError as e:
            self.slowdown.on_block_detected()
            raise CrawlerError(f"network error: {e}") from e
        if r.status_code == 429 or len(r.text) < 500 or any(s in r.text for s in BLOCK_SIGNS):
            self.slowdown.on_block_detected()
            raise CrawlerError(f"rate limited (status={r.status_code})")
        if r.status_code != 200:
            raise CrawlerError(f"unexpected status {r.status_code}")
        self.slowdown.on_success()
        return r.text
