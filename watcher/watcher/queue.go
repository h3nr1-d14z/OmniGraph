package watcher

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/omnigraph/watcher/models"
	semanticworker "github.com/omnigraph/watcher/semantic/worker"
	_ "modernc.org/sqlite"
)

// LocalQueue persists events to SQLite when the Hub is unreachable.
type LocalQueue struct {
	db *sql.DB
}

type QueuedBatch struct {
	ID int
	models.BatchPayload
}

type SemanticJobState string

const (
	SemanticJobPending SemanticJobState = "pending"
	SemanticJobRunning SemanticJobState = "running"
	SemanticJobDone    SemanticJobState = "done"
	SemanticJobDead    SemanticJobState = "dead"
)

type SemanticQueuedJob struct {
	ID            int
	LeaseVersion  int
	MachineID     string
	Project       string
	Root          string
	Path          string
	ContentHash   string
	Content       []byte
	AttemptCount  int
	MaxRetries    int
	NextAttemptAt int64
	State         SemanticJobState
	LastError     string
}

var ErrSemanticJobStale = errors.New("semantic job is stale")

// OpenQueue initializes the local SQLite queue.
func OpenQueue(path string) (*LocalQueue, error) {
	if path == "" {
		home, _ := os.UserHomeDir()
		path = filepath.Join(home, ".config", "omnigraph", "watcher-queue.db")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return nil, err
	}

	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	if _, err := db.Exec("PRAGMA busy_timeout = 5000; PRAGMA journal_mode = WAL;"); err != nil {
		db.Close()
		return nil, err
	}

	schema := `
		CREATE TABLE IF NOT EXISTS events (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			machine_id TEXT NOT NULL,
			project TEXT NOT NULL,
			payload BLOB NOT NULL,
			created_at INTEGER NOT NULL
		);
		CREATE INDEX IF NOT EXISTS idx_created ON events(created_at);

		CREATE TABLE IF NOT EXISTS semantic_jobs (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			machine_id TEXT NOT NULL,
			project TEXT NOT NULL,
			root TEXT NOT NULL,
			path TEXT NOT NULL,
			content_hash TEXT NOT NULL,
			content BLOB NOT NULL,
			state TEXT NOT NULL,
			attempt_count INTEGER NOT NULL DEFAULT 0,
			lease_version INTEGER NOT NULL DEFAULT 0,
			max_retries INTEGER NOT NULL,
			next_attempt_at INTEGER NOT NULL,
			last_error TEXT NOT NULL DEFAULT '',
			created_at INTEGER NOT NULL,
			updated_at INTEGER NOT NULL,
			done_at INTEGER,
			dead_at INTEGER,
			UNIQUE(machine_id, project, path)
		);
		CREATE INDEX IF NOT EXISTS idx_semantic_jobs_due ON semantic_jobs(state, next_attempt_at, updated_at);
		`
	if _, err := db.Exec(schema); err != nil {
		return nil, err
	}
	if _, err := db.Exec("ALTER TABLE semantic_jobs ADD COLUMN lease_version INTEGER NOT NULL DEFAULT 0"); err != nil && !isDuplicateColumnError(err) {
		return nil, err
	}
	if _, err := db.Exec("ALTER TABLE semantic_jobs ADD COLUMN replay_count INTEGER NOT NULL DEFAULT 0"); err != nil && !isDuplicateColumnError(err) {
		return nil, err
	}

	return &LocalQueue{db: db}, nil
}

// Enqueue stores events for later delivery.
func (q *LocalQueue) Enqueue(machineID, project string, events []models.FileEvent) error {
	payload, err := json.Marshal(events)
	if err != nil {
		return err
	}
	_, err = q.db.Exec(
		`INSERT INTO events (machine_id, project, payload, created_at)
		 VALUES (:machine_id, :project, :payload, :created_at)`,
		sql.Named("machine_id", machineID),
		sql.Named("project", project),
		sql.Named("payload", payload),
		sql.Named("created_at", time.Now().Unix()),
	)
	return err
}

// Dequeue retrieves up to limit pending batches.
func (q *LocalQueue) Dequeue(limit int) ([]QueuedBatch, error) {
	rows, err := q.db.Query(
		"SELECT id, machine_id, project, payload FROM events ORDER BY id ASC LIMIT ?",
		limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var batches []QueuedBatch
	for rows.Next() {
		var id int
		var machineID, project string
		var payload []byte
		if err := rows.Scan(&id, &machineID, &project, &payload); err != nil {
			continue
		}
		var events []models.FileEvent
		if err := json.Unmarshal(payload, &events); err != nil {
			continue
		}
		batches = append(batches, QueuedBatch{
			ID: id,
			BatchPayload: models.BatchPayload{
				MachineID: machineID,
				Project:   project,
				Events:    events,
				SentAt:    time.Now(),
			},
		})
	}
	return batches, rows.Err()
}

// Ack removes successfully sent batches.
func (q *LocalQueue) Ack(ids []int) error {
	if len(ids) == 0 {
		return nil
	}
	tx, err := q.db.Begin()
	if err != nil {
		return err
	}
	stmt, err := tx.Prepare("DELETE FROM events WHERE id = ?")
	if err != nil {
		tx.Rollback()
		return err
	}
	defer stmt.Close()
	for _, id := range ids {
		if _, err := stmt.Exec(id); err != nil {
			tx.Rollback()
			return err
		}
	}
	return tx.Commit()
}

func isDuplicateColumnError(err error) bool {
	return strings.Contains(err.Error(), "duplicate column name")
}

func (q *LocalQueue) UpsertSemanticJob(job semanticworker.Job, maxRetries int) error {
	if job.Path == "" || job.ContentHash == "" || len(job.Content) == 0 {
		return nil
	}
	if maxRetries <= 0 {
		maxRetries = 3
	}
	now := unixMilli()
	_, err := q.db.Exec(`
		INSERT INTO semantic_jobs (
			machine_id, project, root, path, content_hash, content, state,
			attempt_count, max_retries, next_attempt_at, last_error, created_at, updated_at
		) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, '', ?, ?)
		ON CONFLICT(machine_id, project, path) DO UPDATE SET
			root = excluded.root,
			content_hash = excluded.content_hash,
			content = excluded.content,
			state = ?,
			attempt_count = 0,
			max_retries = excluded.max_retries,
			next_attempt_at = excluded.next_attempt_at,
			last_error = '',
			updated_at = excluded.updated_at,
			done_at = NULL,
			dead_at = NULL
		WHERE semantic_jobs.content_hash != excluded.content_hash`,
		job.MachineID, job.Project, job.Root, job.Path, job.ContentHash, job.Content,
		string(SemanticJobPending), maxRetries, now, now, now,
		string(SemanticJobPending),
	)
	return err
}

func (q *LocalQueue) ClaimDueSemanticJobs(limit int, leaseDuration time.Duration) ([]SemanticQueuedJob, error) {
	if limit <= 0 {
		return nil, nil
	}
	if leaseDuration <= 0 {
		leaseDuration = time.Minute
	}
	now := unixMilli()
	leaseCutoff := time.Now().Add(-leaseDuration).UnixMilli()
	rows, err := q.db.Query(`
		UPDATE semantic_jobs
		SET state = ?, updated_at = ?, lease_version = lease_version + 1
		WHERE id IN (
			SELECT id FROM semantic_jobs
			WHERE (state = ? AND next_attempt_at <= ?)
			   OR (state = ? AND updated_at <= ?)
			ORDER BY next_attempt_at ASC, updated_at ASC
			LIMIT ?
		)
		RETURNING id, lease_version, machine_id, project, root, path, content_hash, content,
		          attempt_count, max_retries, next_attempt_at, state, last_error`,
		string(SemanticJobRunning), now,
		string(SemanticJobPending), now,
		string(SemanticJobRunning), leaseCutoff,
		limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var jobs []SemanticQueuedJob
	for rows.Next() {
		var job SemanticQueuedJob
		var state string
		if err := rows.Scan(
			&job.ID,
			&job.LeaseVersion,
			&job.MachineID,
			&job.Project,
			&job.Root,
			&job.Path,
			&job.ContentHash,
			&job.Content,
			&job.AttemptCount,
			&job.MaxRetries,
			&job.NextAttemptAt,
			&state,
			&job.LastError,
		); err != nil {
			return nil, err
		}
		job.State = SemanticJobState(state)
		jobs = append(jobs, job)
	}
	return jobs, rows.Err()
}

func (q *LocalQueue) IsSemanticJobCurrent(id int, contentHash string, leaseVersion int) (bool, error) {
	var count int
	if err := q.db.QueryRow(
		"SELECT COUNT(*) FROM semantic_jobs WHERE id = ? AND content_hash = ? AND lease_version = ? AND state = ?",
		id, contentHash, leaseVersion, string(SemanticJobRunning),
	).Scan(&count); err != nil {
		return false, err
	}
	return count > 0, nil
}

func (q *LocalQueue) MarkSemanticJobDone(id int, contentHash string, leaseVersion int) error {
	now := unixMilli()
	result, err := q.db.Exec(
		"UPDATE semantic_jobs SET state = ?, updated_at = ?, done_at = ?, last_error = '' WHERE id = ? AND content_hash = ? AND lease_version = ? AND state = ?",
		string(SemanticJobDone), now, now, id, contentHash, leaseVersion, string(SemanticJobRunning),
	)
	if err != nil {
		return err
	}
	return semanticRowsChanged(result)
}

func (q *LocalQueue) MarkSemanticJobFailed(id int, contentHash string, leaseVersion int, baseDelay time.Duration, lastError string) error {
	if baseDelay <= 0 {
		baseDelay = 500 * time.Millisecond
	}
	tx, err := q.db.Begin()
	if err != nil {
		return err
	}
	var attemptCount, maxRetries int
	if err := tx.QueryRow(
		"SELECT attempt_count, max_retries FROM semantic_jobs WHERE id = ? AND content_hash = ? AND lease_version = ?",
		id, contentHash, leaseVersion,
	).Scan(&attemptCount, &maxRetries); err != nil {
		tx.Rollback()
		if errors.Is(err, sql.ErrNoRows) {
			return ErrSemanticJobStale
		}
		return err
	}
	attemptCount++
	now := unixMilli()
	state := SemanticJobPending
	var deadAt any
	nextAttemptAt := now + semanticBackoff(baseDelay, attemptCount).Milliseconds()
	if attemptCount >= maxRetries {
		state = SemanticJobDead
		deadAt = now
		nextAttemptAt = now
	}
	result, err := tx.Exec(
		"UPDATE semantic_jobs SET state = ?, attempt_count = ?, next_attempt_at = ?, last_error = ?, updated_at = ?, dead_at = ? WHERE id = ? AND content_hash = ? AND lease_version = ? AND state = ?",
		string(state), attemptCount, nextAttemptAt, lastError, now, deadAt, id, contentHash, leaseVersion, string(SemanticJobRunning),
	)
	if err != nil {
		tx.Rollback()
		return err
	}
	if err := semanticRowsChanged(result); err != nil {
		tx.Rollback()
		return err
	}
	return tx.Commit()
}

func (q *LocalQueue) ReleaseSemanticJob(id int, contentHash string, leaseVersion int, delay time.Duration) error {
	if delay < 0 {
		delay = 0
	}
	now := unixMilli()
	nextAttemptAt := now + delay.Milliseconds()
	result, err := q.db.Exec(
		"UPDATE semantic_jobs SET state = ?, next_attempt_at = ?, updated_at = ? WHERE id = ? AND content_hash = ? AND lease_version = ? AND state = ?",
		string(SemanticJobPending), nextAttemptAt, now, id, contentHash, leaseVersion, string(SemanticJobRunning),
	)
	if err != nil {
		return err
	}
	return semanticRowsChanged(result)
}

func (q *LocalQueue) DeleteSemanticJob(machineID, project, path string) error {
	_, err := q.db.Exec(
		"DELETE FROM semantic_jobs WHERE machine_id = ? AND project = ? AND path = ?",
		machineID, project, path,
	)
	return err
}

// IncrementReplayCount bumps replay_count for a dead-letter job manually re-injected
// via admin path. attempt_count is intentionally untouched (audit trail of original retries).
func (q *LocalQueue) IncrementReplayCount(id int) error {
	now := unixMilli()
	_, err := q.db.Exec(
		"UPDATE semantic_jobs SET replay_count = replay_count + 1, state = ?, next_attempt_at = ?, updated_at = ?, dead_at = NULL WHERE id = ?",
		string(SemanticJobPending), now, now, id,
	)
	return err
}

func (q *LocalQueue) SemanticJobCount(state SemanticJobState) (int, error) {
	var count int
	row := q.db.QueryRow("SELECT COUNT(*) FROM semantic_jobs WHERE state = ?", string(state))
	err := row.Scan(&count)
	return count, err
}

// SemanticDeadErrorClassCounts groups dead jobs by a content-hash of their
// last_error so reconcile can log error-class diversity without dumping
// every full message. Returns a short hex prefix → count map.
func (q *LocalQueue) SemanticDeadErrorClassCounts() (map[string]int, error) {
	rows, err := q.db.Query(
		"SELECT last_error FROM semantic_jobs WHERE state = ?",
		string(SemanticJobDead),
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := make(map[string]int)
	for rows.Next() {
		var msg string
		if err := rows.Scan(&msg); err != nil {
			return nil, err
		}
		// Short hash prefix as error-class identifier; keeps log compact.
		sum := sha256.Sum256([]byte(msg))
		key := hex.EncodeToString(sum[:4])
		if msg == "" {
			key = "empty"
		}
		out[key]++
	}
	return out, rows.Err()
}

func (job SemanticQueuedJob) WorkerJob() semanticworker.Job {
	return semanticworker.Job{
		OutboxID:     job.ID,
		LeaseVersion: job.LeaseVersion,
		MachineID:    job.MachineID,
		Project:      job.Project,
		Root:         job.Root,
		Path:         job.Path,
		ContentHash:  job.ContentHash,
		Content:      job.Content,
	}
}

func unixMilli() int64 {
	return time.Now().UnixMilli()
}

func semanticRowsChanged(result sql.Result) error {
	rows, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if rows == 0 {
		return ErrSemanticJobStale
	}
	return nil
}

func semanticBackoff(base time.Duration, attempt int) time.Duration {
	if attempt <= 1 {
		return base
	}
	delay := base
	for i := 1; i < attempt; i++ {
		delay *= 2
		if delay > time.Hour {
			return time.Hour
		}
	}
	return delay
}

// Len returns the number of pending events.
func (q *LocalQueue) Len() (int, error) {
	var count int
	row := q.db.QueryRow("SELECT COUNT(*) FROM events")
	err := row.Scan(&count)
	return count, err
}

// Close shuts down the database.
func (q *LocalQueue) Close() error {
	return q.db.Close()
}
