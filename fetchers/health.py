"""
Fetcher observability: rolling health, disk logging, sleep detection, exit summary.

Purely a monitor. It never touches the database, the write path, or the schema — it
only watches what the fetcher reports and makes failure impossible to miss.

Wire-up is three calls:

    health = FetchHealth("stm", poll_interval=20, unit="vehicles")
    ...
    health.begin_poll()                       # once per loop iteration
    health.record_success(n_rows)             # or
    health.record_failure("http", str(err))
    ...
    health.finish()                           # exit summary

Why each check exists — every one of these corresponds to a real overnight failure that
produced no visible signal at the time:

  * consecutive failures / write gap  — the run "succeeded" for 11 hours while writing
    almost nothing; a per-poll success line scrolling past is not a signal.
  * sleep detection                   — the machine idle-slept at 03:11 and woke briefly
    at 03:18, so the process polled once and slept again. Hours showing "621 rows / 397
    distinct vehicles" are one snapshot, not polling.
  * low-yield detection               — a poll returning far below the recent norm is a
    signal, not something to record silently.
  * disk log                          — the only evidence of the failure lived in a
    terminal scrollback that was later closed.
"""

import os
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# A wall-clock jump this many times the poll interval means we lost time somewhere.
SLEEP_FACTOR = 4
# ...and this much unexplained wall-clock time (seconds) is reported as a sleep.
SLEEP_MIN_SEC = 30
# Consecutive failed polls before shouting.
FAIL_STREAK = 3
# Seconds without a successful write before shouting.
DEFAULT_STALE_SEC = 180
# A poll below this fraction of the recent median yield is suspicious.
LOW_YIELD_FRAC = 0.4
# Rolling window for the yield median.
YIELD_WINDOW = 20


def _ts():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class FetchHealth:
    def __init__(self, name, poll_interval, unit="rows", stale_sec=None, log_dir=LOG_DIR,
                 on_wake=None):
        # on_wake() is invoked after a detected sleep, before the next poll, so a caller
        # holding a persistent HTTP session can discard it. Without this the first poll
        # after waking spends its whole timeout on a socket the network already dropped.
        self.on_wake = on_wake
        self.name = name
        self.poll_interval = poll_interval
        self.unit = unit
        self.stale_sec = stale_sec or max(DEFAULT_STALE_SEC, poll_interval * 6)

        self.started_wall = time.time()
        self.started_mono = time.monotonic()
        self._last_mono = None
        self._last_wall = None

        self.polls = 0
        self.ok = 0
        self.rows = 0
        self.errors = Counter()
        self.fail_streak = 0
        self.worst_fail_streak = 0
        self.last_ok_mono = time.monotonic()
        self.longest_gap = 0.0
        self.sleeps = []            # (iso timestamp, seconds lost)
        self.low_yields = 0
        self.recent = deque(maxlen=YIELD_WINDOW)
        self._stale_warned = False

        os.makedirs(log_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = os.path.join(log_dir, f"{name}-{stamp}.log")
        self._fh = open(self.log_path, "a", buffering=1)   # line buffered: survives a kill
        self.log("INFO", f"start name={name} interval={poll_interval}s pid={os.getpid()}")

    # ---------------- logging ----------------

    def log(self, level, msg):
        line = f"{_ts()} [{level}] {msg}"
        try:
            self._fh.write(line + "\n")
        except Exception:
            pass
        if level in ("WARN", "ERROR"):
            print(line, file=sys.stderr)

    def shout(self, title, lines):
        """A warning that cannot be mistaken for a normal per-poll line."""
        bar = "!" * 72
        block = [bar, f"!!  {title}"] + [f"!!  {l}" for l in lines] + [bar]
        out = "\n\n" + "\n".join(block) + "\n"
        print(out, file=sys.stderr, flush=True)
        self.log("WARN", f"{title} | " + " | ".join(lines))

    # ---------------- per-poll ----------------

    def begin_poll(self):
        """Call at the top of each loop iteration. Detects sleep/wake."""
        now_mono, now_wall = time.monotonic(), time.time()
        if self._last_mono is not None:
            mono_d = now_mono - self._last_mono
            wall_d = now_wall - self._last_wall
            # On macOS time.monotonic() pauses while the machine is asleep but the wall
            # clock does not, so the difference between them IS the sleep duration.
            # Falling back to wall-clock alone would also catch a merely slow request,
            # which is why both are compared.
            lost = wall_d - mono_d
            if lost > SLEEP_MIN_SEC:
                self.sleeps.append((_ts(), lost))
                self.shout(
                    f"MACHINE SLEPT — lost {lost/60:.1f} minutes",
                    [f"wall clock advanced {wall_d/60:.1f} min but the process only ran {mono_d:.0f}s",
                     "Data for that period does not exist and cannot be recovered.",
                     "Nothing was asserting sleep prevention (see: pmset -g assertions)."],
                )
                if self.on_wake:
                    try:
                        self.on_wake()
                        self.log("INFO", "reset network session after wake")
                    except Exception as e:
                        self.log("ERROR", f"on_wake handler failed: {e}")
            elif wall_d > max(self.poll_interval * SLEEP_FACTOR, SLEEP_MIN_SEC):
                self.log("WARN", f"slow iteration: {wall_d:.0f}s wall for a "
                                 f"{self.poll_interval}s interval (process was running)")
        self._last_mono, self._last_wall = now_mono, now_wall
        self.polls += 1

    def record_success(self, n_rows, note=""):
        self.ok += 1
        self.rows += n_rows
        gap = time.monotonic() - self.last_ok_mono
        self.longest_gap = max(self.longest_gap, gap)
        self.last_ok_mono = time.monotonic()
        if self.fail_streak:
            self.log("INFO", f"recovered after {self.fail_streak} failed poll(s)")
        self.fail_streak = 0
        self._stale_warned = False

        # a poll that "succeeds" with zero rows is not a success worth trusting
        if n_rows == 0:
            self.log("WARN", f"poll returned 0 {self.unit}{(' ' + note) if note else ''}")
        else:
            self.log("INFO", f"ok {n_rows} {self.unit}{(' ' + note) if note else ''}")
            self._check_low_yield(n_rows)
            self.recent.append(n_rows)

    def record_failure(self, kind, detail=""):
        self.errors[kind] += 1
        self.fail_streak += 1
        self.worst_fail_streak = max(self.worst_fail_streak, self.fail_streak)
        self.log("ERROR", f"{kind}: {detail}")
        if self.fail_streak == FAIL_STREAK or (
                self.fail_streak > FAIL_STREAK and self.fail_streak % 20 == 0):
            self.shout(
                f"{self.fail_streak} CONSECUTIVE FAILED POLLS ({kind})",
                [f"last error: {detail[:120]}",
                 f"no successful write for {(time.monotonic()-self.last_ok_mono)/60:.1f} min",
                 f"log: {self.log_path}"],
            )

    def _check_low_yield(self, n_rows):
        if len(self.recent) < self.recent.maxlen:
            return
        ordered = sorted(self.recent)
        median = ordered[len(ordered) // 2]
        if median > 0 and n_rows < median * LOW_YIELD_FRAC:
            self.low_yields += 1
            self.shout(
                f"LOW YIELD — {n_rows} {self.unit}, recent median {median}",
                [f"that is {n_rows/median:.0%} of normal for the last {len(self.recent)} polls",
                 "Possible causes: upstream degraded, partial feed, or throttling."],
            )

    def check_stale(self):
        """Call each iteration (cheap). Shouts if writes have stopped."""
        idle = time.monotonic() - self.last_ok_mono
        if idle > self.stale_sec and not self._stale_warned:
            self._stale_warned = True
            self.shout(
                f"NO SUCCESSFUL WRITE FOR {idle/60:.1f} MINUTES",
                [f"threshold is {self.stale_sec/60:.1f} min",
                 f"polls attempted {self.polls}, succeeded {self.ok}",
                 f"log: {self.log_path}"],
            )

    def heartbeat_due(self, every_sec=600):
        if not hasattr(self, "_last_hb"):
            self._last_hb = time.monotonic()
            return False
        if time.monotonic() - self._last_hb >= every_sec:
            self._last_hb = time.monotonic()
            return True
        return False

    def heartbeat(self):
        rate = (self.ok / self.polls * 100) if self.polls else 0
        self.log("INFO", f"heartbeat polls={self.polls} ok={self.ok} ({rate:.0f}%) "
                         f"{self.unit}={self.rows} sleeps={len(self.sleeps)} "
                         f"errors={dict(self.errors)}")

    # ---------------- exit ----------------

    def finish(self, reason="stopped"):
        ran_wall = time.time() - self.started_wall
        ran_mono = time.monotonic() - self.started_mono
        expected = int(ran_mono // self.poll_interval) if self.poll_interval else 0
        rate = (self.ok / self.polls * 100) if self.polls else 0

        lines = [
            "",
            "=" * 72,
            f"  {self.name} summary — {reason}",
            "=" * 72,
            f"  wall time            {ran_wall/3600:6.2f} h",
            f"  process time         {ran_mono/3600:6.2f} h"
            + (f"   ({(ran_wall-ran_mono)/3600:.2f} h lost to sleep)" if ran_wall-ran_mono > 60 else ""),
            f"  polls attempted      {self.polls:6d}   (expected ~{expected} at {self.poll_interval}s)",
            f"  polls succeeded      {self.ok:6d}   ({rate:.1f}%)",
            f"  {self.unit + ' written':20s} {self.rows:6d}",
            f"  longest gap          {self.longest_gap/60:6.1f} min between successful writes",
            f"  worst fail streak    {self.worst_fail_streak:6d}",
            f"  low-yield polls      {self.low_yields:6d}",
            f"  sleep events         {len(self.sleeps):6d}",
        ]
        for ts, lost in self.sleeps:
            lines.append(f"      slept {lost/60:.1f} min at {ts}")
        if self.errors:
            lines.append("  errors by type:")
            for kind, n in self.errors.most_common():
                lines.append(f"      {kind:24s} {n}")
        else:
            lines.append("  errors by type:      none")
        lines.append(f"  log file             {self.log_path}")

        # The verdict, so a bad run cannot look like a good one at a glance.
        problems = []
        if self.polls and rate < 90:
            problems.append(f"only {rate:.0f}% of polls succeeded")
        if self.sleeps:
            problems.append(f"{len(self.sleeps)} sleep event(s) — data is missing for those periods")
        if self.longest_gap > self.stale_sec:
            problems.append(f"longest write gap was {self.longest_gap/60:.0f} min")
        if expected and self.ok < expected * 0.9:
            problems.append(f"{self.ok} successful polls vs ~{expected} expected")
        if problems:
            lines.append("")
            lines.append("  ⚠️  THIS RUN WAS DEGRADED:")
            for p in problems:
                lines.append(f"      - {p}")
        else:
            lines.append("")
            lines.append("  ✅ run looks healthy")
        lines.append("=" * 72)

        out = "\n".join(lines)
        print(out)
        for l in lines:
            if l.strip():
                self.log("INFO", "summary| " + l.strip())
        try:
            self._fh.close()
        except Exception:
            pass
        return not problems
