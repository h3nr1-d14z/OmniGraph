package worker

import (
	"context"
	"sync"
	"time"

	"github.com/omnigraph/watcher/models"
)

type Job struct {
	MachineID   string
	Project     string
	Root        string
	Path        string
	ContentHash string
	Content     []byte
}

type Resolver interface {
	Resolve(context.Context, Job) ([]models.Relation, error)
}

type Sender interface {
	Send(context.Context, Job, []models.Relation) error
}

type Config struct {
	QueueCapacity int
	WorkerCount   int
	JobTimeout    time.Duration
	RetryDelay    time.Duration
	MaxRetries    int
}

type Worker struct {
	cfg      Config
	resolver Resolver
	sender   Sender

	mu      sync.Mutex
	pending map[string]Job
	latest  map[string]string
	retries map[string]int
	notify  chan struct{}
	stopped chan struct{}
	cancel  context.CancelFunc
	started bool
	wg      sync.WaitGroup
}

func New(cfg Config, resolver Resolver, sender Sender) *Worker {
	if cfg.QueueCapacity <= 0 {
		cfg.QueueCapacity = 100
	}
	if cfg.WorkerCount <= 0 {
		cfg.WorkerCount = 1
	}
	if cfg.JobTimeout <= 0 {
		cfg.JobTimeout = 3 * time.Second
	}
	if cfg.RetryDelay <= 0 {
		cfg.RetryDelay = 500 * time.Millisecond
	}
	if cfg.MaxRetries <= 0 {
		cfg.MaxRetries = 3
	}
	return &Worker{
		cfg:      cfg,
		resolver: resolver,
		sender:   sender,
		pending:  make(map[string]Job),
		latest:   make(map[string]string),
		retries:  make(map[string]int),
		notify:   make(chan struct{}, 1),
		stopped:  make(chan struct{}),
	}
}

func (w *Worker) Start(parent context.Context) {
	w.mu.Lock()
	if w.started {
		w.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(parent)
	w.cancel = cancel
	w.started = true
	w.mu.Unlock()

	for i := 0; i < w.cfg.WorkerCount; i++ {
		w.wg.Add(1)
		go w.run(ctx)
	}
}

func (w *Worker) Stop() {
	w.mu.Lock()
	cancel := w.cancel
	w.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	w.wg.Wait()
	w.mu.Lock()
	w.started = false
	w.cancel = nil
	w.mu.Unlock()
	select {
	case <-w.stopped:
	default:
		close(w.stopped)
	}
}

func (w *Worker) Enqueue(job Job) bool {
	if job.Path == "" || job.ContentHash == "" {
		return false
	}
	key := jobKey(job)
	w.mu.Lock()
	defer w.mu.Unlock()
	if _, exists := w.pending[key]; !exists && len(w.pending) >= w.cfg.QueueCapacity {
		return false
	}
	w.pending[key] = job
	w.latest[key] = job.ContentHash
	w.retries[key] = 0
	w.signal()
	return true
}

func (w *Worker) PendingLen() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return len(w.pending)
}

func (w *Worker) run(ctx context.Context) {
	defer w.wg.Done()
	for {
		job, ok := w.nextJob(ctx)
		if !ok {
			return
		}
		w.process(ctx, job)
	}
}

func (w *Worker) nextJob(ctx context.Context) (Job, bool) {
	for {
		w.mu.Lock()
		for key, job := range w.pending {
			delete(w.pending, key)
			w.mu.Unlock()
			return job, true
		}
		w.mu.Unlock()

		select {
		case <-ctx.Done():
			return Job{}, false
		case <-w.notify:
		}
	}
}

func (w *Worker) process(parent context.Context, job Job) {
	ctx, cancel := context.WithTimeout(parent, w.cfg.JobTimeout)
	defer cancel()
	relations, err := w.resolver.Resolve(ctx, job)
	if err != nil || len(relations) == 0 {
		w.finish(job)
		return
	}
	if !w.isLatest(job) {
		return
	}
	if err := w.sender.Send(ctx, job, relations); err != nil {
		w.requeue(job)
		return
	}
	w.finish(job)
}

func (w *Worker) requeue(job Job) {
	key := jobKey(job)
	w.mu.Lock()
	if w.latest[key] != job.ContentHash {
		w.mu.Unlock()
		return
	}
	w.retries[key]++
	if w.retries[key] > w.cfg.MaxRetries {
		delete(w.latest, key)
		delete(w.retries, key)
		w.mu.Unlock()
		return
	}
	w.pending[key] = job
	w.mu.Unlock()
	time.AfterFunc(w.cfg.RetryDelay, w.signal)
}

func (w *Worker) finish(job Job) {
	key := jobKey(job)
	w.mu.Lock()
	if w.latest[key] == job.ContentHash {
		delete(w.latest, key)
		delete(w.retries, key)
	}
	w.mu.Unlock()
}

func (w *Worker) isLatest(job Job) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.latest[jobKey(job)] == job.ContentHash
}

func (w *Worker) signal() {
	select {
	case w.notify <- struct{}{}:
	default:
	}
}

func jobKey(job Job) string {
	return job.MachineID + "\x00" + job.Project + "\x00" + job.Path
}
