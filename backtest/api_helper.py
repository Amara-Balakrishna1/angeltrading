import time
from threading import Lock

class RateLimitedSmartApi:
    """
    Wrapper around Angel One's SmartConnect API instance to guarantee 
    compliance with API rate limits and avoid HTTP 429 HTML responses.
    """
    def __init__(self, smart_api_instance, max_requests_per_sec=1.0):
        self.api = smart_api_instance
        self.min_interval = 1.0 / max_requests_per_sec
        self.last_call_time = 0.0
        self._lock = Lock()

    def _rate_limit(self):
        """Enforces a strict minimum delay between consecutive outbound calls."""
        with self._lock:
            elapsed = time.monotonic() - self.last_call_time
            left_to_wait = self.min_interval - elapsed
            if left_to_wait > 0:
                time.sleep(left_to_wait)
            self.last_call_time = time.monotonic()

    def getCandleData(self, params, max_retries=5):
        """
        Rate-limited wrapper for smart_api.getCandleData() with retry logic.
        Handles non-JSON rate limit responses safely via exponential backoff.
        """
        for attempt in range(1, max_retries + 1):
            # Block thread until rate limit window is safe
            self._rate_limit()

            try:
                res = self.api.getCandleData(params)

                # Validate response structure
                if isinstance(res, dict) and res.get("status"):
                    return res
                
                # Invalid payload status retry handling
                print(f"⚠️ API returned invalid status (Attempt {attempt}/{max_retries}): {res}")
                time.sleep(3.0 * attempt)

            except Exception as e:
                err_msg = str(e)
                # Catch non-JSON 429 rate limit HTML responses
                if "Access denied" in err_msg or "exceeding access rate" in err_msg or "429" in err_msg:
                    backoff_delay = 10.0 * (2 ** (attempt - 1))  # Exponential delay: 10s, 20s, 40s...
                    print(f"🛑 Rate limit exceeded! Cooling down for {backoff_delay}s (Attempt {attempt}/{max_retries})...")
                    time.sleep(backoff_delay)
                else:
                    print(f"❌ API Exception (Attempt {attempt}/{max_retries}): {e}")
                    time.sleep(3.0 * attempt)

        return None  # Failed after all retries

    def __getattr__(self, name):
        """Pass through all unhandled method calls directly to the underlying SmartApi instance."""
        return getattr(self.api, name)