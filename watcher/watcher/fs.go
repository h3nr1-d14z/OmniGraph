package watcher

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/fsnotify/fsnotify"
	"github.com/omnigraph/watcher/config"
	"github.com/omnigraph/watcher/models"
	"github.com/omnigraph/watcher/sender"
)

const maxFileSize = 500 * 1024

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

	watcher         *fsnotify.Watcher
	pending         map[string]pendingEvent
	lastContentHash map[string]string
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
		lastContentHash: make(map[string]string),
		ctx:             ctx,
		cancel:          cancel,
		batchTicker:     time.NewTicker(time.Duration(cfg.Hub.BatchSec) * time.Second),
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
		fmt.Fprintf(os.Stderr, "[watch] discovered %d projects: %v\n", len(projects), projects)
	}

	go dw.processLoop()
	go dw.sendLoop()

	return nil
}

// Stop shuts down the watcher gracefully.
func (dw *DebouncedWatcher) Stop() error {
	dw.cancel()
	dw.batchTicker.Stop()
	return dw.watcher.Close()
}

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
			fmt.Fprintf(os.Stderr, "fsnotify error: %v\n", err)
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
			dw.sendOrQueue(project, chunk)
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
	unchanged := dw.lastContentHash[pending.Path] == hash
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

func (dw *DebouncedWatcher) sendOrQueue(project string, events []models.FileEvent) {
	if err := dw.client.SendBatch(events, project); err != nil {
		fmt.Fprintf(os.Stderr, "send failed for project %s, queuing locally: %v\n", project, err)
		if dw.queue == nil {
			return
		}
		if qerr := dw.queue.Enqueue(dw.cfg.MachineID, project, events); qerr != nil {
			fmt.Fprintf(os.Stderr, "queue error: %v\n", qerr)
			return
		}
	}
	dw.markDurable(events)
}

func (dw *DebouncedWatcher) markDurable(events []models.FileEvent) {
	dw.mu.Lock()
	defer dw.mu.Unlock()

	for _, ev := range events {
		switch ev.Type {
		case models.EventDelete, models.EventRename:
			delete(dw.lastContentHash, ev.Path)
		default:
			if ev.ContentHash != "" {
				dw.lastContentHash[ev.Path] = ev.ContentHash
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
			acked = append(acked, b.ID)
		}
		if err := dw.queue.Ack(acked); err != nil {
			return err
		}
	}
}
