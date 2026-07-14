"""JB2 write outbox (P1-09, DD §4.5): same-transaction enqueue + a drainer
worker that posts queued writes to JobBOSS2 with per-work-order FIFO,
backoff, and manual replay for parked rows."""
