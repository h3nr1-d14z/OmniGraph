package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/omnigraph/watcher/config"
	"github.com/omnigraph/watcher/logger"
	"github.com/omnigraph/watcher/semantic/goresolver"
	"github.com/omnigraph/watcher/sender"
	"github.com/omnigraph/watcher/watcher"
)

func isTerminal() bool {
	fi, err := os.Stderr.Stat()
	if err != nil {
		return false
	}
	return fi.Mode()&os.ModeCharDevice != 0
}

func main() {
	jsonMode := !isTerminal()
	slog.SetDefault(logger.New(jsonMode))

	if len(os.Args) < 2 {
		usage()
		os.Exit(1)
	}

	switch os.Args[1] {
	case "init":
		runInit()
	case "watch":
		runWatch()
	case "semantic":
		runSemantic()
	case "replay":
		runReplay()
	default:
		usage()
		os.Exit(1)
	}
}

func usage() {
	fmt.Println("Usage: watcher <command> [options]")
	fmt.Println("")
	fmt.Println("Commands:")
	fmt.Println("  init      Create default config at ~/.config/omnigraph/watcher.yaml")
	fmt.Println("  watch     Start watching a project directory")
	fmt.Println("  semantic  Resolve Go semantic relations for one file and print JSON")
	fmt.Println("  replay    Re-inject a dead-letter semantic job by id (admin)")
	fmt.Println("")
	fmt.Println("Examples:")
	fmt.Println("  watcher init")
	fmt.Println("  watcher watch -config ~/.config/omnigraph/watcher.yaml")
	fmt.Println("  watcher semantic -root ./my-module -file ./my-module/main.go")
	fmt.Println("  watcher replay -id 42")
}

func runReplay() {
	fs := flag.NewFlagSet("replay", flag.ExitOnError)
	id := fs.Int("id", 0, "semantic_jobs row id to re-inject")
	queuePath := fs.String("queue", "", "Path to watcher-queue.db (defaults to ~/.config/omnigraph/watcher-queue.db)")
	fs.Parse(os.Args[2:])

	if *id <= 0 {
		fmt.Fprintln(os.Stderr, "replay requires -id <int>")
		os.Exit(1)
	}

	q, err := watcher.OpenQueue(*queuePath)
	if err != nil {
		slog.Error("queue_open_failed", "err", err)
		os.Exit(1)
	}
	defer q.Close()

	if err := q.IncrementReplayCount(*id); err != nil {
		slog.Error("replay_failed", "err", err)
		os.Exit(1)
	}
	fmt.Printf("replayed semantic_jobs id=%d (state=pending, replay_count incremented; attempt_count untouched)\n", *id)
}

func runInit() {
	cfg := &config.WatcherConfig{}
	cfg.MachineID = "dev-machine-01"
	cfg.WatchRoot = "~/Projects/your-repo"
	cfg.AutoDetect = true
	cfg.Markers = config.DefaultProjectMarkers
	cfg.Hub.URL = "http://localhost:9000"
	cfg.Hub.AuthToken = "changeme"
	cfg.Hub.BatchSize = 50
	cfg.Hub.BatchSec = 5
	cfg.Hub.DebounceMs = 3000
	cfg.Hub.MaxEventsPerSec = 100
	cfg.Semantic.Enabled = false
	cfg.Semantic.WorkerCount = 1
	cfg.Semantic.QueueCapacity = 100
	cfg.Semantic.TimeoutMs = 3000
	cfg.Semantic.RetryDelayMs = 500
	cfg.Semantic.MaxRetries = 3
	cfg.Semantic.CacheSize = config.DefaultSemanticCacheSize
	cfg.Semantic.ReconcileIntervalSec = config.DefaultReconcileIntervalSec
	cfg.Ignore.GitIgnore = true
	cfg.Ignore.DockerIgnore = true

	path := config.DefaultConfigPath()
	if err := cfg.Save(path); err != nil {
		slog.Error("config_write_failed", "err", err)
		os.Exit(1)
	}
	fmt.Printf("Config written to %s\n", path)
	fmt.Println("Edit watch_root and hub URL before running 'watch'.")
}

func runSemantic() {
	fs := flag.NewFlagSet("semantic", flag.ExitOnError)
	root := fs.String("root", ".", "Go module/project root")
	filePath := fs.String("file", "", "Go file to resolve")
	fs.Parse(os.Args[2:])

	if *filePath == "" {
		fmt.Fprintln(os.Stderr, "semantic requires -file")
		os.Exit(1)
	}

	relations, err := (goresolver.Resolver{}).ResolveStrict(context.Background(), goresolver.Request{
		Root:     *root,
		FilePath: *filePath,
	})
	if err != nil {
		slog.Error("semantic resolve error", "err", err)
		os.Exit(1)
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(relations); err != nil {
		slog.Error("semantic_encode_error", "err", err)
		os.Exit(1)
	}
}

func normalizeWatchRoot(root string) (string, error) {
	if root == "~" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		root = home
	} else if len(root) > 2 && root[:2] == "~/" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		root = filepath.Join(home, root[2:])
	}
	return filepath.Abs(root)
}

func runWatch() {
	fs := flag.NewFlagSet("watch", flag.ExitOnError)
	configPath := fs.String("config", "", "Path to watcher config file")
	fs.Parse(os.Args[2:])

	cfg, err := config.Load(*configPath)
	if err != nil {
		slog.Error("config_load_failed", "err", err)
		os.Exit(1)
	}

	root, err := normalizeWatchRoot(cfg.WatchRoot)
	if err != nil {
		slog.Error("watch_root_error", "err", err)
		os.Exit(1)
	}
	cfg.WatchRoot = root

	filter, err := watcher.NewIgnoreFilter(
		root,
		cfg.Ignore.GitIgnore,
		cfg.Ignore.DockerIgnore,
		cfg.Ignore.Extra,
	)
	if err != nil {
		slog.Error("ignore_filter_error", "err", err)
		os.Exit(1)
	}

	var resolver *watcher.ProjectResolver
	if cfg.AutoDetect {
		resolver = watcher.NewProjectResolver(root, cfg.Markers)
	}

	client := sender.NewClient(cfg.Hub.URL, cfg.Hub.AuthToken, cfg.MachineID)
	queue, err := watcher.OpenQueue("")
	if err != nil {
		slog.Error("queue_open_failed", "err", err)
		os.Exit(1)
	}
	defer queue.Close()

	// Local-only HTTP endpoint exposing outbox stats for Hub /stats consumption.
	statsAddr := cfg.QueueStatsAddr
	if statsAddr == "" {
		statsAddr = "127.0.0.1:9100"
	}
	statsServer := watcher.NewQueueStatsServer(statsAddr, queue)
	if statsServer != nil {
		_ = statsServer.Start()
		slog.Info("queue_stats_listening", "addr", statsAddr)
		defer func() {
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			_ = statsServer.Stop(ctx)
		}()
	}

	dw, err := watcher.NewDebouncedWatcher(cfg, filter, resolver, client, queue)
	if err != nil {
		slog.Error("watcher_init_error", "err", err)
		os.Exit(1)
	}

	// Drain any queued events before starting
	if err := dw.DrainQueue(); err != nil {
		slog.Error("drain_queue_error", "err", err)
	}

	if err := dw.Start(); err != nil {
		slog.Error("watcher_start_error", "err", err)
		os.Exit(1)
	}

	// Dead-letter reconcile loop (REPORT-ONLY).
	reconcileCtx, reconcileCancel := context.WithCancel(context.Background())
	defer reconcileCancel()
	if cfg.Semantic.ReconcileIntervalSec > 0 {
		reconciler := watcher.NewReconciler(queue, time.Duration(cfg.Semantic.ReconcileIntervalSec)*time.Second)
		go reconciler.Run(reconcileCtx)
	}

	slog.Info("watching", "root", root, "machine_id", cfg.MachineID)
	fmt.Printf("Hub: %s | Debounce: %dms | Batch: %ds/%d events\n",
		cfg.Hub.URL, cfg.Hub.DebounceMs, cfg.Hub.BatchSec, cfg.Hub.BatchSize)
	fmt.Println("Press Ctrl+C to stop.")

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh

	fmt.Println("\nShutting down...")
	if err := dw.Stop(); err != nil {
		slog.Error("stop_error", "err", err)
	}
	fmt.Println("Done.")
}
