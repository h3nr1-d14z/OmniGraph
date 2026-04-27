package watcher

import (
	"context"
	"crypto/sha256"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"time"

	"github.com/omnigraph/watcher/config"
)

// AutoReplayPolicy gates how the reconciler heals transient-error dead jobs.
// Zero values disable auto-replay (report-only mode).
type AutoReplayPolicy struct {
	Enabled   bool
	MinAge    time.Duration
	MaxCount  int
	BatchSize int
}

// transientErrorPatterns are case-insensitive substrings that mark an error
// as worth retrying automatically. Auth/permission/parse failures are
// excluded — those need operator attention, not a quiet replay.
//
// "hub returned 5" matches every 5xx the sender raises (the sender error
// format is `hub returned NNN` — see watcher/sender/client.go). 408 (Request
// Timeout) and 429 (Too Many Requests) are the only 4xx codes that are
// retryable per RFC 9110 + Google client-library defaults; everything else
// in 4xx is a client bug and stays dead.
//
// Bare "eof" was removed: substring matching let it false-positive on
// terminal parser errors like `expected '}', found 'EOF'`. Narrower
// "unexpected EOF" still catches the transport-layer case (io.ErrUnexpectedEOF
// surfaces this exact phrase) without churning syntax errors.
var transientErrorPatterns = []string{
	"timeout",
	"timed out",
	"context deadline exceeded",
	"connection refused",
	"connection reset",
	"broken pipe",
	"network is unreachable",
	"no route to host",
	"i/o timeout",
	"unexpected eof",
	"temporary failure",
	"hub returned 408",
	"hub returned 429",
	"hub returned 5",
}

// IsTransientError reports whether the recorded last_error message looks
// retryable. Empty messages are treated as transient (the worker died
// before recording an explicit class).
func IsTransientError(msg string) bool {
	if msg == "" {
		return true
	}
	lower := strings.ToLower(msg)
	for _, pat := range transientErrorPatterns {
		if strings.Contains(lower, pat) {
			return true
		}
	}
	return false
}

// Dead-letter reconciliation loop.
//
// REPORT-ONLY: this loop observes the semantic_jobs outbox and logs the
// dead-row count + per-error-class breakdown on each tick. It does NOT
// auto-replay; manual replay is performed via the `watcher replay <id>`
// subcommand which calls LocalQueue.IncrementReplayCount.
//
// Log dedup: if (dead_count, error_hash) is unchanged from the previous
// tick, the log is suppressed unless reconcileLogForceEvery ticks have
// elapsed. This prevents spamming for stable error classes while still
// surfacing periodic state.
const reconcileLogForceEvery = 24

// First report runs after this short delay so operator sees state at
// startup; subsequent ticks use the configured interval.
const reconcileStartupDelay = 5 * time.Minute

// Reconciler tracks dedup state across ticks for a single watcher process.
type Reconciler struct {
	queue      *LocalQueue
	interval   time.Duration
	policy     AutoReplayPolicy
	lastDigest string
	tickCount  int
	now        func() time.Time
	// replayCursorID persists the last scanned semantic_jobs.id across
	// Tick() calls so a non-transient prefix larger than
	// BatchSize × reconcileMaxScanRounds cannot starve transient rows
	// queued behind it. Wraps to 0 when a tick exhausts all candidates.
	replayCursorID int
}

func NewReconciler(queue *LocalQueue, interval time.Duration) *Reconciler {
	return &Reconciler{queue: queue, interval: interval, now: time.Now}
}

// WithAutoReplay attaches an auto-replay policy. Returns the same reconciler
// so callers can chain construction.
func (r *Reconciler) WithAutoReplay(policy AutoReplayPolicy) *Reconciler {
	if r != nil {
		r.policy = policy
	}
	return r
}

// Tick performs one reconcile pass: counts dead rows, groups by error
// class, logs if state changed (or forced).
func (r *Reconciler) Tick(ctx context.Context) error {
	if r == nil || r.queue == nil {
		return nil
	}
	r.tickCount++

	deadCount, err := r.queue.SemanticJobCount(SemanticJobDead)
	if err != nil {
		return fmt.Errorf("semantic dead count: %w", err)
	}

	errorCounts, err := r.queue.SemanticDeadErrorClassCounts()
	if err != nil {
		return fmt.Errorf("dead error breakdown: %w", err)
	}

	digest := reconcileDigest(deadCount, errorCounts)
	shouldLog := digest != r.lastDigest || r.tickCount%reconcileLogForceEvery == 0
	r.lastDigest = digest

	if shouldLog {
		if deadCount > config.SemanticDeadRowSoftCap {
			slog.Warn("reconcile_dead_row_cap_exceeded", "dead_count", deadCount, "soft_cap", config.SemanticDeadRowSoftCap)
		}
		if deadCount == 0 && len(errorCounts) == 0 {
			slog.Info("reconcile_tick", "tick", r.tickCount, "dead_count", 0)
		} else {
			slog.Info("reconcile_tick", "tick", r.tickCount, "dead_count", deadCount, "error_classes", errorCounts)
		}
	}
	return r.replayDueJobs(ctx)
}

// reconcileMaxScanRounds bounds the cursor-paging loop in replayDueJobs so
// a wholly-dead-letter table can't make a single tick scan unbounded rows.
// Pages × MaxScanRounds is the worst-case work per tick.
const reconcileMaxScanRounds = 10

// replayDueJobs is a no-op when auto-replay is disabled. Otherwise it
// cursor-pages through dead jobs eligible by age + replay budget, skipping
// past non-transient errors so they never block transient ones queued
// behind them, and re-queues those classified transient up to BatchSize.
func (r *Reconciler) replayDueJobs(ctx context.Context) error {
	if r == nil || r.queue == nil {
		return nil
	}
	if !r.policy.Enabled || r.policy.BatchSize <= 0 || r.policy.MaxCount <= 0 {
		return nil
	}
	if err := ctx.Err(); err != nil {
		return err
	}

	nowMs := r.now().UnixMilli()
	minAgeMs := r.policy.MinAge.Milliseconds()

	afterID := r.replayCursorID
	var (
		replayed     int
		skipped      int
		scannedTotal int
		rounds       int
		wrapped      bool
	)
	for replayed < r.policy.BatchSize && rounds < reconcileMaxScanRounds {
		if err := ctx.Err(); err != nil {
			return err
		}
		candidates, err := r.queue.ListReplayCandidates(afterID, nowMs, minAgeMs, r.policy.MaxCount, r.policy.BatchSize)
		if err != nil {
			return fmt.Errorf("list replay candidates: %w", err)
		}
		if len(candidates) == 0 {
			// afterID == 0 means we just scanned from the head and the
			// table has nothing eligible — full sweep is done. Otherwise
			// wrap to 0 so transient rows that aged in behind a prefix
			// still get reached. The reconcileMaxScanRounds cap prevents
			// pathological cycling.
			if afterID == 0 {
				break
			}
			afterID = 0
			wrapped = true
			continue
		}
		rounds++
		scannedTotal += len(candidates)
		for _, c := range candidates {
			if !IsTransientError(c.LastError) {
				skipped++
				if c.ID > afterID {
					afterID = c.ID
				}
				continue
			}
			// Honour BatchSize before advancing the cursor: if we exit
			// here without bumping afterID, next tick re-fetches starting
			// at this same candidate and replays it.
			if replayed >= r.policy.BatchSize {
				break
			}
			if err := r.queue.IncrementReplayCount(c.ID); err != nil {
				slog.Error("reconcile_replay_failed", "job_id", c.ID, "err", err)
				continue
			}
			replayed++
			if c.ID > afterID {
				afterID = c.ID
			}
		}
	}
	r.replayCursorID = afterID

	if replayed > 0 || skipped > 0 {
		slog.Info("reconcile_auto_replay",
			"tick", r.tickCount,
			"replayed", replayed,
			"skipped_non_transient", skipped,
			"scanned_total", scannedTotal,
			"scan_rounds", rounds,
			"cursor_after", afterID,
			"cursor_wrapped", wrapped,
		)
	}
	return nil
}

// Run loops Tick() at the configured interval until ctx is canceled. The
// first tick fires after reconcileStartupDelay so operator sees state
// shortly after startup.
func (r *Reconciler) Run(ctx context.Context) {
	if r == nil || r.interval <= 0 {
		return
	}

	startup := time.NewTimer(reconcileStartupDelay)
	defer startup.Stop()
	select {
	case <-ctx.Done():
		return
	case <-startup.C:
	}

	if err := r.Tick(ctx); err != nil {
		slog.Error("reconcile_tick_error", "err", err)
	}

	ticker := time.NewTicker(r.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := r.Tick(ctx); err != nil {
				slog.Error("reconcile_tick_error", "err", err)
			}
		}
	}
}

func reconcileDigest(deadCount int, errorCounts map[string]int) string {
	keys := make([]string, 0, len(errorCounts))
	for k := range errorCounts {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	h := sha256.New()
	fmt.Fprintf(h, "dead=%d", deadCount)
	for _, k := range keys {
		fmt.Fprintf(h, ";%s=%d", k, errorCounts[k])
	}
	return fmt.Sprintf("%x", h.Sum(nil))
}
