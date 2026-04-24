package watcher

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/omnigraph/watcher/config"
	"github.com/omnigraph/watcher/models"
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
