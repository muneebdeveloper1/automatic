from __future__ import annotations
import random, time
from dataclasses import dataclass
from typing import Callable, Optional
from .logging_utils import log

class FailureClass:
    TEMPORARY="TEMPORARY"
    NETWORK="NETWORK_ERROR"
    RATE_LIMIT="RATE_LIMIT"
    SERVER="SERVER_ERROR"
    QUOTA="QUOTA_EXCEEDED"
    AUTH="AUTH_ERROR"
    INVALID="INVALID_REQUEST"
    MISSING="MISSING_FILE"
    CORRUPT="CORRUPT_FILE"
    DISK="DISK_FULL"
    PERMANENT="PERMANENT"

@dataclass
class ClassifiedError(Exception):
    category: str
    message: str
    retry_after: Optional[float] = None
    original: Optional[Exception] = None
    def __str__(self): return self.message

def classify(exc: Exception) -> ClassifiedError:
    msg = str(exc)
    low = msg.lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status is None and getattr(exc, "resp", None) is not None:
        status = getattr(exc.resp, "status", None)
    retry_after = getattr(exc, "retry_after", None)
    if isinstance(status, int):
        if status == 429: return ClassifiedError(FailureClass.RATE_LIMIT,msg,retry_after,exc)
        if status in (408,): return ClassifiedError(FailureClass.NETWORK,msg,retry_after,exc)
        if status >= 500: return ClassifiedError(FailureClass.SERVER,msg,retry_after,exc)
        if status == 401: return ClassifiedError(FailureClass.AUTH,msg,None,exc)
        if status == 403 and any(x in low for x in ("quota","rate","daily limit")):
            return ClassifiedError(FailureClass.QUOTA,msg,retry_after,exc)
        if status == 403: return ClassifiedError(FailureClass.AUTH,msg,None,exc)
        if 400 <= status < 500: return ClassifiedError(FailureClass.INVALID,msg,None,exc)
    if "quota" in low or "resource exhausted" in low or "daily limit" in low:
        return ClassifiedError(FailureClass.QUOTA,msg,retry_after,exc)
    if "429" in low or "rate limit" in low or "too many requests" in low:
        return ClassifiedError(FailureClass.RATE_LIMIT,msg,retry_after,exc)
    if any(x in low for x in ("timeout","timed out","connection reset","connection aborted","temporarily unavailable","temporary failure","503","502","500","network")):
        return ClassifiedError(FailureClass.NETWORK,msg,retry_after,exc)
    if any(x in low for x in ("unauthorized","invalid api key","authentication","permission denied")):
        return ClassifiedError(FailureClass.AUTH,msg,None,exc)
    if any(x in low for x in ("invalid request","invalid argument","bad request")):
        return ClassifiedError(FailureClass.INVALID,msg,None,exc)
    if any(x in low for x in ("no space left","disk full")):
        return ClassifiedError(FailureClass.DISK,msg,None,exc)
    return ClassifiedError(FailureClass.PERMANENT,msg,None,exc)

class RetryEngine:
    def __init__(self, attempts=5, min_delay=5, max_delay=30):
        self.attempts=int(attempts); self.min_delay=float(min_delay); self.max_delay=float(max_delay)

    def run(self, fn: Callable, provider: str, model: str="-", on_failure=None):
        last=None
        for attempt in range(1,self.attempts+1):
            try:
                log.info("provider=%s model=%s attempt=%d/%d",provider,model,attempt,self.attempts)
                return fn()
            except Exception as exc:
                err=classify(exc); last=err
                if on_failure: on_failure(err,attempt,self.attempts)
                if err.category in (FailureClass.AUTH,FailureClass.INVALID,FailureClass.MISSING,FailureClass.CORRUPT,FailureClass.DISK,FailureClass.QUOTA):
                    raise err
                if attempt >= self.attempts: raise err
                delay=err.retry_after if err.retry_after is not None else min(self.max_delay,self.min_delay*(2**(attempt-1)))
                delay *= random.uniform(0.85,1.15)
                log.warning("provider=%s category=%s retry_in=%.1fs error=%s",provider,err.category,delay,err.message[:240])
                time.sleep(delay)
        raise last
