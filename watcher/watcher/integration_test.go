package watcher

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/omnigraph/watcher/config"
	"github.com/omnigraph/watcher/models"
	semanticworker "github.com/omnigraph/watcher/semantic/worker"
	"github.com/omnigraph/watcher/sender"
)

type receivedBatches struct {
	mu      sync.Mutex
	batches []models.BatchPayload
}

func (r *receivedBatches) append(batch models.BatchPayload) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.batches = append(r.batches, batch)
}

func (r *receivedBatches) snapshot() []models.BatchPayload {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]models.BatchPayload, len(r.batches))
	copy(out, r.batches)
	return out
}

func startTestWatcher(t *testing.T, dir string, debounceMs int, batchSize int, status int) (*receivedBatches, *DebouncedWatcher, func()) {
	t.Helper()

	received := &receivedBatches{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/batch" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		if auth := r.Header.Get("Authorization"); auth != "Bearer test-token" {
			t.Errorf("unexpected auth: %s", auth)
		}

		var payload models.BatchPayload
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode error: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		received.append(payload)
		w.WriteHeader(status)
	}))

	cfg := &config.WatcherConfig{
		MachineID:  "test-machine",
		WatchRoot:  dir,
		AutoDetect: true,
		Markers:    config.DefaultProjectMarkers,
	}
	cfg.Hub.URL = server.URL
	cfg.Hub.AuthToken = "test-token"
	cfg.Hub.DebounceMs = debounceMs
	cfg.Hub.BatchSec = 2
	cfg.Hub.BatchSize = batchSize

	filter, err := NewIgnoreFilter(dir, false, false, nil)
	if err != nil {
		t.Fatalf("ignore filter: %v", err)
	}
	resolver := NewProjectResolver(dir, config.DefaultProjectMarkers)
	client := sender.NewClient(cfg.Hub.URL, cfg.Hub.AuthToken, cfg.MachineID)
	queue, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("queue: %v", err)
	}

	dw, err := NewDebouncedWatcher(cfg, filter, resolver, client, queue)
	if err != nil {
		t.Fatalf("new watcher: %v", err)
	}
	if err := dw.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}

	cleanup := func() {
		dw.Stop()
		queue.Close()
		server.Close()
	}
	return received, dw, cleanup
}

func collectEvents(batches []models.BatchPayload, base string) []models.FileEvent {
	var events []models.FileEvent
	for _, batch := range batches {
		for _, ev := range batch.Events {
			if filepath.Base(ev.Path) == base {
				events = append(events, ev)
			}
		}
	}
	return events
}

func waitForTestCondition(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("condition not met")
}

type fakeSemanticResolver struct {
	mu        sync.Mutex
	jobs      []semanticworker.Job
	relations []models.Relation
	block     chan struct{}
}

func (f *fakeSemanticResolver) Resolve(ctx context.Context, job semanticworker.Job) ([]models.Relation, error) {
	f.mu.Lock()
	f.jobs = append(f.jobs, job)
	f.mu.Unlock()
	if f.block != nil {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-f.block:
		}
	}
	return f.relations, nil
}

func (f *fakeSemanticResolver) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.jobs)
}

func TestProjectResolverResolveRootFallsBackToWatchRoot(t *testing.T) {
	dir := t.TempDir()
	pr := NewProjectResolver(dir, []string{"go.mod"})
	if err := pr.Discover(); err != nil {
		t.Fatalf("discover: %v", err)
	}
	path := filepath.Join(dir, "nested", "main.go")
	if got := pr.ResolveRoot(path); got != dir {
		t.Fatalf("resolve root = %s, want %s", got, dir)
	}
}

func TestSemanticDisabledByDefaultDoesNotCreateWorker(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.WatcherConfig{MachineID: "test-machine", WatchRoot: dir}
	cfg.Hub.BatchSec = 60
	cfg.Hub.BatchSize = 50
	filter, err := NewIgnoreFilter(dir, false, false, nil)
	if err != nil {
		t.Fatalf("ignore filter: %v", err)
	}
	client := sender.NewClient("http://127.0.0.1", "test-token", cfg.MachineID)
	dw, err := NewDebouncedWatcher(cfg, filter, nil, client, nil)
	if err != nil {
		t.Fatalf("new watcher: %v", err)
	}
	defer dw.Stop()

	if dw.semantic != nil {
		t.Fatal("semantic worker should be nil when semantic.enabled is false")
	}
	dw.enqueueSemantic([]models.FileEvent{{
		Type:        models.EventModify,
		Path:        filepath.Join(dir, "main.go"),
		Project:     "demo",
		MachineID:   cfg.MachineID,
		ContentHash: "hash-1",
		Content:     "package main\nfunc main() {}\n",
	}})
}

func TestSemanticEnabledSendsSemanticOnlyBatch(t *testing.T) {
	dir := t.TempDir()
	received := &receivedBatches{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload models.BatchPayload
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode error: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		received.append(payload)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	cfg := &config.WatcherConfig{MachineID: "test-machine", WatchRoot: dir}
	cfg.Hub.URL = server.URL
	cfg.Hub.AuthToken = "test-token"
	cfg.Hub.BatchSec = 60
	cfg.Hub.BatchSize = 50
	cfg.Semantic.Enabled = true
	cfg.Semantic.WorkerCount = 1
	cfg.Semantic.QueueCapacity = 10
	cfg.Semantic.TimeoutMs = 1000
	cfg.Semantic.RetryDelayMs = 10
	cfg.Semantic.MaxRetries = 2
	client := sender.NewClient(cfg.Hub.URL, cfg.Hub.AuthToken, cfg.MachineID)
	queue, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("queue: %v", err)
	}
	defer queue.Close()
	resolver := &fakeSemanticResolver{relations: []models.Relation{{Type: "CALLS_RESOLVED", Source: "main", Target: "fmt.Println", SymbolID: "go:fmt.Println"}}}
	dw := &DebouncedWatcher{
		cfg:             cfg,
		client:          client,
		queue:           queue,
		lastContentHash: newBoundedHashMap(lastContentHashLimit),
		ctx:             context.Background(),
		semanticNotify:  make(chan struct{}, 1),
	}
	retryDelay := time.Duration(cfg.Semantic.RetryDelayMs) * time.Millisecond
	dw.semantic = semanticworker.New(semanticConfig(cfg), resolver, semanticBatchSender{client: client, queue: queue, retryDelay: retryDelay})
	dw.semantic.Start(dw.ctx)
	defer dw.semantic.Stop()

	dw.markDurable([]models.FileEvent{{Path: filepath.Join(dir, "main.go"), ContentHash: "hash-1"}})
	dw.enqueueSemantic([]models.FileEvent{{
		Type:        models.EventModify,
		Path:        filepath.Join(dir, "main.go"),
		Project:     "demo",
		MachineID:   cfg.MachineID,
		ContentHash: "hash-1",
		Content:     "package main\nfunc main() {}\n",
	}})
	dw.drainSemanticJobs()

	waitForTestCondition(t, func() bool { return len(received.snapshot()) == 1 })
	batch := received.snapshot()[0]
	if batch.Project != "demo" || batch.MachineID != cfg.MachineID {
		t.Fatalf("unexpected batch scope: %#v", batch)
	}
	if len(batch.Events) != 1 {
		t.Fatalf("expected one semantic event, got %#v", batch.Events)
	}
	event := batch.Events[0]
	if event.Content != "" || len(event.Entities) != 0 || len(event.Relations) != 1 {
		t.Fatalf("expected semantic-only event, got %#v", event)
	}
	if event.ContentHash != "hash-1" || event.Path == "" {
		t.Fatalf("unexpected semantic event identity: %#v", event)
	}
	if done, err := queue.SemanticJobCount(SemanticJobDone); err != nil || done != 1 {
		t.Fatalf("done semantic jobs = %d err=%v", done, err)
	}
}

func TestSemanticDrainLogsWhenWorkerQueueIsFull(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.WatcherConfig{MachineID: "test-machine", WatchRoot: dir}
	cfg.Semantic.WorkerCount = 1
	cfg.Semantic.QueueCapacity = 1
	cfg.Semantic.TimeoutMs = 1000
	cfg.Semantic.RetryDelayMs = 10
	cfg.Semantic.MaxRetries = 1
	queue, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("queue: %v", err)
	}
	defer queue.Close()
	dw := &DebouncedWatcher{
		cfg:             cfg,
		queue:           queue,
		lastContentHash: newBoundedHashMap(lastContentHashLimit),
		ctx:             context.Background(),
	}
	dw.semantic = semanticworker.New(semanticworker.Config{QueueCapacity: 1, WorkerCount: 1, JobTimeout: time.Second}, &fakeSemanticResolver{block: make(chan struct{})}, semanticBatchSender{client: sender.NewClient("http://127.0.0.1", "test-token", cfg.MachineID), queue: queue, retryDelay: time.Millisecond})
	if !dw.semantic.Enqueue(semanticworker.Job{MachineID: cfg.MachineID, Project: "demo", Path: filepath.Join(dir, "busy.go"), ContentHash: "busy", Content: []byte("package main\n")}) {
		t.Fatal("failed to prefill semantic worker queue")
	}
	if err := queue.UpsertSemanticJob(semanticworker.Job{
		MachineID:   cfg.MachineID,
		Project:     "demo",
		Root:        dir,
		Path:        filepath.Join(dir, "queued.go"),
		ContentHash: "hash-1",
		Content:     []byte("package main\nfunc queued() {}\n"),
	}, cfg.Semantic.MaxRetries); err != nil {
		t.Fatalf("upsert semantic job: %v", err)
	}

	oldStderr := os.Stderr
	readPipe, writePipe, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	os.Stderr = writePipe
	defer func() { os.Stderr = oldStderr }()

	dw.drainSemanticJobs()
	writePipe.Close()
	var buf bytes.Buffer
	if _, err := buf.ReadFrom(readPipe); err != nil {
		t.Fatalf("read stderr: %v", err)
	}
	if !strings.Contains(buf.String(), "semantic enqueue skipped") {
		t.Fatalf("expected semantic enqueue warning, got %q", buf.String())
	}
}

func TestSemanticRelationCacheHitAndEviction(t *testing.T) {
	cache := newSemanticRelationCache(1)
	job1 := semanticworker.Job{Root: "/repo", Path: "/repo/main.go", ContentHash: "hash-1"}
	job2 := semanticworker.Job{Root: "/repo", Path: "/repo/main.go", ContentHash: "hash-2"}
	relations := []models.Relation{{Type: "CALLS_RESOLVED", Target: "fmt.Println"}}

	cache.put(semanticCacheKey(job1), relations)
	relations[0].Target = "mutated"
	cached, ok := cache.get(semanticCacheKey(job1))
	if !ok || cached[0].Target != "fmt.Println" {
		t.Fatalf("cache hit returned %#v ok=%v", cached, ok)
	}
	cached[0].Target = "mutated-again"
	cached, ok = cache.get(semanticCacheKey(job1))
	if !ok || cached[0].Target != "fmt.Println" {
		t.Fatalf("cache should clone on get, got %#v ok=%v", cached, ok)
	}

	cache.put(semanticCacheKey(job2), []models.Relation{{Type: "CALLS_RESOLVED", Target: "helper"}})
	if _, ok := cache.get(semanticCacheKey(job1)); ok {
		t.Fatal("expected oldest cache entry to be evicted")
	}
	if cached, ok := cache.get(semanticCacheKey(job2)); !ok || cached[0].Target != "helper" {
		t.Fatalf("expected newest cache entry, got %#v ok=%v", cached, ok)
	}
}

func TestSemanticRelationCacheCanBeDisabled(t *testing.T) {
	cache := newSemanticRelationCache(0)
	job := semanticworker.Job{Root: "/repo", Path: "/repo/main.go", ContentHash: "hash-1"}
	cache.put(semanticCacheKey(job), []models.Relation{{Type: "CALLS_RESOLVED", Target: "fmt.Println"}})
	if _, ok := cache.get(semanticCacheKey(job)); ok {
		t.Fatal("expected disabled cache to miss")
	}
}

func newTestSemanticWorker(cfg *config.WatcherConfig, client *sender.Client, queue *LocalQueue, resolver semanticworker.Resolver) *semanticworker.Worker {
	retryDelay := time.Duration(cfg.Semantic.RetryDelayMs) * time.Millisecond
	return semanticworker.New(semanticConfig(cfg), resolver, semanticBatchSender{client: client, queue: queue, retryDelay: retryDelay})
}

func TestBlockedSemanticResolverDoesNotDelaySyntaxSend(t *testing.T) {
	dir := t.TempDir()
	received := &receivedBatches{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload models.BatchPayload
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode error: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		received.append(payload)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	cfg := &config.WatcherConfig{MachineID: "test-machine", WatchRoot: dir}
	cfg.Hub.URL = server.URL
	cfg.Hub.AuthToken = "test-token"
	cfg.Hub.BatchSec = 60
	cfg.Hub.BatchSize = 50
	cfg.Semantic.WorkerCount = 1
	cfg.Semantic.QueueCapacity = 10
	cfg.Semantic.TimeoutMs = 1000
	cfg.Semantic.RetryDelayMs = 10
	cfg.Semantic.MaxRetries = 1
	client := sender.NewClient(cfg.Hub.URL, cfg.Hub.AuthToken, cfg.MachineID)
	queue, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("queue: %v", err)
	}
	defer queue.Close()
	resolver := &fakeSemanticResolver{relations: []models.Relation{{Type: "CALLS_RESOLVED", Target: "fmt.Println"}}, block: make(chan struct{})}
	dw := &DebouncedWatcher{
		cfg:             cfg,
		client:          client,
		queue:           queue,
		lastContentHash: newBoundedHashMap(lastContentHashLimit),
		ctx:             context.Background(),
		semanticNotify:  make(chan struct{}, 1),
	}
	dw.semantic = newTestSemanticWorker(cfg, client, queue, resolver)
	dw.semantic.Start(dw.ctx)
	defer dw.semantic.Stop()

	events := []models.FileEvent{{
		Type:        models.EventModify,
		Path:        filepath.Join(dir, "main.go"),
		Project:     "demo",
		MachineID:   cfg.MachineID,
		ContentHash: "hash-1",
		Content:     "package main\nfunc main() {}\n",
	}}
	if !dw.sendOrQueue("demo", events) {
		t.Fatal("syntax send failed")
	}
	dw.enqueueSemantic(events)
	dw.drainSemanticJobs()

	waitForTestCondition(t, func() bool { return len(received.snapshot()) == 1 && resolver.count() == 1 })
	if got := received.snapshot()[0].Events[0].Content; got == "" {
		t.Fatal("expected syntax event content to be sent before blocked semantic resolution")
	}
}

func TestDrainQueueEnqueuesSemanticAfterSuccessfulReplay(t *testing.T) {
	dir := t.TempDir()
	received := &receivedBatches{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload models.BatchPayload
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode error: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		received.append(payload)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	cfg := &config.WatcherConfig{MachineID: "test-machine", WatchRoot: dir}
	cfg.Hub.URL = server.URL
	cfg.Hub.AuthToken = "test-token"
	cfg.Hub.BatchSec = 60
	cfg.Hub.BatchSize = 50
	cfg.Semantic.WorkerCount = 1
	cfg.Semantic.QueueCapacity = 10
	cfg.Semantic.TimeoutMs = 1000
	cfg.Semantic.RetryDelayMs = 10
	cfg.Semantic.MaxRetries = 1
	client := sender.NewClient(cfg.Hub.URL, cfg.Hub.AuthToken, cfg.MachineID)
	queue, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("queue: %v", err)
	}
	defer queue.Close()
	resolver := &fakeSemanticResolver{relations: []models.Relation{{Type: "CALLS_RESOLVED", Source: "main", Target: "fmt.Println", SymbolID: "go:fmt.Println"}}}
	dw := &DebouncedWatcher{
		cfg:             cfg,
		client:          client,
		queue:           queue,
		lastContentHash: newBoundedHashMap(lastContentHashLimit),
		ctx:             context.Background(),
	}
	dw.semantic = newTestSemanticWorker(cfg, client, queue, resolver)
	dw.semantic.Start(dw.ctx)
	defer dw.semantic.Stop()

	events := []models.FileEvent{{
		Type:        models.EventModify,
		Path:        filepath.Join(dir, "main.go"),
		Project:     "demo",
		MachineID:   cfg.MachineID,
		ContentHash: "hash-1",
		Content:     "package main\nfunc main() {}\n",
	}}
	if err := queue.Enqueue(cfg.MachineID, "demo", events); err != nil {
		t.Fatalf("enqueue: %v", err)
	}
	if err := dw.DrainQueue(); err != nil {
		t.Fatalf("drain queue: %v", err)
	}
	dw.drainSemanticJobs()

	waitForTestCondition(t, func() bool { return len(received.snapshot()) == 2 })
	batches := received.snapshot()
	if batches[0].Events[0].Content == "" {
		t.Fatalf("expected replayed syntax event first, got %#v", batches[0].Events[0])
	}
	if len(batches[1].Events[0].Relations) != 1 || batches[1].Events[0].Content != "" {
		t.Fatalf("expected semantic-only replay follow-up, got %#v", batches[1].Events[0])
	}
}

func TestDebouncedWatcher_CreateModifyDelete(t *testing.T) {
	dir := t.TempDir()

	var received []models.BatchPayload
	var mu sync.Mutex

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/batch" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		auth := r.Header.Get("Authorization")
		if auth != "Bearer test-token" {
			t.Errorf("unexpected auth: %s", auth)
		}

		var payload models.BatchPayload
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode error: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		mu.Lock()
		received = append(received, payload)
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	cfg := &config.WatcherConfig{
		MachineID:  "test-machine",
		WatchRoot:  dir,
		AutoDetect: true,
		Markers:    config.DefaultProjectMarkers,
	}
	cfg.Hub.URL = server.URL
	cfg.Hub.AuthToken = "test-token"
	cfg.Hub.DebounceMs = 500
	cfg.Hub.BatchSec = 2

	filter, err := NewIgnoreFilter(dir, false, false, nil)
	if err != nil {
		t.Fatalf("ignore filter: %v", err)
	}
	resolver := NewProjectResolver(dir, config.DefaultProjectMarkers)
	client := sender.NewClient(cfg.Hub.URL, cfg.Hub.AuthToken, cfg.MachineID)
	queue, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("queue: %v", err)
	}
	defer queue.Close()

	dw, err := NewDebouncedWatcher(cfg, filter, resolver, client, queue)
	if err != nil {
		t.Fatalf("new watcher: %v", err)
	}
	if err := dw.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}
	defer dw.Stop()

	// Create file
	f1 := filepath.Join(dir, "main.go")
	if err := os.WriteFile(f1, []byte("package main\nfunc main() {}\n"), 0644); err != nil {
		t.Fatalf("write: %v", err)
	}

	// Modify file
	time.Sleep(100 * time.Millisecond)
	if err := os.WriteFile(f1, []byte("package main\nfunc main() { println(\"hello\") }\n"), 0644); err != nil {
		t.Fatalf("write2: %v", err)
	}

	// Wait for debounce + flush
	time.Sleep(800 * time.Millisecond)

	mu.Lock()
	if len(received) == 0 {
		mu.Unlock()
		t.Fatal("no batches received")
	}
	last := received[len(received)-1]
	mu.Unlock()

	if last.MachineID != "test-machine" {
		t.Errorf("machine_id = %s", last.MachineID)
	}
	if len(last.Events) == 0 {
		t.Fatal("no events in batch")
	}

	// Verify at least one event is for main.go
	found := false
	for _, ev := range last.Events {
		if filepath.Base(ev.Path) == "main.go" {
			found = true
			if ev.Type != models.EventCreate && ev.Type != models.EventModify {
				t.Errorf("unexpected event type: %s", ev.Type)
			}
			if len(ev.Entities) == 0 {
				t.Error("expected AST entities for Go file")
			}
			foundFunc := false
			for _, ent := range ev.Entities {
				if ent.Name == "main" && ent.Type == "function" {
					foundFunc = true
				}
			}
			if !foundFunc {
				t.Errorf("expected 'main' function entity, got %v", ev.Entities)
			}
		}
	}
	if !found {
		t.Errorf("main.go not found in events: %v", last.Events)
	}

	// Test delete
	os.Remove(f1)
	time.Sleep(800 * time.Millisecond)

	mu.Lock()
	foundDelete := false
	for _, batch := range received {
		for _, ev := range batch.Events {
			if ev.Type == models.EventDelete && filepath.Base(ev.Path) == "main.go" {
				foundDelete = true
			}
		}
	}
	mu.Unlock()
	if !foundDelete {
		t.Error("DELETE event not received")
	}
}

func TestDebouncedWatcher_CoalescesRapidWrites(t *testing.T) {
	dir := t.TempDir()
	received, _, cleanup := startTestWatcher(t, dir, 300, 50, http.StatusOK)
	defer cleanup()

	path := filepath.Join(dir, "main.go")
	for i := 0; i < 5; i++ {
		content := fmt.Sprintf("package main\nfunc main() { println(%d) }\n", i)
		if err := os.WriteFile(path, []byte(content), 0644); err != nil {
			t.Fatalf("write %d: %v", i, err)
		}
		time.Sleep(30 * time.Millisecond)
	}

	time.Sleep(700 * time.Millisecond)

	batches := received.snapshot()
	mainEvents := collectEvents(batches, "main.go")
	if len(mainEvents) != 1 {
		t.Fatalf("expected 1 coalesced main.go event, got %d: %#v", len(mainEvents), mainEvents)
	}
	if mainEvents[0].Content != "package main\nfunc main() { println(4) }\n" {
		t.Fatalf("expected final content, got %q", mainEvents[0].Content)
	}
	if len(mainEvents[0].Entities) == 0 {
		t.Fatal("expected AST entities")
	}
}

func TestDebouncedWatcher_SkipsUnchangedContent(t *testing.T) {
	dir := t.TempDir()
	received, _, cleanup := startTestWatcher(t, dir, 200, 50, http.StatusOK)
	defer cleanup()

	path := filepath.Join(dir, "main.go")
	content := []byte("package main\nfunc main() {}\n")
	if err := os.WriteFile(path, content, 0644); err != nil {
		t.Fatalf("write: %v", err)
	}
	time.Sleep(500 * time.Millisecond)

	if err := os.WriteFile(path, content, 0644); err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	time.Sleep(500 * time.Millisecond)

	mainEvents := collectEvents(received.snapshot(), "main.go")
	if len(mainEvents) != 1 {
		t.Fatalf("expected unchanged rewrite to be skipped, got %d events", len(mainEvents))
	}
	if mainEvents[0].ContentHash == "" {
		t.Fatal("expected content hash")
	}
}

func TestDebouncedWatcher_ChunksByBatchSize(t *testing.T) {
	dir := t.TempDir()
	received, _, cleanup := startTestWatcher(t, dir, 200, 2, http.StatusOK)
	defer cleanup()

	for i := 0; i < 5; i++ {
		path := filepath.Join(dir, fmt.Sprintf("file%d.go", i))
		content := fmt.Sprintf("package main\nfunc f%d() {}\n", i)
		if err := os.WriteFile(path, []byte(content), 0644); err != nil {
			t.Fatalf("write file %d: %v", i, err)
		}
	}
	time.Sleep(600 * time.Millisecond)

	batches := received.snapshot()
	if len(batches) != 3 {
		t.Fatalf("expected 3 batches for 5 events with batch_size=2, got %d", len(batches))
	}
	count := 0
	for _, batch := range batches {
		if len(batch.Events) > 2 {
			t.Fatalf("batch too large: %d", len(batch.Events))
		}
		count += len(batch.Events)
	}
	if count != 5 {
		t.Fatalf("expected 5 events, got %d", count)
	}
}

func TestDebouncedWatcher_QueuesAndDrainsWhenHubRecovers(t *testing.T) {
	dir := t.TempDir()
	received := &receivedBatches{}
	var hubUp atomic.Bool

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/batch" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		if auth := r.Header.Get("Authorization"); auth != "Bearer test-token" {
			t.Errorf("unexpected auth: %s", auth)
		}
		if !hubUp.Load() {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}

		var payload models.BatchPayload
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode error: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		received.append(payload)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	cfg := &config.WatcherConfig{
		MachineID:   "test-machine",
		WatchRoot:   dir,
		ProjectName: "offline-project",
	}
	cfg.Hub.URL = server.URL
	cfg.Hub.AuthToken = "test-token"
	cfg.Hub.BatchSec = 60
	cfg.Hub.BatchSize = 50

	filter, err := NewIgnoreFilter(dir, false, false, nil)
	if err != nil {
		t.Fatalf("ignore filter: %v", err)
	}
	client := sender.NewClient(cfg.Hub.URL, cfg.Hub.AuthToken, cfg.MachineID)
	client.RetryBackoff = func(int) time.Duration { return 0 }
	queue, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("queue: %v", err)
	}
	defer queue.Close()

	dw, err := NewDebouncedWatcher(cfg, filter, nil, client, queue)
	if err != nil {
		t.Fatalf("new watcher: %v", err)
	}

	events := []models.FileEvent{{
		Type:        models.EventCreate,
		Path:        filepath.Join(dir, "offline.go"),
		Project:     "offline-project",
		MachineID:   cfg.MachineID,
		Timestamp:   time.Now().Unix(),
		ContentHash: "offline-hash",
		Content:     "package main\nfunc offline() {}\n",
		Entities: []models.Entity{{
			Name:      "offline",
			Type:      "function",
			Line:      2,
			StartLine: 2,
			EndLine:   2,
		}},
	}}

	dw.sendOrQueue("offline-project", events)
	queued, err := queue.Len()
	if err != nil {
		t.Fatalf("queue len after failed send: %v", err)
	}
	if queued != 1 {
		t.Fatalf("expected 1 queued batch while hub is down, got %d", queued)
	}
	if got := len(received.snapshot()); got != 0 {
		t.Fatalf("expected no successful batches while hub is down, got %d", got)
	}

	hubUp.Store(true)
	if err := dw.DrainQueue(); err != nil {
		t.Fatalf("drain queue: %v", err)
	}
	queued, err = queue.Len()
	if err != nil {
		t.Fatalf("queue len after drain: %v", err)
	}
	if queued != 0 {
		t.Fatalf("expected queue to be acked after drain, got %d", queued)
	}

	batches := received.snapshot()
	if len(batches) != 1 {
		t.Fatalf("expected 1 drained batch, got %d", len(batches))
	}
	if batches[0].MachineID != cfg.MachineID || batches[0].Project != "offline-project" {
		t.Fatalf("unexpected drained batch scope: %#v", batches[0])
	}
	if len(batches[0].Events) != 1 || batches[0].Events[0].Path != events[0].Path {
		t.Fatalf("unexpected drained events: %#v", batches[0].Events)
	}
}

func TestDebouncedWatcher_DeleteClearsHashForRecreate(t *testing.T) {
	dir := t.TempDir()
	received, _, cleanup := startTestWatcher(t, dir, 200, 50, http.StatusOK)
	defer cleanup()

	path := filepath.Join(dir, "main.go")
	content := []byte("package main\nfunc main() {}\n")
	if err := os.WriteFile(path, content, 0644); err != nil {
		t.Fatalf("write: %v", err)
	}
	time.Sleep(500 * time.Millisecond)

	if err := os.Remove(path); err != nil {
		t.Fatalf("remove: %v", err)
	}
	time.Sleep(500 * time.Millisecond)

	if err := os.WriteFile(path, content, 0644); err != nil {
		t.Fatalf("recreate: %v", err)
	}
	time.Sleep(500 * time.Millisecond)

	mainEvents := collectEvents(received.snapshot(), "main.go")
	var upserts, deletes int
	for _, ev := range mainEvents {
		switch ev.Type {
		case models.EventCreate, models.EventModify:
			upserts++
		case models.EventDelete, models.EventRename:
			deletes++
		}
	}
	if upserts != 2 || deletes == 0 {
		t.Fatalf("expected two upserts and at least one delete/rename, got upserts=%d deletes=%d events=%#v", upserts, deletes, mainEvents)
	}
}

func TestExtractSymbols_IncludesLineRanges(t *testing.T) {
	entities, err := ExtractSymbols("main.go", []byte("package main\n\nfunc main() {\n\tprintln(\"hello\")\n}\n"))
	if err != nil {
		t.Fatalf("extract: %v", err)
	}
	if len(entities) == 0 {
		t.Fatal("expected entities")
	}
	for _, ent := range entities {
		if ent.Name == "main" {
			if ent.StartLine <= 0 || ent.EndLine < ent.StartLine {
				t.Fatalf("invalid range: %#v", ent)
			}
			return
		}
	}
	t.Fatalf("main entity not found: %#v", entities)
}

func TestExtractGraph_GoRelations(t *testing.T) {
	content := []byte("package main\n\nimport \"fmt\"\n\ntype Runner struct{}\n\nfunc (Runner) Run() { helper() }\n\nfunc helper() {}\n\nfunc main() {\n\tfmt.Println(\"hello\")\n\thelper()\n}\n")
	entities, relations, err := ExtractGraph("main.go", content)
	if err != nil {
		t.Fatalf("extract graph: %v", err)
	}
	if len(entities) < 2 {
		t.Fatalf("expected function entities, got %#v", entities)
	}

	want := map[string]bool{
		"CONTAINS::main":                 false,
		"CONTAINS::helper":               false,
		"CONTAINS::Runner.Run":           false,
		"IMPORTS::fmt":                   false,
		"CALLS_SYNTAX:main:fmt.Println":  false,
		"CALLS_SYNTAX:main:helper":       false,
		"CALLS_SYNTAX:Runner.Run:helper": false,
	}
	for _, rel := range relations {
		key := rel.Type + ":" + rel.Source + ":" + rel.Target
		if _, ok := want[key]; ok {
			want[key] = true
		}
	}
	for key, found := range want {
		if !found {
			t.Fatalf("missing relation %s in %#v", key, relations)
		}
	}
}

func TestIgnoreFilter_Hardcoded(t *testing.T) {
	dir := t.TempDir()
	os.MkdirAll(filepath.Join(dir, "node_modules", "foo"), 0755)
	os.WriteFile(filepath.Join(dir, "node_modules", "foo", "index.js"), []byte(""), 0644)
	os.WriteFile(filepath.Join(dir, "src", "main.go"), []byte("package main"), 0644)

	filter, _ := NewIgnoreFilter(dir, false, false, nil)
	if !filter.Match(filepath.Join(dir, "node_modules", "foo", "index.js")) {
		t.Error("node_modules should be ignored")
	}
	if filter.Match(filepath.Join(dir, "src", "main.go")) {
		t.Error("src/main.go should NOT be ignored")
	}
}

func TestProjectResolver_AutoDiscover(t *testing.T) {
	dir := t.TempDir()

	// Create nested projects
	os.MkdirAll(filepath.Join(dir, "OmniGraph", "hub"), 0755)
	os.WriteFile(filepath.Join(dir, "OmniGraph", "go.mod"), []byte("module omnigraph\n"), 0644)
	os.WriteFile(filepath.Join(dir, "OmniGraph", "hub", "main.go"), []byte("package main\n"), 0644)

	os.MkdirAll(filepath.Join(dir, "backend-api", "src"), 0755)
	os.WriteFile(filepath.Join(dir, "backend-api", "go.mod"), []byte("module backend\n"), 0644)
	os.WriteFile(filepath.Join(dir, "backend-api", "src", "server.go"), []byte("package main\n"), 0644)

	os.MkdirAll(filepath.Join(dir, "frontend-app", "node_modules", "foo"), 0755)
	os.WriteFile(filepath.Join(dir, "frontend-app", "package.json"), []byte(`{"name":"frontend"}`), 0644)

	pr := NewProjectResolver(dir, []string{"go.mod", "package.json"})
	if err := pr.Discover(); err != nil {
		t.Fatalf("discover: %v", err)
	}

	projects := pr.List()
	if len(projects) != 3 {
		t.Fatalf("expected 3 projects, got %d: %v", len(projects), projects)
	}

	// Resolve paths
	if got := pr.Resolve(filepath.Join(dir, "OmniGraph", "hub", "main.go")); got != "OmniGraph" {
		t.Errorf("resolve OmniGraph/hub/main.go = %s, want OmniGraph", got)
	}
	if got := pr.Resolve(filepath.Join(dir, "backend-api", "src", "server.go")); got != "backend-api" {
		t.Errorf("resolve backend-api/src/server.go = %s, want backend-api", got)
	}
	if got := pr.Resolve(filepath.Join(dir, "frontend-app", "node_modules", "foo", "index.js")); got != "frontend-app" {
		// node_modules should still resolve to frontend-app because project boundary is at package.json
		t.Errorf("resolve frontend-app/node_modules = %s, want frontend-app", got)
	}
}

func TestSemanticOutboxPersistsClaimsAndCompletesJob(t *testing.T) {
	path := filepath.Join(t.TempDir(), "queue.db")
	q, err := OpenQueue(path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	job := semanticworker.Job{MachineID: "m1", Project: "p1", Root: "/repo", Path: "/repo/main.go", ContentHash: "hash-1", Content: []byte("package main")}
	if err := q.UpsertSemanticJob(job, 3); err != nil {
		t.Fatalf("upsert: %v", err)
	}
	q.Close()

	q, err = OpenQueue(path)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer q.Close()
	jobs, err := q.ClaimDueSemanticJobs(10, time.Second)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(jobs) != 1 || jobs[0].ContentHash != "hash-1" || string(jobs[0].Content) != "package main" {
		t.Fatalf("unexpected jobs: %#v", jobs)
	}
	if current, err := q.IsSemanticJobCurrent(jobs[0].ID, "hash-1", jobs[0].LeaseVersion); err != nil || !current {
		t.Fatalf("current = %v err=%v", current, err)
	}
	if err := q.MarkSemanticJobDone(jobs[0].ID, "hash-1", jobs[0].LeaseVersion); err != nil {
		t.Fatalf("done: %v", err)
	}
	if done, err := q.SemanticJobCount(SemanticJobDone); err != nil || done != 1 {
		t.Fatalf("done count = %d err=%v", done, err)
	}
}

func TestSemanticOutboxConcurrentClaimIsExclusive(t *testing.T) {
	path := filepath.Join(t.TempDir(), "queue.db")
	q1, err := OpenQueue(path)
	if err != nil {
		t.Fatalf("open q1: %v", err)
	}
	defer q1.Close()
	q2, err := OpenQueue(path)
	if err != nil {
		t.Fatalf("open q2: %v", err)
	}
	defer q2.Close()
	job := semanticworker.Job{MachineID: "m1", Project: "p1", Root: "/repo", Path: "/repo/main.go", ContentHash: "hash-1", Content: []byte("package main")}
	if err := q1.UpsertSemanticJob(job, 3); err != nil {
		t.Fatalf("upsert: %v", err)
	}

	var wg sync.WaitGroup
	results := make(chan int, 2)
	for _, q := range []*LocalQueue{q1, q2} {
		wg.Add(1)
		go func(q *LocalQueue) {
			defer wg.Done()
			jobs, err := q.ClaimDueSemanticJobs(1, time.Second)
			if err != nil {
				t.Errorf("claim: %v", err)
				return
			}
			results <- len(jobs)
		}(q)
	}
	wg.Wait()
	close(results)
	total := 0
	for count := range results {
		total += count
	}
	if total != 1 {
		t.Fatalf("claimed jobs total = %d, want 1", total)
	}
}

func TestSemanticOutboxReclaimsExpiredRunningJob(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()
	job := semanticworker.Job{MachineID: "m1", Project: "p1", Root: "/repo", Path: "/repo/main.go", ContentHash: "hash-1", Content: []byte("package main")}
	if err := q.UpsertSemanticJob(job, 3); err != nil {
		t.Fatalf("upsert: %v", err)
	}
	jobs, err := q.ClaimDueSemanticJobs(1, time.Hour)
	if err != nil || len(jobs) != 1 {
		t.Fatalf("initial claim: jobs=%#v err=%v", jobs, err)
	}
	jobs, err = q.ClaimDueSemanticJobs(1, time.Hour)
	if err != nil || len(jobs) != 0 {
		t.Fatalf("unexpected unexpired reclaim: jobs=%#v err=%v", jobs, err)
	}
	if _, err := q.db.Exec("UPDATE semantic_jobs SET updated_at = ?", unixMilli()-2000); err != nil {
		t.Fatalf("age running job: %v", err)
	}
	jobs, err = q.ClaimDueSemanticJobs(1, time.Second)
	if err != nil || len(jobs) != 1 {
		t.Fatalf("expected expired reclaim: jobs=%#v err=%v", jobs, err)
	}
}

func TestSemanticOutboxRejectsStaleOwnerAfterReclaim(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()
	job := semanticworker.Job{MachineID: "m1", Project: "p1", Root: "/repo", Path: "/repo/main.go", ContentHash: "hash-1", Content: []byte("package main")}
	if err := q.UpsertSemanticJob(job, 3); err != nil {
		t.Fatalf("upsert: %v", err)
	}
	firstClaim, err := q.ClaimDueSemanticJobs(1, time.Second)
	if err != nil || len(firstClaim) != 1 {
		t.Fatalf("first claim: jobs=%#v err=%v", firstClaim, err)
	}
	if _, err := q.db.Exec("UPDATE semantic_jobs SET updated_at = ?", unixMilli()-2000); err != nil {
		t.Fatalf("age running job: %v", err)
	}
	secondClaim, err := q.ClaimDueSemanticJobs(1, time.Second)
	if err != nil || len(secondClaim) != 1 {
		t.Fatalf("second claim: jobs=%#v err=%v", secondClaim, err)
	}
	if firstClaim[0].LeaseVersion == secondClaim[0].LeaseVersion {
		t.Fatalf("lease version did not advance: first=%d second=%d", firstClaim[0].LeaseVersion, secondClaim[0].LeaseVersion)
	}
	if current, err := q.IsSemanticJobCurrent(firstClaim[0].ID, "hash-1", firstClaim[0].LeaseVersion); err != nil || current {
		t.Fatalf("stale owner current = %v err=%v", current, err)
	}
	if err := q.MarkSemanticJobDone(firstClaim[0].ID, "hash-1", firstClaim[0].LeaseVersion); err != ErrSemanticJobStale {
		t.Fatalf("stale done err = %v, want ErrSemanticJobStale", err)
	}
	if err := q.MarkSemanticJobFailed(firstClaim[0].ID, "hash-1", firstClaim[0].LeaseVersion, time.Millisecond, "stale"); err != ErrSemanticJobStale {
		t.Fatalf("stale fail err = %v, want ErrSemanticJobStale", err)
	}
	if err := q.MarkSemanticJobDone(secondClaim[0].ID, "hash-1", secondClaim[0].LeaseVersion); err != nil {
		t.Fatalf("current done: %v", err)
	}
}

func TestSemanticOutboxRetriesThenMarksDead(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()
	job := semanticworker.Job{MachineID: "m1", Project: "p1", Root: "/repo", Path: "/repo/main.go", ContentHash: "hash-1", Content: []byte("package main")}
	if err := q.UpsertSemanticJob(job, 2); err != nil {
		t.Fatalf("upsert: %v", err)
	}
	jobs, err := q.ClaimDueSemanticJobs(1, time.Second)
	if err != nil || len(jobs) != 1 {
		t.Fatalf("claim first: jobs=%#v err=%v", jobs, err)
	}
	if err := q.MarkSemanticJobFailed(jobs[0].ID, "hash-1", jobs[0].LeaseVersion, time.Millisecond, "temporary"); err != nil {
		t.Fatalf("fail first: %v", err)
	}
	if pending, err := q.SemanticJobCount(SemanticJobPending); err != nil || pending != 1 {
		t.Fatalf("pending count = %d err=%v", pending, err)
	}
	time.Sleep(2 * time.Millisecond)
	jobs, err = q.ClaimDueSemanticJobs(1, time.Second)
	if err != nil || len(jobs) != 1 {
		t.Fatalf("claim second: jobs=%#v err=%v", jobs, err)
	}
	if err := q.MarkSemanticJobFailed(jobs[0].ID, "hash-1", jobs[0].LeaseVersion, time.Millisecond, "terminal"); err != nil {
		t.Fatalf("fail second: %v", err)
	}
	if dead, err := q.SemanticJobCount(SemanticJobDead); err != nil || dead != 1 {
		t.Fatalf("dead count = %d err=%v", dead, err)
	}
}

func TestSemanticOutboxUpsertNewHashInvalidatesRunningJob(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()
	oldJob := semanticworker.Job{MachineID: "m1", Project: "p1", Root: "/repo", Path: "/repo/main.go", ContentHash: "old", Content: []byte("old")}
	newJob := semanticworker.Job{MachineID: "m1", Project: "p1", Root: "/repo", Path: "/repo/main.go", ContentHash: "new", Content: []byte("new")}
	if err := q.UpsertSemanticJob(oldJob, 3); err != nil {
		t.Fatalf("upsert old: %v", err)
	}
	jobs, err := q.ClaimDueSemanticJobs(1, time.Second)
	if err != nil || len(jobs) != 1 {
		t.Fatalf("claim old: jobs=%#v err=%v", jobs, err)
	}
	if err := q.UpsertSemanticJob(newJob, 3); err != nil {
		t.Fatalf("upsert new: %v", err)
	}
	if current, err := q.IsSemanticJobCurrent(jobs[0].ID, "old", jobs[0].LeaseVersion); err != nil || current {
		t.Fatalf("old current = %v err=%v", current, err)
	}
	jobs, err = q.ClaimDueSemanticJobs(1, time.Second)
	if err != nil || len(jobs) != 1 || jobs[0].ContentHash != "new" {
		t.Fatalf("claim new: jobs=%#v err=%v", jobs, err)
	}
}

func TestSemanticOutboxDeleteRemovesPendingJob(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()
	job := semanticworker.Job{MachineID: "m1", Project: "p1", Root: "/repo", Path: "/repo/main.go", ContentHash: "hash-1", Content: []byte("package main")}
	if err := q.UpsertSemanticJob(job, 3); err != nil {
		t.Fatalf("upsert: %v", err)
	}
	if err := q.DeleteSemanticJob("m1", "p1", "/repo/main.go"); err != nil {
		t.Fatalf("delete: %v", err)
	}
	jobs, err := q.ClaimDueSemanticJobs(10, time.Second)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(jobs) != 0 {
		t.Fatalf("expected no semantic jobs, got %#v", jobs)
	}
}

// QueueStatsServer GET /queue/stats returns state counts + dead error
// classes + pending events. Local-only endpoint consumed by Hub /stats.
func TestQueueStatsServerHandlerReturnsStateCounts(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	job := semanticworker.Job{
		MachineID: "m1", Project: "p", Root: "/r", Path: "/r/x.go",
		ContentHash: "h1", Content: []byte("package x"),
	}
	if err := q.UpsertSemanticJob(job, 3); err != nil {
		t.Fatalf("upsert: %v", err)
	}
	if err := q.Enqueue("m1", "p", []models.FileEvent{{Type: models.EventCreate, Path: "/r/y.go"}}); err != nil {
		t.Fatalf("enqueue: %v", err)
	}

	srv := NewQueueStatsServer("127.0.0.1:0", q)
	if srv == nil {
		t.Fatal("server nil")
	}

	req := httptest.NewRequest(http.MethodGet, "/queue/stats", nil)
	rec := httptest.NewRecorder()
	srv.handleStats(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	var resp QueueStatsResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.StateCounts[string(SemanticJobPending)] != 1 {
		t.Errorf("expected 1 pending semantic job, got %d", resp.StateCounts[string(SemanticJobPending)])
	}
	if resp.PendingEvents != 1 {
		t.Errorf("expected 1 pending event, got %d", resp.PendingEvents)
	}
}

func TestQueueStatsServerRejectsPost(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	srv := NewQueueStatsServer("127.0.0.1:0", q)
	req := httptest.NewRequest(http.MethodPost, "/queue/stats", nil)
	rec := httptest.NewRecorder()
	srv.handleStats(rec, req)

	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", rec.Code)
	}
}

func TestQueueStatsServerEmptyAddrDisablesServer(t *testing.T) {
	if NewQueueStatsServer("", nil) != nil {
		t.Fatal("expected nil server when addr empty")
	}
}

//Reconciler.Tick reports dead-row count and per-error-class
// breakdown. Force one job to dead state, verify Tick observes count > 0.
func TestReconcileReportsDeadRowCount(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	job := semanticworker.Job{
		MachineID: "m1", Project: "p", Root: "/r", Path: "/r/x.go",
		ContentHash: "h1", Content: []byte("package x"),
	}
	if err := q.UpsertSemanticJob(job, 1); err != nil {
		t.Fatalf("upsert: %v", err)
	}
	jobs, err := q.ClaimDueSemanticJobs(10, time.Minute)
	if err != nil || len(jobs) != 1 {
		t.Fatalf("claim: %v %d", err, len(jobs))
	}
	if err := q.MarkSemanticJobFailed(jobs[0].ID, jobs[0].ContentHash, jobs[0].LeaseVersion, time.Millisecond, "package broken"); err != nil {
		t.Fatalf("mark failed (force dead via max_retries=1): %v", err)
	}

	deadCount, err := q.SemanticJobCount(SemanticJobDead)
	if err != nil {
		t.Fatalf("count: %v", err)
	}
	if deadCount != 1 {
		t.Fatalf("expected 1 dead row, got %d", deadCount)
	}

	classes, err := q.SemanticDeadErrorClassCounts()
	if err != nil {
		t.Fatalf("classes: %v", err)
	}
	if len(classes) != 1 {
		t.Fatalf("expected 1 error class, got %d (%v)", len(classes), classes)
	}
	for _, n := range classes {
		if n != 1 {
			t.Errorf("expected class count 1, got %d", n)
		}
	}
}

//log dedup — when (deadCount, errorClasses) is unchanged
// across consecutive ticks, digest should match (forcing log suppression).
func TestReconcileDigestStableAcrossTicksWhenStateUnchanged(t *testing.T) {
	d1 := reconcileDigest(3, map[string]int{"abc": 2, "def": 1})
	d2 := reconcileDigest(3, map[string]int{"def": 1, "abc": 2})
	if d1 != d2 {
		t.Errorf("digest must be order-independent (sorted internally): %s vs %s", d1, d2)
	}
	d3 := reconcileDigest(4, map[string]int{"abc": 2, "def": 1})
	if d1 == d3 {
		t.Errorf("digest must change when count changes")
	}
}

//replay_count audit trail — admin replay increments
// replay_count without touching attempt_count. (Companion test to
// TestIncrementReplayCountAuditPreserved which targets the queue method
// directly.) This one targets a dead row specifically.
func TestAdminReplayResurrectsDeadRow(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	job := semanticworker.Job{
		MachineID: "m1", Project: "p", Root: "/r", Path: "/r/dead.go",
		ContentHash: "h1", Content: []byte("package x"),
	}
	if err := q.UpsertSemanticJob(job, 1); err != nil {
		t.Fatalf("upsert: %v", err)
	}
	jobs, err := q.ClaimDueSemanticJobs(10, time.Minute)
	if err != nil || len(jobs) != 1 {
		t.Fatalf("claim: %v %d", err, len(jobs))
	}
	if err := q.MarkSemanticJobFailed(jobs[0].ID, jobs[0].ContentHash, jobs[0].LeaseVersion, time.Millisecond, "fatal"); err != nil {
		t.Fatalf("force dead: %v", err)
	}

	dead, _ := q.SemanticJobCount(SemanticJobDead)
	if dead != 1 {
		t.Fatalf("setup: expected 1 dead row, got %d", dead)
	}

	if err := q.IncrementReplayCount(jobs[0].ID); err != nil {
		t.Fatalf("admin replay: %v", err)
	}

	// Dead count back to 0, pending = 1
	dead, _ = q.SemanticJobCount(SemanticJobDead)
	pending, _ := q.SemanticJobCount(SemanticJobPending)
	if dead != 0 || pending != 1 {
		t.Errorf("expected dead=0 pending=1 after replay, got dead=%d pending=%d", dead, pending)
	}

	var replay, attempt int
	if err := q.db.QueryRow("SELECT replay_count, attempt_count FROM semantic_jobs WHERE id = ?", jobs[0].ID).Scan(&replay, &attempt); err != nil {
		t.Fatalf("scan: %v", err)
	}
	if replay != 1 {
		t.Errorf("expected replay_count=1, got %d", replay)
	}
	if attempt != 1 {
		t.Errorf("attempt_count must stay 1 (audit), got %d", attempt)
	}
}

//addRecursive emits synthetic Create events for files
// already on disk at scan time (catch-up scan). Without this fix, files
// created BEFORE watcher startup OR inside a freshly-created subdir during
// the Add()→list-contents window would be missed.
func TestAddRecursiveEmitsCatchUpForExistingFiles(t *testing.T) {
	dir := t.TempDir()

	// Pre-populate 50 .go files BEFORE watcher starts.
	const n = 50
	for i := 0; i < n; i++ {
		path := filepath.Join(dir, fmt.Sprintf("file%d.go", i))
		if err := os.WriteFile(path, []byte(fmt.Sprintf("package x\nvar V%d = %d\n", i, i)), 0644); err != nil {
			t.Fatalf("seed write: %v", err)
		}
	}

	received, _, cleanup := startTestWatcher(t, dir, 50, 100, 200)
	defer cleanup()

	// addRecursive (in Start) emits synthetic Creates → pending → debounce →
	// flush → batch sent. Wait for catch-up to land.
	waitForTestCondition(t, func() bool {
		snap := received.snapshot()
		seen := make(map[string]struct{})
		for _, b := range snap {
			for _, ev := range b.Events {
				if filepath.Ext(ev.Path) == ".go" {
					seen[ev.Path] = struct{}{}
				}
			}
		}
		return len(seen) >= n
	})
}

//race-safe catch-up — files created concurrently during
// addRecursive are not lost. We pre-populate then continue creating during
// the watcher's startup window.
func TestAddRecursiveCatchUpUnderConcurrentCreation(t *testing.T) {
	dir := t.TempDir()

	const preSeed = 20
	const concurrent = 30

	for i := 0; i < preSeed; i++ {
		path := filepath.Join(dir, fmt.Sprintf("seed%d.go", i))
		if err := os.WriteFile(path, []byte(fmt.Sprintf("package s\nvar S%d = 0\n", i)), 0644); err != nil {
			t.Fatalf("seed write: %v", err)
		}
	}

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		// Race against startup: create more files while watcher boots.
		time.Sleep(2 * time.Millisecond)
		for i := 0; i < concurrent; i++ {
			path := filepath.Join(dir, fmt.Sprintf("conc%d.go", i))
			_ = os.WriteFile(path, []byte(fmt.Sprintf("package c\nvar C%d = 0\n", i)), 0644)
			time.Sleep(time.Millisecond)
		}
	}()

	received, _, cleanup := startTestWatcher(t, dir, 50, 100, 200)
	defer cleanup()

	wg.Wait()

	waitForTestCondition(t, func() bool {
		snap := received.snapshot()
		seen := make(map[string]struct{})
		for _, b := range snap {
			for _, ev := range b.Events {
				seen[ev.Path] = struct{}{}
			}
		}
		// Total .go file count = preSeed + concurrent. All must reach Hub.
		count := 0
		for path := range seen {
			if filepath.Ext(path) == ".go" {
				count++
			}
		}
		return count >= preSeed+concurrent
	})
}

//lastContentHash is FIFO-bounded; oldest entries evict
// after limit. 60k inserts on a 50k cap leaves exactly 50k, with first 10k
// gone and most-recent 50k retained.
func TestLastContentHashLRUEviction(t *testing.T) {
	b := newBoundedHashMap(50_000)
	for i := 0; i < 60_000; i++ {
		b.set(fmt.Sprintf("/p/%d.go", i), fmt.Sprintf("h%d", i))
	}
	if got := b.len(); got != 50_000 {
		t.Fatalf("expected 50000 after 60k inserts, got %d", got)
	}
	// Oldest 10k evicted (FIFO).
	for i := 0; i < 10_000; i++ {
		if got := b.get(fmt.Sprintf("/p/%d.go", i)); got != "" {
			t.Errorf("expected /p/%d.go evicted, still present with %s", i, got)
			break
		}
	}
	// Most-recent retained.
	for i := 50_000; i < 60_000; i++ {
		if got := b.get(fmt.Sprintf("/p/%d.go", i)); got != fmt.Sprintf("h%d", i) {
			t.Errorf("expected /p/%d.go retained, got %q", i, got)
			break
		}
	}
}

// delete must remove key from both data and order slice so it can be
// re-inserted after delete without phantom presence in eviction order.
func TestBoundedHashMapDeleteAllowsReinsert(t *testing.T) {
	b := newBoundedHashMap(3)
	b.set("a", "1")
	b.set("b", "2")
	b.delete("a")
	b.set("c", "3")
	b.set("d", "4")
	// At this point: b, c, d (3 entries). a was deleted, not bumping eviction.
	if got := b.len(); got != 3 {
		t.Fatalf("expected len 3, got %d", got)
	}
	if got := b.get("b"); got != "2" {
		t.Errorf("expected b retained after delete-then-insert, got %q", got)
	}
}

//stable Dequeue order even when created_at is shared
// (second-resolution time.Now().Unix() ties → must rely on id ASC for ordering).
func TestDequeueStableOrderUnderSharedCreatedAt(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	for i := 0; i < 5; i++ {
		ev := []models.FileEvent{{Type: models.EventCreate, Path: fmt.Sprintf("/f%d.go", i)}}
		if err := q.Enqueue("m1", "p1", ev); err != nil {
			t.Fatalf("enqueue: %v", err)
		}
	}

	batches, err := q.Dequeue(10)
	if err != nil {
		t.Fatalf("dequeue: %v", err)
	}
	if len(batches) != 5 {
		t.Fatalf("expected 5 batches, got %d", len(batches))
	}
	for i := 1; i < len(batches); i++ {
		if batches[i].ID <= batches[i-1].ID {
			t.Errorf("batches not ordered by id: %v at %d not > %v at %d", batches[i].ID, i, batches[i-1].ID, i-1)
		}
		expected := fmt.Sprintf("/f%d.go", i)
		if batches[i].Events[0].Path != expected {
			t.Errorf("batch %d: expected %s, got %s", i, expected, batches[i].Events[0].Path)
		}
	}
}

//replay_count column exists with default 0; IncrementReplayCount
// bumps replay_count without touching attempt_count (audit trail preserved).
func TestIncrementReplayCountAuditPreserved(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	job := semanticworker.Job{
		MachineID:   "m1",
		Project:     "p1",
		Root:        "/r",
		Path:        "/r/x.go",
		ContentHash: "h1",
		Content:     []byte("package x"),
	}
	if err := q.UpsertSemanticJob(job, 3); err != nil {
		t.Fatalf("upsert: %v", err)
	}

	var initialReplay, initialAttempt int
	row := q.db.QueryRow("SELECT replay_count, attempt_count FROM semantic_jobs WHERE path = ?", job.Path)
	if err := row.Scan(&initialReplay, &initialAttempt); err != nil {
		t.Fatalf("read replay_count default: %v", err)
	}
	if initialReplay != 0 {
		t.Errorf("expected replay_count default 0, got %d", initialReplay)
	}

	var jobID int
	if err := q.db.QueryRow("SELECT id FROM semantic_jobs WHERE path = ?", job.Path).Scan(&jobID); err != nil {
		t.Fatalf("get id: %v", err)
	}

	if err := q.IncrementReplayCount(jobID); err != nil {
		t.Fatalf("replay: %v", err)
	}

	var afterReplay, afterAttempt int
	var afterState string
	if err := q.db.QueryRow("SELECT replay_count, attempt_count, state FROM semantic_jobs WHERE id = ?", jobID).Scan(&afterReplay, &afterAttempt, &afterState); err != nil {
		t.Fatalf("read after: %v", err)
	}
	if afterReplay != 1 {
		t.Errorf("expected replay_count 1, got %d", afterReplay)
	}
	if afterAttempt != initialAttempt {
		t.Errorf("attempt_count changed from %d to %d (must stay untouched)", initialAttempt, afterAttempt)
	}
	if afterState != string(SemanticJobPending) {
		t.Errorf("expected state pending after replay, got %s", afterState)
	}
}

//schema migration is idempotent — opening existing DB twice
// must not error on duplicate replay_count column.
func TestOpenQueueIdempotentReplayCountMigration(t *testing.T) {
	path := filepath.Join(t.TempDir(), "queue.db")

	q1, err := OpenQueue(path)
	if err != nil {
		t.Fatalf("open #1: %v", err)
	}
	q1.Close()

	q2, err := OpenQueue(path)
	if err != nil {
		t.Fatalf("open #2: %v", err)
	}
	defer q2.Close()
}

func TestLocalQueue_EnqueueDequeue(t *testing.T) {
	q, err := OpenQueue(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer q.Close()

	events := []models.FileEvent{
		{Type: models.EventCreate, Path: "/a.go"},
		{Type: models.EventModify, Path: "/b.go"},
	}
	if err := q.Enqueue("m1", "p1", events); err != nil {
		t.Fatalf("enqueue: %v", err)
	}

	batches, err := q.Dequeue(10)
	if err != nil {
		t.Fatalf("dequeue: %v", err)
	}
	if len(batches) != 1 {
		t.Fatalf("expected 1 batch, got %d", len(batches))
	}
	if batches[0].ID == 0 {
		t.Error("expected queued batch id")
	}
	if len(batches[0].Events) != 2 {
		t.Errorf("expected 2 events, got %d", len(batches[0].Events))
	}
	if err := q.Ack([]int{batches[0].ID}); err != nil {
		t.Fatalf("ack: %v", err)
	}
	remaining, err := q.Len()
	if err != nil {
		t.Fatalf("len: %v", err)
	}
	if remaining != 0 {
		t.Fatalf("expected empty queue after ack, got %d", remaining)
	}
}
