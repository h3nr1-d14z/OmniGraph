package watcher

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/fsnotify/fsnotify"
	"github.com/omnigraph/watcher/config"
	"github.com/omnigraph/watcher/models"
	semanticworker "github.com/omnigraph/watcher/semantic/worker"
	"github.com/omnigraph/watcher/sender"
)

const maxFileSize = 500 * 1024

// enqueueTimeout caps how long fsnotify will wait for SQLite when the
// queue is busy; missing the window logs a warning and drops the batch
// (acceptable since fs events get re-emitted on the next debounce tick).
const enqueueTimeout = 2 * time.Second

// lastContentHash cap prevents unbounded growth on long-running watchers.
// Files moved out of watch tree (rm -rf parent, FS unmount) leave orphan
// entries; FIFO eviction matches the semanticRelationCache precedent.
const lastContentHashLimit = 50000

// boundedHashMap is a FIFO-bounded path->hash map. Caller must serialize access.
type boundedHashMap struct {
	limit int
	order []string
	data  map[string]string
}

func newBoundedHashMap(limit int) *boundedHashMap {
	return &boundedHashMap{limit: limit, data: make(map[string]string)}
}

func (b *boundedHashMap) get(key string) string {
	if b == nil {
		return ""
	}
	return b.data[key]
}

func (b *boundedHashMap) set(key, value string) {
	if b == nil {
		return
	}
	if _, exists := b.data[key]; !exists {
		b.order = append(b.order, key)
	}
	b.data[key] = value
	for len(b.order) > b.limit {
		oldest := b.order[0]
		b.order = b.order[1:]
		delete(b.data, oldest)
	}
}

func (b *boundedHashMap) delete(key string) {
	if b == nil {
		return
	}
	if _, exists := b.data[key]; !exists {
		return
	}
	delete(b.data, key)
	for i, k := range b.order {
		if k == key {
			b.order = append(b.order[:i], b.order[i+1:]...)
			return
		}
	}
}

func (b *boundedHashMap) len() int {
	if b == nil {
		return 0
	}
	return len(b.data)
}

type pendingEvent struct {
	Type      models.EventType
	Path      string
	OldPath   string
	Project   string
	Timestamp int64
}

// DebouncedWatcher monitors a directory tree and batches events.
type DebouncedWatcher struct {
	cfg      *config.WatcherConfig
	filter   *IgnoreFilter
	resolver *ProjectResolver
	client   *sender.Client
	queue    *LocalQueue

	semantic        *semanticworker.Worker
	semanticNotify  chan struct{}
	semanticWg      sync.WaitGroup
	watcher         *fsnotify.Watcher
	pending         map[string]pendingEvent
	lastContentHash *boundedHashMap
	mu              sync.Mutex
	timer           *time.Timer
	batchTicker     *time.Ticker
	ctx             context.Context
	cancel          context.CancelFunc
}

// NewDebouncedWatcher creates a watcher for the given config.
func NewDebouncedWatcher(cfg *config.WatcherConfig, filter *IgnoreFilter, resolver *ProjectResolver, client *sender.Client, queue *LocalQueue) (*DebouncedWatcher, error) {
	w, err := fsnotify.NewWatcher()
	if err != nil {
		return nil, err
	}

	ctx, cancel := context.WithCancel(context.Background())
	dw := &DebouncedWatcher{
		cfg:             cfg,
		filter:          filter,
		resolver:        resolver,
		client:          client,
		queue:           queue,
		watcher:         w,
		pending:         make(map[string]pendingEvent),
		lastContentHash: newBoundedHashMap(lastContentHashLimit),
		ctx:             ctx,
		cancel:          cancel,
		batchTicker:     time.NewTicker(time.Duration(cfg.Hub.BatchSec) * time.Second),
	}
	if cfg.Semantic.Enabled {
		dw.semanticNotify = make(chan struct{}, 1)
		retryDelay := time.Duration(cfg.Semantic.RetryDelayMs) * time.Millisecond
		dw.semantic = semanticworker.New(semanticConfig(cfg), newGoSemanticResolver(cfg.Semantic.CacheSize, queue, retryDelay), semanticBatchSender{client: client, queue: queue, retryDelay: retryDelay})
	}
	return dw, nil
}

// Start begins recursive watching and event processing.
func (dw *DebouncedWatcher) Start() error {
	root := dw.cfg.WatchRoot
	if err := dw.addRecursive(root); err != nil {
		return err
	}

	if dw.cfg.AutoDetect && dw.resolver != nil {
		if err := dw.resolver.Discover(); err != nil {
			return err
		}
		projects := dw.resolver.List()
		slog.Info("projects_discovered", "count", len(projects), "projects", projects)
	}

	if dw.semantic != nil {
		dw.semantic.Start(dw.ctx)
		dw.semanticWg.Add(1)
		go dw.semanticLoop()
		dw.signalSemantic()
	}
	go dw.processLoop()
	go dw.sendLoop()

	return nil
}

// Stop shuts down the watcher gracefully.
func (dw *DebouncedWatcher) Stop() error {
	dw.cancel()
	if dw.semantic != nil {
		dw.semantic.Stop()
		dw.semanticWg.Wait()
	}
	dw.batchTicker.Stop()
	return dw.watcher.Close()
}

// addRecursive installs fsnotify watchers on root and every subdirectory,
// AND emits synthetic Create events for files already present at scan time.
// This closes the race where files created inside a fresh subdirectory
// between mkdir and our Add() would otherwise be missed.
//
// Order is critical: filepath.Walk visits a directory's callback BEFORE
// listing its contents, so we install the watcher on a dir before any
// children are processed. Files created during the microsecond window
// between Add() and content listing are still captured because the
// watcher is already installed → kernel Create events route through
// processLoop to the pending map. The pending map's dedupe (via
// mergeEventType) collapses any synthetic+kernel double-fire on the
// same path within the debounce window into a single event.
func (dw *DebouncedWatcher) addRecursive(root string) error {
	return filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return nil
		}
		if info.IsDir() {
			if dw.filter.Match(path) {
				return filepath.SkipDir
			}
			return dw.watcher.Add(path)
		}
		if dw.filter.Match(path) {
			return nil
		}
		dw.handleEvent(fsnotify.Event{Name: path, Op: fsnotify.Create})
		return nil
	})
}

func (dw *DebouncedWatcher) processLoop() {
	for {
		select {
		case <-dw.ctx.Done():
			return
		case event, ok := <-dw.watcher.Events:
			if !ok {
				return
			}
			dw.handleEvent(event)
		case err, ok := <-dw.watcher.Errors:
			if !ok {
				return
			}
			slog.Error("fsnotify_error", "err", err)
		}
	}
}

func (dw *DebouncedWatcher) handleEvent(evt fsnotify.Event) {
	if dw.filter.Match(evt.Name) {
		return
	}

	var etype models.EventType
	switch {
	case evt.Op&fsnotify.Create == fsnotify.Create:
		etype = models.EventCreate
		if info, err := os.Stat(evt.Name); err == nil && info.IsDir() {
			dw.addRecursive(evt.Name)
		}
	case evt.Op&fsnotify.Write == fsnotify.Write:
		etype = models.EventModify
	case evt.Op&fsnotify.Remove == fsnotify.Remove:
		etype = models.EventDelete
	case evt.Op&fsnotify.Rename == fsnotify.Rename:
		etype = models.EventDelete
	default:
		return
	}

	project := dw.cfg.ProjectName
	if project == "" && dw.resolver != nil {
		project = dw.resolver.Resolve(evt.Name)
	}

	dw.mu.Lock()
	defer dw.mu.Unlock()

	pending := pendingEvent{
		Type:      etype,
		Path:      evt.Name,
		Project:   project,
		Timestamp: time.Now().Unix(),
	}
	if prev, ok := dw.pending[evt.Name]; ok {
		pending.Type = mergeEventType(prev.Type, etype)
		if project == "" {
			pending.Project = prev.Project
		}
	}
	dw.pending[evt.Name] = pending

	if dw.timer != nil {
		dw.timer.Stop()
	}
	dw.timer = time.AfterFunc(time.Duration(dw.cfg.Hub.DebounceMs)*time.Millisecond, func() {
		dw.flush()
	})
}

func mergeEventType(prev, next models.EventType) models.EventType {
	switch next {
	case models.EventDelete, models.EventRename:
		return next
	case models.EventCreate:
		return models.EventCreate
	case models.EventModify:
		if prev == models.EventCreate {
			return models.EventCreate
		}
		if prev == models.EventDelete || prev == models.EventRename {
			return models.EventCreate
		}
		return models.EventModify
	default:
		return next
	}
}

func (dw *DebouncedWatcher) sendLoop() {
	for {
		select {
		case <-dw.ctx.Done():
			return
		case <-dw.batchTicker.C:
			dw.flush()
		}
	}
}

func (dw *DebouncedWatcher) flush() {
	pending := dw.takePending()
	if len(pending) == 0 {
		return
	}

	batch := dw.finalizeEvents(pending)
	if len(batch) == 0 {
		return
	}

	byProject := make(map[string][]models.FileEvent)
	for _, ev := range batch {
		byProject[ev.Project] = append(byProject[ev.Project], ev)
	}

	for project, projBatch := range byProject {
		for _, chunk := range chunkEvents(projBatch, dw.cfg.Hub.BatchSize) {
			if dw.sendOrQueue(project, chunk) {
				dw.enqueueSemantic(chunk)
			}
		}
	}
}

func (dw *DebouncedWatcher) takePending() []pendingEvent {
	dw.mu.Lock()
	defer dw.mu.Unlock()

	if len(dw.pending) == 0 {
		return nil
	}

	pending := make([]pendingEvent, 0, len(dw.pending))
	for _, ev := range dw.pending {
		pending = append(pending, ev)
	}
	dw.pending = make(map[string]pendingEvent)
	return pending
}

func (dw *DebouncedWatcher) finalizeEvents(pending []pendingEvent) []models.FileEvent {
	batch := make([]models.FileEvent, 0, len(pending))
	for _, ev := range pending {
		fileEvent, ok := dw.finalizeEvent(ev)
		if ok {
			batch = append(batch, fileEvent)
		}
	}
	return batch
}

func (dw *DebouncedWatcher) finalizeEvent(pending pendingEvent) (models.FileEvent, bool) {
	ev := models.FileEvent{
		Type:      pending.Type,
		Path:      pending.Path,
		OldPath:   pending.OldPath,
		Project:   pending.Project,
		MachineID: dw.cfg.MachineID,
		Timestamp: pending.Timestamp,
	}

	if pending.Type == models.EventDelete || pending.Type == models.EventRename {
		return ev, true
	}

	info, err := os.Stat(pending.Path)
	if err != nil || info.IsDir() || info.Size() >= maxFileSize {
		return models.FileEvent{}, false
	}

	content, err := os.ReadFile(pending.Path)
	if err != nil {
		return models.FileEvent{}, false
	}

	hash := contentHash(content)
	dw.mu.Lock()
	unchanged := dw.lastContentHash.get(pending.Path) == hash
	dw.mu.Unlock()
	if unchanged {
		return models.FileEvent{}, false
	}

	entities, relations, _ := ExtractGraph(pending.Path, content)
	ev.ContentHash = hash
	ev.Content = string(content)
	ev.Entities = entities
	ev.Relations = relations
	return ev, true
}

func contentHash(content []byte) string {
	sum := sha256.Sum256(content)
	return fmt.Sprintf("%x", sum)
}

func chunkEvents(events []models.FileEvent, size int) [][]models.FileEvent {
	if len(events) == 0 {
		return nil
	}
	if size <= 0 || size >= len(events) {
		return [][]models.FileEvent{events}
	}

	chunks := make([][]models.FileEvent, 0, (len(events)+size-1)/size)
	for start := 0; start < len(events); start += size {
		end := start + size
		if end > len(events) {
			end = len(events)
		}
		chunks = append(chunks, events[start:end])
	}
	return chunks
}

func (dw *DebouncedWatcher) sendOrQueue(project string, events []models.FileEvent) bool {
	if err := dw.client.SendBatch(events, project); err != nil {
		slog.Warn("send_failed_queuing_locally", "project", project, "err", err)
		if dw.queue == nil {
			return false
		}
		enqueueCtx, cancel := context.WithTimeout(context.Background(), enqueueTimeout)
		qerr := dw.queue.Enqueue(enqueueCtx, dw.cfg.MachineID, project, events)
		cancel()
		if qerr != nil {
			// Distinguish "SQLite slow / busy" (ctx deadline) from "schema
			// or disk error" so operators can tell disk pressure from a
			// real corruption — both used to log identically.
			if errors.Is(qerr, context.DeadlineExceeded) {
				slog.Warn("queue_enqueue_timeout", "timeout", enqueueTimeout, "err", qerr)
			} else {
				slog.Error("queue_error", "err", qerr)
			}
			return false
		}
		dw.markDurable(events)
		return false
	}
	dw.markDurable(events)
	return true
}

func (dw *DebouncedWatcher) markDurable(events []models.FileEvent) {
	dw.mu.Lock()
	defer dw.mu.Unlock()

	for _, ev := range events {
		switch ev.Type {
		case models.EventDelete, models.EventRename:
			dw.lastContentHash.delete(ev.Path)
		default:
			if ev.ContentHash != "" {
				dw.lastContentHash.set(ev.Path, ev.ContentHash)
			}
		}
	}
}

// DrainQueue attempts to send any locally queued events.
func (dw *DebouncedWatcher) DrainQueue() error {
	if dw.queue == nil {
		return nil
	}

	for {
		batches, err := dw.queue.Dequeue(10)
		if err != nil || len(batches) == 0 {
			return err
		}
		acked := make([]int, 0, len(batches))
		for _, b := range batches {
			if err := dw.client.SendBatch(b.Events, b.Project); err != nil {
				if len(acked) > 0 {
					if ackErr := dw.queue.Ack(acked); ackErr != nil {
						return ackErr
					}
				}
				return err
			}
			dw.markDurable(b.Events)
			dw.enqueueSemantic(b.Events)
			acked = append(acked, b.ID)
		}
		if err := dw.queue.Ack(acked); err != nil {
			return err
		}
	}
}
