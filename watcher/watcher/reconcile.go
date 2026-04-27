package watcher

import (
	"context"
	"crypto/sha256"
	"fmt"
	"log/slog"
	"sort"
	"time"

	"github.com/omnigraph/watcher/config"
)

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
	lastDigest string
	tickCount  int
}

func NewReconciler(queue *LocalQueue, interval time.Duration) *Reconciler {
	return &Reconciler{queue: queue, interval: interval}
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
	if digest == r.lastDigest && r.tickCount%reconcileLogForceEvery != 0 {
		return nil
	}
	r.lastDigest = digest

	if deadCount > config.SemanticDeadRowSoftCap {
		slog.Warn("reconcile_dead_row_cap_exceeded", "dead_count", deadCount, "soft_cap", config.SemanticDeadRowSoftCap)
	}

	if deadCount == 0 && len(errorCounts) == 0 {
		slog.Info("reconcile_tick", "tick", r.tickCount, "dead_count", 0)
		return nil
	}
	slog.Info("reconcile_tick", "tick", r.tickCount, "dead_count", deadCount, "error_classes", errorCounts)
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
