package sender

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/omnigraph/watcher/models"
)

// Client sends batches to the Hub.
type Client struct {
	BaseURL      string
	AuthToken    string
	MachineID    string
	HTTP         *http.Client
	RetryBackoff func(attempt int) time.Duration
}

func defaultRetryBackoff(attempt int) time.Duration {
	return time.Duration(attempt) * 2 * time.Second
}

// NewClient creates a sender with retry logic.
func NewClient(baseURL, token, machineID string) *Client {
	return &Client{
		BaseURL:   baseURL,
		AuthToken: token,
		MachineID: machineID,
		HTTP: &http.Client{
			Timeout: 30 * time.Second,
		},
		RetryBackoff: defaultRetryBackoff,
	}
}

// SendBatch POSTs events to the Hub with exponential backoff.
func (c *Client) SendBatch(events []models.FileEvent, project string) error {
	return c.SendBatchContext(context.Background(), events, project)
}

func (c *Client) SendBatchContext(ctx context.Context, events []models.FileEvent, project string) error {
	payload := models.BatchPayload{
		MachineID: c.MachineID,
		Project:   project,
		Events:    events,
		SentAt:    time.Now(),
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}

	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		if attempt > 0 && c.RetryBackoff != nil {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(c.RetryBackoff(attempt)):
			}
		}

		req, err := http.NewRequestWithContext(ctx, "POST", c.BaseURL+"/batch", bytes.NewReader(body))
		if err != nil {
			return err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+c.AuthToken)
		req.Header.Set("X-Machine-ID", c.MachineID)

		resp, err := c.HTTP.Do(req)
		if err != nil {
			lastErr = err
			continue
		}
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
		resp.Body.Close()

		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			return nil
		}
		lastErr = fmt.Errorf("hub returned %d", resp.StatusCode)
	}

	return lastErr
}
