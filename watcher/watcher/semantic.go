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
	cache *semanticRelationCache
}

func newGoSemanticResolver(cacheSize int) goSemanticResolver {
	if cacheSize < 0 {
		cacheSize = config.DefaultSemanticCacheSize
	}
	return goSemanticResolver{cache: newSemanticRelationCache(cacheSize)}
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
		return nil, err
	}
	r.cache.put(key, relations)
	return relations, nil
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
	client *sender.Client
}

func (s semanticBatchSender) Send(ctx context.Context, job semanticworker.Job, relations []models.Relation) error {
	event := models.FileEvent{
		Type:        models.EventModify,
		Path:        job.Path,
		Project:     job.Project,
		MachineID:   job.MachineID,
		Timestamp:   time.Now().Unix(),
		ContentHash: job.ContentHash,
		Relations:   relations,
	}
	return s.client.SendBatchContext(ctx, []models.FileEvent{event}, job.Project)
}

func (dw *DebouncedWatcher) enqueueSemantic(events []models.FileEvent) {
	if dw.semantic == nil {
		return
	}
	for _, ev := range events {
		if ev.ContentHash == "" || ev.Content == "" || filepath.Ext(ev.Path) != ".go" {
			continue
		}
		root := dw.cfg.WatchRoot
		if dw.resolver != nil {
			root = dw.resolver.ResolveRoot(ev.Path)
		}
		if !dw.semantic.Enqueue(semanticworker.Job{
			MachineID:   dw.cfg.MachineID,
			Project:     ev.Project,
			Root:        root,
			Path:        ev.Path,
			ContentHash: ev.ContentHash,
			Content:     []byte(ev.Content),
		}) {
			fmt.Fprintf(os.Stderr, "semantic enqueue skipped for %s: queue full\n", ev.Path)
		}
	}
}
