package main

import (
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/omnigraph/watcher/config"
	"github.com/omnigraph/watcher/sender"
	"github.com/omnigraph/watcher/watcher"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(1)
	}

	switch os.Args[1] {
	case "init":
		runInit()
	case "watch":
		runWatch()
	default:
		usage()
		os.Exit(1)
	}
}

func usage() {
	fmt.Println("Usage: watcher <command> [options]")
	fmt.Println("")
	fmt.Println("Commands:")
	fmt.Println("  init    Create default config at ~/.config/omnigraph/watcher.yaml")
	fmt.Println("  watch   Start watching a project directory")
	fmt.Println("")
	fmt.Println("Examples:")
	fmt.Println("  watcher init")
	fmt.Println("  watcher watch -config ~/.config/omnigraph/watcher.yaml")
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
	cfg.Ignore.GitIgnore = true
	cfg.Ignore.DockerIgnore = true

	path := config.DefaultConfigPath()
	if err := cfg.Save(path); err != nil {
		fmt.Fprintf(os.Stderr, "failed to write config: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("Config written to %s\n", path)
	fmt.Println("Edit watch_root and hub URL before running 'watch'.")
}

func runWatch() {
	fs := flag.NewFlagSet("watch", flag.ExitOnError)
	configPath := fs.String("config", "", "Path to watcher config file")
	fs.Parse(os.Args[2:])

	cfg, err := config.Load(*configPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "failed to load config: %v\n", err)
		os.Exit(1)
	}

	root := cfg.WatchRoot
	absPath, err := filepath.Abs(root)
	if err != nil {
		absPath = root
	}
	root = absPath

	filter, err := watcher.NewIgnoreFilter(
		root,
		cfg.Ignore.GitIgnore,
		cfg.Ignore.DockerIgnore,
		cfg.Ignore.Extra,
	)
	if err != nil {
		fmt.Fprintf(os.Stderr, "ignore filter error: %v\n", err)
		os.Exit(1)
	}

	var resolver *watcher.ProjectResolver
	if cfg.AutoDetect {
		resolver = watcher.NewProjectResolver(root, cfg.Markers)
	}

	client := sender.NewClient(cfg.Hub.URL, cfg.Hub.AuthToken, cfg.MachineID)
	queue, err := watcher.OpenQueue("")
	if err != nil {
		fmt.Fprintf(os.Stderr, "queue open error: %v\n", err)
		os.Exit(1)
	}
	defer queue.Close()

	dw, err := watcher.NewDebouncedWatcher(cfg, filter, resolver, client, queue)
	if err != nil {
		fmt.Fprintf(os.Stderr, "watcher init error: %v\n", err)
		os.Exit(1)
	}

	// Drain any queued events before starting
	if err := dw.DrainQueue(); err != nil {
		fmt.Fprintf(os.Stderr, "drain queue error: %v\n", err)
	}

	if err := dw.Start(); err != nil {
		fmt.Fprintf(os.Stderr, "watcher start error: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("Watching %s (machine=%s)\n", root, cfg.MachineID)
	fmt.Printf("Hub: %s | Debounce: %dms | Batch: %ds/%d events\n",
		cfg.Hub.URL, cfg.Hub.DebounceMs, cfg.Hub.BatchSec, cfg.Hub.BatchSize)
	fmt.Println("Press Ctrl+C to stop.")

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh

	fmt.Println("\nShutting down...")
	if err := dw.Stop(); err != nil {
		fmt.Fprintf(os.Stderr, "stop error: %v\n", err)
	}
	fmt.Println("Done.")
}
