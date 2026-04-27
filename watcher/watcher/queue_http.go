package watcher

import (
	"context"
	"encoding/json"
	"net/http"
	"time"
)

// QueueStatsResponse is the shape returned by GET /queue/stats. Hub `/stats`
// fetches this and surfaces dead_semantic_jobs alongside its own counters.
type QueueStatsResponse struct {
	StateCounts          map[string]int `json:"state_counts"`
	DeadErrorClassCounts map[string]int `json:"dead_error_class_counts"`
	PendingEvents        int            `json:"pending_events"`
}

// QueueStatsServer exposes a tiny read-only HTTP endpoint over the local
// outbox. Bound to 127.0.0.1 only — no auth needed since the surface is
// loopback. Hub fetches it via configurable URL with graceful degrade.
type QueueStatsServer struct {
	server *http.Server
	queue  *LocalQueue
}

func NewQueueStatsServer(addr string, q *LocalQueue) *QueueStatsServer {
	if addr == "" {
		return nil
	}
	s := &QueueStatsServer{queue: q}
	mux := http.NewServeMux()
	mux.HandleFunc("/queue/stats", s.handleStats)
	s.server = &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 2 * time.Second,
	}
	return s
}

func (s *QueueStatsServer) Addr() string {
	if s == nil || s.server == nil {
		return ""
	}
	return s.server.Addr
}

func (s *QueueStatsServer) Start() error {
	if s == nil {
		return nil
	}
	go func() {
		_ = s.server.ListenAndServe()
	}()
	return nil
}

func (s *QueueStatsServer) Stop(ctx context.Context) error {
	if s == nil {
		return nil
	}
	return s.server.Shutdown(ctx)
}

func (s *QueueStatsServer) handleStats(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}

	resp, err := s.collect()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}

func (s *QueueStatsServer) collect() (QueueStatsResponse, error) {
	states := []SemanticJobState{
		SemanticJobPending,
		SemanticJobRunning,
		SemanticJobDone,
		SemanticJobDead,
	}
	counts := make(map[string]int, len(states))
	for _, st := range states {
		n, err := s.queue.SemanticJobCount(st)
		if err != nil {
			return QueueStatsResponse{}, err
		}
		counts[string(st)] = n
	}
	deadClasses, err := s.queue.SemanticDeadErrorClassCounts()
	if err != nil {
		return QueueStatsResponse{}, err
	}
	events, err := s.queue.Len()
	if err != nil {
		return QueueStatsResponse{}, err
	}
	return QueueStatsResponse{
		StateCounts:          counts,
		DeadErrorClassCounts: deadClasses,
		PendingEvents:        events,
	}, nil
}
