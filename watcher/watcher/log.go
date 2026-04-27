package watcher

import (
	"log/slog"

	"github.com/omnigraph/watcher/logger"
)

func init() {
	// Install a dynamic-stderr slog default so that any package-level slog.*
	// calls (Info, Warn, Error) write to os.Stderr at call time rather than
	// at handler-construction time. This ensures tests that redirect os.Stderr
	// via os.Pipe capture log output correctly.
	slog.SetDefault(logger.New(false))
}
