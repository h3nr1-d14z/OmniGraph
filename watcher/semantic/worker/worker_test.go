package worker

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/omnigraph/watcher/models"
)

func TestWorkerSendsResolvedRelations(t *testing.T) {
	resolver := &fakeResolver{relations: []models.Relation{{Type: "CALLS_RESOLVED", Source: "main", Target: "fmt.Println"}}}
	sender := &fakeSender{}
	w := New(Config{JobTimeout: time.Second}, resolver, sender)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w.Start(ctx)
	defer w.Stop()

	if !w.Enqueue(testJob("hash-1")) {
		t.Fatal("enqueue failed")
	}
	waitFor(t, func() bool { return sender.count() == 1 })

	if got := sender.snapshot()[0].Job.ContentHash; got != "hash-1" {
		t.Fatalf("sent hash = %s", got)
	}
}

func TestWorkerCoalescesPendingJobsByPath(t *testing.T) {
	resolver := &fakeResolver{relations: []models.Relation{{Type: "CALLS_RESOLVED", Source: "main", Target: "fmt.Println"}}}
	sender := &fakeSender{}
	w := New(Config{JobTimeout: time.Second}, resolver, sender)

	if !w.Enqueue(testJob("old")) {
		t.Fatal("enqueue old failed")
	}
	if !w.Enqueue(testJob("new")) {
		t.Fatal("enqueue new failed")
	}
	if got := w.PendingLen(); got != 1 {
		t.Fatalf("pending len = %d", got)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w.Start(ctx)
	defer w.Stop()
	waitFor(t, func() bool { return sender.count() == 1 })
	if got := sender.snapshot()[0].Job.ContentHash; got != "new" {
		t.Fatalf("sent hash = %s", got)
	}
}

func TestWorkerDropsStaleResult(t *testing.T) {
	release := make(chan struct{})
	resolver := &fakeResolver{
		relations: []models.Relation{{Type: "CALLS_RESOLVED", Source: "main", Target: "fmt.Println"}},
		block:     release,
	}
	sender := &fakeSender{}
	w := New(Config{JobTimeout: time.Second}, resolver, sender)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w.Start(ctx)
	defer w.Stop()

	if !w.Enqueue(testJob("old")) {
		t.Fatal("enqueue old failed")
	}
	waitFor(t, func() bool { return resolver.startedCount() == 1 })
	if !w.Enqueue(testJob("new")) {
		t.Fatal("enqueue new failed")
	}
	close(release)
	waitFor(t, func() bool { return sender.count() == 1 })
	if got := sender.snapshot()[0].Job.ContentHash; got != "new" {
		t.Fatalf("sent hash = %s", got)
	}
}

func TestWorkerTimeoutSkipsSend(t *testing.T) {
	resolver := &fakeResolver{
		relations: []models.Relation{{Type: "CALLS_RESOLVED", Source: "main", Target: "fmt.Println"}},
		block:     make(chan struct{}),
	}
	sender := &fakeSender{}
	w := New(Config{JobTimeout: 20 * time.Millisecond}, resolver, sender)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w.Start(ctx)
	defer w.Stop()

	if !w.Enqueue(testJob("hash-1")) {
		t.Fatal("enqueue failed")
	}
	time.Sleep(80 * time.Millisecond)
	if got := sender.count(); got != 0 {
		t.Fatalf("sent count = %d", got)
	}
}

func TestWorkerCapacityRejectsNewPath(t *testing.T) {
	w := New(Config{QueueCapacity: 1}, &fakeResolver{}, &fakeSender{})
	if !w.Enqueue(testJob("hash-1")) {
		t.Fatal("first enqueue failed")
	}
	other := testJob("hash-2")
	other.Path = "/src/other.go"
	if w.Enqueue(other) {
		t.Fatal("expected capacity rejection")
	}
}

func TestWorkerResolverErrorSkipsSend(t *testing.T) {
	resolver := &fakeResolver{err: errors.New("resolver failed")}
	sender := &fakeSender{}
	w := New(Config{JobTimeout: time.Second}, resolver, sender)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w.Start(ctx)
	defer w.Stop()

	if !w.Enqueue(testJob("hash-1")) {
		t.Fatal("enqueue failed")
	}
	time.Sleep(50 * time.Millisecond)
	if got := sender.count(); got != 0 {
		t.Fatalf("sent count = %d", got)
	}
}

func TestWorkerStartStopIsIdempotent(t *testing.T) {
	resolver := &fakeResolver{relations: []models.Relation{{Type: "CALLS_RESOLVED"}}}
	sender := &fakeSender{}
	w := New(Config{JobTimeout: time.Second}, resolver, sender)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	w.Start(ctx)
	w.Start(ctx)
	if !w.Enqueue(testJob("hash-1")) {
		t.Fatal("enqueue failed")
	}
	waitFor(t, func() bool { return sender.count() == 1 })
	w.Stop()
	w.Stop()
}

func TestWorkerStopCancelsBlockedResolver(t *testing.T) {
	resolver := &fakeResolver{
		relations: []models.Relation{{Type: "CALLS_RESOLVED"}},
		block:     make(chan struct{}),
	}
	sender := &fakeSender{}
	w := New(Config{JobTimeout: time.Second}, resolver, sender)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w.Start(ctx)
	if !w.Enqueue(testJob("hash-1")) {
		t.Fatal("enqueue failed")
	}
	waitFor(t, func() bool { return resolver.startedCount() == 1 })
	w.Stop()
	if got := sender.count(); got != 0 {
		t.Fatalf("sent count = %d", got)
	}
}

func TestWorkerRequeuesLatestJobOnSendFailure(t *testing.T) {
	resolver := &fakeResolver{relations: []models.Relation{{Type: "CALLS_RESOLVED"}}}
	sender := &fakeSender{failures: 1}
	w := New(Config{JobTimeout: time.Second, RetryDelay: time.Millisecond}, resolver, sender)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w.Start(ctx)
	defer w.Stop()

	if !w.Enqueue(testJob("hash-1")) {
		t.Fatal("enqueue failed")
	}
	waitFor(t, func() bool { return sender.count() == 2 })
	if got := sender.snapshot()[1].Job.ContentHash; got != "hash-1" {
		t.Fatalf("retried hash = %s", got)
	}
}

func TestWorkerDropsJobAfterMaxSendRetries(t *testing.T) {
	resolver := &fakeResolver{relations: []models.Relation{{Type: "CALLS_RESOLVED"}}}
	sender := &fakeSender{failures: 10}
	w := New(Config{JobTimeout: time.Second, RetryDelay: time.Millisecond, MaxRetries: 2}, resolver, sender)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w.Start(ctx)
	defer w.Stop()

	if !w.Enqueue(testJob("hash-1")) {
		t.Fatal("enqueue failed")
	}
	waitFor(t, func() bool { return sender.count() == 3 })
	time.Sleep(20 * time.Millisecond)
	if got := sender.count(); got != 3 {
		t.Fatalf("sent count = %d", got)
	}
	if got := w.PendingLen(); got != 0 {
		t.Fatalf("pending len = %d", got)
	}
}

type fakeResolver struct {
	mu        sync.Mutex
	started   int
	relations []models.Relation
	err       error
	block     chan struct{}
}

func (f *fakeResolver) Resolve(ctx context.Context, job Job) ([]models.Relation, error) {
	f.mu.Lock()
	f.started++
	f.mu.Unlock()
	if f.block != nil {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-f.block:
		}
	}
	return f.relations, f.err
}

func (f *fakeResolver) startedCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.started
}

type sentBatch struct {
	Job       Job
	Relations []models.Relation
}

type fakeSender struct {
	mu       sync.Mutex
	sent     []sentBatch
	failures int
}

func (f *fakeSender) Send(ctx context.Context, job Job, relations []models.Relation) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.sent = append(f.sent, sentBatch{Job: job, Relations: relations})
	if f.failures > 0 {
		f.failures--
		return errors.New("send failed")
	}
	return nil
}

func (f *fakeSender) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.sent)
}

func (f *fakeSender) snapshot() []sentBatch {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]sentBatch, len(f.sent))
	copy(out, f.sent)
	return out
}

func testJob(hash string) Job {
	return Job{
		MachineID:   "m1",
		Project:     "demo",
		Root:        "/repo",
		Path:        "/src/main.go",
		ContentHash: hash,
		Content:     []byte("package main"),
	}
}

func waitFor(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("condition not met")
}
