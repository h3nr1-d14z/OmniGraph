package models

import "time"

// EventType represents the kind of file system change.
type EventType string

const (
	EventCreate EventType = "CREATE"
	EventModify EventType = "MODIFY"
	EventDelete EventType = "DELETE"
	EventRename EventType = "RENAME"
)

// FileEvent represents a single file change.
type FileEvent struct {
	Type        EventType `json:"type"`
	Path        string    `json:"path"`
	OldPath     string    `json:"old_path,omitempty"`
	Project     string    `json:"project"`
	MachineID   string    `json:"machine_id"`
	Timestamp   int64     `json:"timestamp"`
	ContentHash string    `json:"content_hash,omitempty"`
	Content     string    `json:"content,omitempty"`  // optional: file content for small files
	Entities    []Entity  `json:"entities,omitempty"` // AST extraction results
}

// Entity represents an extracted code symbol.
type Entity struct {
	Name      string `json:"name"`
	Type      string `json:"type"` // function, class, struct, interface, import
	Line      int    `json:"line"`
	StartLine int    `json:"start_line,omitempty"`
	EndLine   int    `json:"end_line,omitempty"`
}

// BatchPayload is the HTTP request body sent to Hub.
type BatchPayload struct {
	MachineID string      `json:"machine_id"`
	Project   string      `json:"project"`
	Events    []FileEvent `json:"events"`
	SentAt    time.Time   `json:"sent_at"`
}

// TombstoneEvent is a special delete event.
type TombstoneEvent struct {
	MachineID string `json:"machine_id"`
	Project   string `json:"project"`
	Path      string `json:"path"`
}
