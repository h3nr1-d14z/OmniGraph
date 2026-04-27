package logger

import (
	"io"
	"log/slog"
	"os"
)

// dynamicStderr is an io.Writer that delegates to os.Stderr at call time.
// This ensures log output tracks any runtime replacement of os.Stderr
// (e.g., in tests that redirect the file descriptor via os.Pipe).
type dynamicStderr struct{}

func (dynamicStderr) Write(p []byte) (int, error) {
	return os.Stderr.Write(p)
}

// New returns a slog.Logger that writes to os.Stderr. When jsonMode is true
// the output is newline-delimited JSON; otherwise it uses the human-readable
// text format. The writer delegates to os.Stderr at write time so that tests
// can redirect os.Stderr and still capture log output.
func New(jsonMode bool) *slog.Logger {
	w := io.Writer(dynamicStderr{})
	opts := &slog.HandlerOptions{Level: slog.LevelInfo}
	if jsonMode {
		return slog.New(slog.NewJSONHandler(w, opts))
	}
	return slog.New(slog.NewTextHandler(w, opts))
}
