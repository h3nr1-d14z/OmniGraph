package watcher

import (
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"time"

	"github.com/omnigraph/watcher/models"
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

	schema := `
	CREATE TABLE IF NOT EXISTS events (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		machine_id TEXT NOT NULL,
		project TEXT NOT NULL,
		payload BLOB NOT NULL,
		created_at INTEGER NOT NULL
	);
	CREATE INDEX IF NOT EXISTS idx_created ON events(created_at);
	`
	if _, err := db.Exec(schema); err != nil {
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
		"INSERT INTO events (machine_id, project, payload, created_at) VALUES (?, ?, ?, ?)",
		machineID, project, payload, time.Now().Unix(),
	)
	return err
}

// Dequeue retrieves up to limit pending batches.
func (q *LocalQueue) Dequeue(limit int) ([]QueuedBatch, error) {
	rows, err := q.db.Query(
		"SELECT id, machine_id, project, payload FROM events ORDER BY created_at ASC LIMIT ?",
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
