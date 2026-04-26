package watcher

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/omnigraph/watcher/config"
	"github.com/omnigraph/watcher/models"
	"github.com/omnigraph/watcher/semantic/goresolver"
	semanticworker "github.com/omnigraph/watcher/semantic/worker"
	"github.com/omnigraph/watcher/sender"
)

func semanticConfig(cfg *config.WatcherConfig) semanticworker.Config {
	return semanticworker.Config{
		QueueCapacity: cfg.Semantic.QueueCapacity,
		WorkerCount:   cfg.Semantic.WorkerCount,
		JobTimeout:    time.Duration(cfg.Semantic.TimeoutMs) * time.Millisecond,
		RetryDelay:    time.Duration(cfg.Semantic.RetryDelayMs) * time.Millisecond,
		MaxRetries:    cfg.Semantic.MaxRetries,
	}
}

type goSemanticResolver struct {
	cache      *semanticRelationCache
	queue      *LocalQueue
	retryDelay time.Duration
}

func newGoSemanticResolver(cacheSize int, queue *LocalQueue, retryDelay time.Duration) goSemanticResolver {
	if cacheSize < 0 {
		cacheSize = config.DefaultSemanticCacheSize
	}
	return goSemanticResolver{cache: newSemanticRelationCache(cacheSize), queue: queue, retryDelay: retryDelay}
}

func (r goSemanticResolver) Resolve(ctx context.Context, job semanticworker.Job) ([]models.Relation, error) {
	key := semanticCacheKey(job)
	if relations, ok := r.cache.get(key); ok {
		return relations, nil
	}
	relations, err := (goresolver.Resolver{}).Resolve(ctx, goresolver.Request{
		Root:     job.Root,
		FilePath: job.Path,
		Content:  job.Content,
	})
	if err != nil {
		r.markFailed(job, err)
		if job.OutboxID != 0 && r.queue != nil {
			return nil, nil
		}
		return nil, err
	}
	r.cache.put(key, relations)
	if len(relations) == 0 {
		r.markDone(job)
	}
	return relations, nil
}

func (r goSemanticResolver) markDone(job semanticworker.Job) {
	markSemanticDone(r.queue, job)
}

func (r goSemanticResolver) markFailed(job semanticworker.Job, err error) {
	markSemanticFailed(r.queue, job, r.retryDelay, err.Error())
}

type semanticRelationCache struct {
	mu        sync.Mutex
	limit     int
	order     []string
	relations map[string][]models.Relation
}

func newSemanticRelationCache(limit int) *semanticRelationCache {
	if limit < 0 {
		limit = 0
	}
	return &semanticRelationCache{limit: limit, relations: make(map[string][]models.Relation)}
}

func (c *semanticRelationCache) get(key string) ([]models.Relation, bool) {
	if c == nil || c.limit == 0 {
		return nil, false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	relations, ok := c.relations[key]
	if !ok {
		return nil, false
	}
	return cloneRelations(relations), true
}

func (c *semanticRelationCache) put(key string, relations []models.Relation) {
	if c == nil || c.limit == 0 {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, exists := c.relations[key]; !exists {
		c.order = append(c.order, key)
	}
	c.relations[key] = cloneRelations(relations)
	for len(c.order) > c.limit {
		oldest := c.order[0]
		c.order = c.order[1:]
		delete(c.relations, oldest)
	}
}

func semanticCacheKey(job semanticworker.Job) string {
	return job.Root + "\x00" + job.Path + "\x00" + job.ContentHash
}

func cloneRelations(relations []models.Relation) []models.Relation {
	out := make([]models.Relation, len(relations))
	copy(out, relations)
	return out
}

type semanticBatchSender struct {
	client     *sender.Client
	queue      *LocalQueue
	retryDelay time.Duration
}

func markSemanticDone(queue *LocalQueue, job semanticworker.Job) {
	if queue != nil && job.OutboxID != 0 {
		_ = queue.MarkSemanticJobDone(job.OutboxID, job.ContentHash, job.LeaseVersion)
	}
}

func markSemanticFailed(queue *LocalQueue, job semanticworker.Job, retryDelay time.Duration, message string) {
	if queue != nil && job.OutboxID != 0 {
		_ = queue.MarkSemanticJobFailed(job.OutboxID, job.ContentHash, job.LeaseVersion, retryDelay, message)
	}
}

func (s semanticBatchSender) Send(ctx context.Context, job semanticworker.Job, relations []models.Relation) error {
	if s.queue != nil && job.OutboxID != 0 {
		current, err := s.queue.IsSemanticJobCurrent(job.OutboxID, job.ContentHash, job.LeaseVersion)
		if err != nil {
			markSemanticFailed(s.queue, job, s.retryDelay, err.Error())
			return err
		}
		if !current {
			return nil
		}
	}
	event := models.FileEvent{
		Type:        models.EventModify,
		Path:        job.Path,
		Project:     job.Project,
		MachineID:   job.MachineID,
		Timestamp:   time.Now().Unix(),
		ContentHash: job.ContentHash,
		Relations:   relations,
	}
	if err := s.client.SendBatchContext(ctx, []models.FileEvent{event}, job.Project); err != nil {
		markSemanticFailed(s.queue, job, s.retryDelay, err.Error())
		if job.OutboxID != 0 && s.queue != nil {
			return nil
		}
		return err
	}
	markSemanticDone(s.queue, job)
	return nil
}

func (dw *DebouncedWatcher) semanticLoop() {
	defer dw.semanticWg.Done()
	ticker := time.NewTicker(time.Duration(dw.cfg.Semantic.RetryDelayMs) * time.Millisecond)
	defer ticker.Stop()
	for {
		dw.drainSemanticJobs()
		select {
		case <-dw.ctx.Done():
			return
		case <-dw.semanticNotify:
		case <-ticker.C:
		}
	}
}

func (dw *DebouncedWatcher) drainSemanticJobs() {
	if dw.queue == nil || dw.semantic == nil {
		return
	}
	jobs, err := dw.queue.ClaimDueSemanticJobs(dw.cfg.Semantic.QueueCapacity, semanticLeaseDuration(dw.cfg))
	if err != nil {
		fmt.Fprintf(os.Stderr, "semantic job claim failed: %v\n", err)
		return
	}
	for _, job := range jobs {
		workerJob := job.WorkerJob()
		if !dw.isLatestSemanticJob(workerJob) {
			if err := dw.queue.ReleaseSemanticJob(job.ID, job.ContentHash, job.LeaseVersion, time.Duration(dw.cfg.Semantic.RetryDelayMs)*time.Millisecond); err != nil && err != ErrSemanticJobStale {
				fmt.Fprintf(os.Stderr, "semantic job release failed for %s: %v\n", job.Path, err)
			}
			continue
		}
		if !dw.semantic.Enqueue(workerJob) {
			if err := dw.queue.ReleaseSemanticJob(job.ID, job.ContentHash, job.LeaseVersion, time.Duration(dw.cfg.Semantic.RetryDelayMs)*time.Millisecond); err != nil && err != ErrSemanticJobStale {
				fmt.Fprintf(os.Stderr, "semantic job release failed for %s: %v\n", job.Path, err)
			}
			fmt.Fprintf(os.Stderr, "semantic enqueue skipped for %s: queue full\n", job.Path)
			return
		}
	}
}

func semanticLeaseDuration(cfg *config.WatcherConfig) time.Duration {
	lease := time.Duration(cfg.Semantic.TimeoutMs+cfg.Semantic.RetryDelayMs) * time.Millisecond
	if lease < time.Second {
		return time.Second
	}
	return lease
}

func (dw *DebouncedWatcher) signalSemantic() {
	if dw.semanticNotify == nil {
		return
	}
	select {
	case dw.semanticNotify <- struct{}{}:
	default:
	}
}

func (dw *DebouncedWatcher) isLatestSemanticJob(job semanticworker.Job) bool {
	dw.mu.Lock()
	defer dw.mu.Unlock()
	latest := dw.lastContentHash[job.Path]
	return latest == "" || latest == job.ContentHash
}

func (dw *DebouncedWatcher) enqueueSemantic(events []models.FileEvent) {
	if dw.semantic == nil || dw.queue == nil {
		return
	}
	for _, ev := range events {
		if ev.Type == models.EventDelete || ev.Type == models.EventRename {
			if err := dw.queue.DeleteSemanticJob(dw.cfg.MachineID, ev.Project, ev.Path); err != nil {
				fmt.Fprintf(os.Stderr, "semantic job delete failed for %s: %v\n", ev.Path, err)
			}
			continue
		}
		if ev.ContentHash == "" || ev.Content == "" || filepath.Ext(ev.Path) != ".go" {
			continue
		}
		root := dw.cfg.WatchRoot
		if dw.resolver != nil {
			root = dw.resolver.ResolveRoot(ev.Path)
		}
		if err := dw.queue.UpsertSemanticJob(semanticworker.Job{
			MachineID:   dw.cfg.MachineID,
			Project:     ev.Project,
			Root:        root,
			Path:        ev.Path,
			ContentHash: ev.ContentHash,
			Content:     []byte(ev.Content),
		}, dw.cfg.Semantic.MaxRetries); err != nil {
			fmt.Fprintf(os.Stderr, "semantic job enqueue failed for %s: %v\n", ev.Path, err)
			continue
		}
		dw.signalSemantic()
	}
}
