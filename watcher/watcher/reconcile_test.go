package watcher

import (
	"context"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	semanticworker "github.com/omnigraph/watcher/semantic/worker"
)

func TestIsTransientErrorClassification(t *testing.T) {
	cases := []struct {
		msg  string
		want bool
	}{
		{"", true},
		{"context deadline exceeded", true},
		{"Post \"http://hub:9000/batch\": dial tcp: connection refused", true},
		{"hub returned 408", true},
		{"hub returned 429", true},
		{"hub returned 500", true},
		{"hub returned 502", true},
		{"hub returned 503", true},
		{"hub returned 504 gateway timeout", true},
		{"hub returned 599 unknown server error", true},
		{"hub returned 400 bad request", false},
		{"hub returned 404 not found", false},
		// Bare EOF substring used to false-positive parser errors. After
		// narrowing to "unexpected EOF", a Go syntax error stays dead.
		{"expected '}', found 'EOF'", false},
		// Network-layer truncation does still match (io.ErrUnexpectedEOF
		// surfaces this exact phrase from the http transport).
		{"unexpected EOF", true},
		{"read tcp 127.0.0.1:55012->127.0.0.1:9000: i/o timeout", true},
		{"connection reset by peer", true},
		{"broken pipe", true},
		{"permission denied: invalid token", false},
		{"unauthorized", false},
		{"hub returned 422 Unprocessable Entity", false},
		{"hub returned 401", false},
		{"invalid hub schema: missing field", false},
	}
	for _, c := range cases {
		if got := IsTransientError(c.msg); got != c.want {
			t.Errorf("IsTransientError(%q) = %v, want %v", c.msg, got, c.want)
		}
	}
}

// seedDeadJob creates a single semantic job that has been marked dead by
// exhausting maxRetries=1. Returns the job's outbox ID and the lease that
// produced the dead row (already invalidated; for inspection only).
func seedDeadJob(t *testing.T, q *LocalQueue, path, hash, lastErr string) int {
	t.Helper()
	job := semanticworker.Job{
		MachineID:   "m1",
		Project:     "p1",
		Root:        "/repo",
		Path:        path,
		ContentHash: hash,
		Content:     []byte("package main"),
	}
	if err := q.UpsertSemanticJob(job, 1); err != nil {
		t.Fatalf("upsert %s: %v", path, err)
	}
	jobs, err := q.ClaimDueSemanticJobs(1, time.Second)
	if err != nil || len(jobs) == 0 {
		t.Fatalf("claim %s: jobs=%v err=%v", path, jobs, err)
	}
	var claimed *SemanticQueuedJob
	for i := range jobs {
		if jobs[i].Path == path {
			claimed = &jobs[i]
			break
		}
	}
	if claimed == nil {
		t.Fatalf("no claimed job for %s", path)
	}
	if err := q.MarkSemanticJobFailed(claimed.ID, hash, claimed.LeaseVersion, time.Millisecond, lastErr); err != nil {
		t.Fatalf("fail %s: %v", path, err)
	}
	return claimed.ID
}

// fixedNow returns a clock function that yields t every call.
func fixedNow(at time.Time) func() time.Time {
	return func() time.Time { return at }
}

// queryReplayCount reads the replay_count for a given outbox row.
func queryReplayCount(t *testing.T, q *LocalQueue, id int) int {
	t.Helper()
	var rc int
	if err := q.db.QueryRow("SELECT replay_count FROM semantic_jobs WHERE id = ?", id).Scan(&rc); err != nil {
		t.Fatalf("query replay_count: %v", err)
	}
	return rc
}

func queryState(t *testing.T, q *LocalQueue, id int) string {
	t.Helper()
	var s string
	if err := q.db.QueryRow("SELECT state FROM semantic_jobs WHERE id = ?", id).Scan(&s); err != nil {
		t.Fatalf("query state: %v", err)
	}
	return s
}

func TestReconcilerAutoReplaysTransientDeadJob(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	id := seedDeadJob(t, q, "/repo/a.go", "h1", "context deadline exceeded")

	r := NewReconciler(q, time.Hour).WithAutoReplay(AutoReplayPolicy{
		Enabled:   true,
		MinAge:    0, // age gate disabled for this test
		MaxCount:  3,
		BatchSize: 10,
	})
	if err := r.Tick(context.Background()); err != nil {
		t.Fatalf("tick: %v", err)
	}

	if got := queryReplayCount(t, q, id); got != 1 {
		t.Errorf("replay_count = %d, want 1", got)
	}
	if got := queryState(t, q, id); got != string(SemanticJobPending) {
		t.Errorf("state = %s, want pending", got)
	}
}

func TestReconcilerSkipsNonTransientDeadJob(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	id := seedDeadJob(t, q, "/repo/b.go", "h1", "permission denied: invalid token")

	r := NewReconciler(q, time.Hour).WithAutoReplay(AutoReplayPolicy{
		Enabled:   true,
		MinAge:    0,
		MaxCount:  3,
		BatchSize: 10,
	})
	if err := r.Tick(context.Background()); err != nil {
		t.Fatalf("tick: %v", err)
	}

	if got := queryReplayCount(t, q, id); got != 0 {
		t.Errorf("replay_count = %d, want 0 (non-transient should not replay)", got)
	}
	if got := queryState(t, q, id); got != string(SemanticJobDead) {
		t.Errorf("state = %s, want dead", got)
	}
}

func TestReconcilerHonorsMaxReplayCount(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	id := seedDeadJob(t, q, "/repo/c.go", "h1", "i/o timeout")
	// Pre-populate replay_count to the cap so reconciler must skip.
	if _, err := q.db.Exec("UPDATE semantic_jobs SET replay_count = 3 WHERE id = ?", id); err != nil {
		t.Fatalf("seed replay_count: %v", err)
	}
	// IncrementReplayCount above flipped state→pending; revert to dead so
	// the candidate query finds it.
	if _, err := q.db.Exec("UPDATE semantic_jobs SET state = ?, dead_at = ? WHERE id = ?",
		string(SemanticJobDead), time.Now().UnixMilli(), id); err != nil {
		t.Fatalf("revert state: %v", err)
	}

	r := NewReconciler(q, time.Hour).WithAutoReplay(AutoReplayPolicy{
		Enabled:   true,
		MinAge:    0,
		MaxCount:  3,
		BatchSize: 10,
	})
	if err := r.Tick(context.Background()); err != nil {
		t.Fatalf("tick: %v", err)
	}
	if got := queryReplayCount(t, q, id); got != 3 {
		t.Errorf("replay_count = %d, want 3 (cap reached, no further replay)", got)
	}
	if got := queryState(t, q, id); got != string(SemanticJobDead) {
		t.Errorf("state = %s, want dead", got)
	}
}

func TestReconcilerHonorsMinAgeGate(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	id := seedDeadJob(t, q, "/repo/d.go", "h1", "connection refused")

	// dead_at == "now in test"; reconciler clock is also "now in test" so
	// MinAge=10s gate must skip the still-fresh row.
	r := NewReconciler(q, time.Hour).WithAutoReplay(AutoReplayPolicy{
		Enabled:   true,
		MinAge:    10 * time.Second,
		MaxCount:  3,
		BatchSize: 10,
	})
	if err := r.Tick(context.Background()); err != nil {
		t.Fatalf("tick: %v", err)
	}
	if got := queryReplayCount(t, q, id); got != 0 {
		t.Errorf("replay_count = %d, want 0 (still within MinAge)", got)
	}

	// Advance the reconciler clock past MinAge — same row should now replay.
	r.now = fixedNow(time.Now().Add(time.Hour))
	if err := r.Tick(context.Background()); err != nil {
		t.Fatalf("tick advanced: %v", err)
	}
	if got := queryReplayCount(t, q, id); got != 1 {
		t.Errorf("replay_count = %d, want 1 after MinAge window", got)
	}
}

func TestReconcilerPagesPastNonTransientStuckPage(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	// Seed 2 non-transient dead rows first (oldest IDs), then 3 transient.
	// Without cursor paging, BatchSize=2 would fetch only the 2 non-transient
	// rows every tick and the 3 transient rows behind them never get healed.
	nonT1 := seedDeadJob(t, q, "/repo/auth_a.go", "h1", "permission denied: invalid token")
	nonT2 := seedDeadJob(t, q, "/repo/auth_b.go", "h1", "unauthorized")
	t1 := seedDeadJob(t, q, "/repo/x.go", "h1", "context deadline exceeded")
	t2 := seedDeadJob(t, q, "/repo/y.go", "h1", "hub returned 500")
	t3 := seedDeadJob(t, q, "/repo/z.go", "h1", "i/o timeout")

	r := NewReconciler(q, time.Hour).WithAutoReplay(AutoReplayPolicy{
		Enabled:   true,
		MinAge:    0,
		MaxCount:  3,
		BatchSize: 2, // small batch forces cursor advancement
	})
	if err := r.Tick(context.Background()); err != nil {
		t.Fatalf("tick: %v", err)
	}

	// Non-transient rows must remain dead with replay_count == 0.
	for _, id := range []int{nonT1, nonT2} {
		if got := queryReplayCount(t, q, id); got != 0 {
			t.Errorf("non-transient row %d replay_count = %d, want 0", id, got)
		}
		if got := queryState(t, q, id); got != string(SemanticJobDead) {
			t.Errorf("non-transient row %d state = %s, want dead", id, got)
		}
	}
	// At least 2 of the 3 transient rows must have replayed (BatchSize=2).
	transientReplayed := 0
	for _, id := range []int{t1, t2, t3} {
		if queryReplayCount(t, q, id) == 1 {
			transientReplayed++
		}
	}
	if transientReplayed < 2 {
		t.Errorf("transient replayed = %d, want ≥2 (BatchSize)", transientReplayed)
	}
}

func TestReconcilerAllNonTransientTerminates(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	// Seed only non-transient rows. The cursor loop must terminate after
	// scanning them once (round 1 returns the page, round 2 returns empty)
	// rather than spinning until reconcileMaxScanRounds.
	for i := 0; i < 5; i++ {
		seedDeadJob(t, q, fmt.Sprintf("/repo/n%d.go", i), "h1", "permission denied")
	}

	done := make(chan error, 1)
	r := NewReconciler(q, time.Hour).WithAutoReplay(AutoReplayPolicy{
		Enabled:   true,
		MinAge:    0,
		MaxCount:  3,
		BatchSize: 2,
	})
	go func() { done <- r.Tick(context.Background()) }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("tick: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Tick did not terminate within 2s — cursor loop is unbounded")
	}
}

func TestReconcilerCursorAdvancesAcrossTicksToReachStarvedTransient(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	// Seed a non-transient prefix that exceeds BatchSize × MaxScanRounds:
	// BatchSize=1, MaxScanRounds=10 → first 11 IDs are non-transient.
	// Without persistent cursor the 12th (transient) row is permanently
	// starved. With persistent cursor it must heal within a few ticks.
	const prefix = 11
	for i := 0; i < prefix; i++ {
		seedDeadJob(t, q, fmt.Sprintf("/repo/n%d.go", i), "h1", "permission denied")
	}
	transientID := seedDeadJob(t, q, "/repo/yes.go", "h1", "context deadline exceeded")

	r := NewReconciler(q, time.Hour).WithAutoReplay(AutoReplayPolicy{
		Enabled:   true,
		MinAge:    0,
		MaxCount:  3,
		BatchSize: 1,
	})

	// Hit Tick repeatedly; cursor advances each tick. Within a bounded
	// number of ticks (prefix / BatchSize / MaxScanRounds rounded up + 1
	// for the wrap pass) the transient row is reached.
	const maxTicks = prefix + 5
	for i := 0; i < maxTicks; i++ {
		if err := r.Tick(context.Background()); err != nil {
			t.Fatalf("tick %d: %v", i, err)
		}
		if queryReplayCount(t, q, transientID) == 1 {
			return
		}
	}
	t.Fatalf("transient row never replayed after %d ticks (cursor starvation)", maxTicks)
}

func TestReconcilerBatchCapDoesNotAdvancePastUnreplayed(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	// Three transient rows in id ASC order, BatchSize=2: tick 1 must
	// replay the first two. Tick 2 starts AT the third row (cursor not
	// advanced past it), so id3 must replay then — never starve.
	id1 := seedDeadJob(t, q, "/repo/a.go", "h1", "context deadline exceeded")
	id2 := seedDeadJob(t, q, "/repo/b.go", "h1", "i/o timeout")
	id3 := seedDeadJob(t, q, "/repo/c.go", "h1", "connection refused")

	r := NewReconciler(q, time.Hour).WithAutoReplay(AutoReplayPolicy{
		Enabled:   true,
		MinAge:    0,
		MaxCount:  3,
		BatchSize: 2,
	})
	if err := r.Tick(context.Background()); err != nil {
		t.Fatalf("tick1: %v", err)
	}
	if queryReplayCount(t, q, id1) != 1 || queryReplayCount(t, q, id2) != 1 {
		t.Fatalf("tick1: id1=%d id2=%d, want both 1",
			queryReplayCount(t, q, id1), queryReplayCount(t, q, id2))
	}
	if got := queryReplayCount(t, q, id3); got != 0 {
		t.Fatalf("tick1: id3 replay_count = %d, want 0 (BatchSize=2)", got)
	}
	// Re-mark id1 and id2 as dead so the eligibility predicate
	// (state=dead) is satisfied for tick2 — without this all three rows
	// look pending and the cursor query returns nothing.
	if _, err := q.db.Exec("UPDATE semantic_jobs SET state = ?, dead_at = ? WHERE id IN (?, ?)",
		string(SemanticJobDead), time.Now().UnixMilli(), id1, id2); err != nil {
		t.Fatalf("re-dead: %v", err)
	}
	if err := r.Tick(context.Background()); err != nil {
		t.Fatalf("tick2: %v", err)
	}
	if got := queryReplayCount(t, q, id3); got != 1 {
		t.Errorf("tick2: id3 replay_count = %d, want 1 (cursor must not have skipped it)", got)
	}
}

func TestReconcilerCursorWrapsAfterSweep(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	id := seedDeadJob(t, q, "/repo/a.go", "h1", "context deadline exceeded")

	r := NewReconciler(q, time.Hour).WithAutoReplay(AutoReplayPolicy{
		Enabled:   true,
		MinAge:    0,
		MaxCount:  3,
		BatchSize: 5,
	})
	// Tick 1: replay → row state=pending, cursor advanced past id.
	if err := r.Tick(context.Background()); err != nil {
		t.Fatalf("tick1: %v", err)
	}
	if queryState(t, q, id) != string(SemanticJobPending) {
		t.Fatalf("after tick1 state=%s want pending", queryState(t, q, id))
	}
	// Manually re-mark as dead (simulating worker fail again).
	if _, err := q.db.Exec("UPDATE semantic_jobs SET state = ?, dead_at = ?, last_error = ? WHERE id = ?",
		string(SemanticJobDead), time.Now().UnixMilli(), "context deadline exceeded", id); err != nil {
		t.Fatalf("re-dead: %v", err)
	}
	// Tick 2: cursor is past id; without wrap it would return empty and
	// stop. Wrap-to-0 logic must rescan from start and re-replay.
	if err := r.Tick(context.Background()); err != nil {
		t.Fatalf("tick2: %v", err)
	}
	if got := queryReplayCount(t, q, id); got != 2 {
		t.Errorf("replay_count = %d, want 2 after wrap-around", got)
	}
}

// Regression: a row that hits MaxCount once must not be permanently
// excluded from auto-replay if the content (hash) changes — operators
// edit files after outages and the new job deserves its own budget.
func TestUpsertSemanticJobResetsReplayCountOnContentHashChange(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	id := seedDeadJob(t, q, "/repo/main.go", "h1", "context deadline exceeded")
	if _, err := q.db.Exec("UPDATE semantic_jobs SET replay_count = 3 WHERE id = ?", id); err != nil {
		t.Fatalf("seed cap: %v", err)
	}

	newJob := semanticworker.Job{
		MachineID:   "m1",
		Project:     "p1",
		Root:        "/repo",
		Path:        "/repo/main.go",
		ContentHash: "h2", // different hash → upsert path triggers
		Content:     []byte("package main // edited"),
	}
	if err := q.UpsertSemanticJob(newJob, 3); err != nil {
		t.Fatalf("upsert with new hash: %v", err)
	}

	var rc int
	var hash string
	if err := q.db.QueryRow("SELECT replay_count, content_hash FROM semantic_jobs WHERE id = ?", id).
		Scan(&rc, &hash); err != nil {
		t.Fatalf("query: %v", err)
	}
	if rc != 0 {
		t.Errorf("replay_count = %d after content-hash change, want 0 (budget reset)", rc)
	}
	if hash != "h2" {
		t.Errorf("content_hash = %s, want h2", hash)
	}
}

func TestReconcilerDisabledIsReportOnly(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	id := seedDeadJob(t, q, "/repo/e.go", "h1", "context deadline exceeded")

	r := NewReconciler(q, time.Hour) // no WithAutoReplay → policy zero-valued
	if err := r.Tick(context.Background()); err != nil {
		t.Fatalf("tick: %v", err)
	}
	if got := queryReplayCount(t, q, id); got != 0 {
		t.Errorf("replay_count = %d, want 0 (auto-replay disabled)", got)
	}
}
