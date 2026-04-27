package config

import (
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

// WatcherConfig holds all configuration for the watcher node.
type WatcherConfig struct {
	MachineID string `yaml:"machine_id"`
	WatchRoot string `yaml:"watch_root"` // e.g. ~/Projects/your-repo

	AutoDetect bool     `yaml:"auto_detect_projects"` // default true
	Markers    []string `yaml:"project_markers"`      // e.g. .git, go.mod

	// Legacy single-project mode (optional override)
	ProjectName string `yaml:"project_name,omitempty"`

	Hub struct {
		URL             string `yaml:"url"`
		AuthToken       string `yaml:"auth_token"`
		BatchSize       int    `yaml:"batch_size"`
		BatchSec        int    `yaml:"batch_sec"`
		DebounceMs      int    `yaml:"debounce_ms"`
		MaxEventsPerSec int    `yaml:"max_events_per_sec"`
	} `yaml:"hub"`

	Semantic struct {
		Enabled       bool `yaml:"enabled"`
		WorkerCount   int  `yaml:"worker_count"`
		QueueCapacity int  `yaml:"queue_capacity"`
		TimeoutMs     int  `yaml:"timeout_ms"`
		RetryDelayMs  int  `yaml:"retry_delay_ms"`
		MaxRetries    int  `yaml:"max_retries"`
		CacheSize     int  `yaml:"cache_size"`
		// Dead-letter reconciliation cadence (REPORT-ONLY).
		// 0 = disabled; default 3600 (1h) on first Load() if unset.
		ReconcileIntervalSec int `yaml:"reconcile_interval_sec"`
	} `yaml:"semantic"`

	Ignore struct {
		GitIgnore    bool     `yaml:"gitignore"`
		DockerIgnore bool     `yaml:"dockerignore"`
		Extra        []string `yaml:"extra"`
	} `yaml:"ignore"`
}

const DefaultSemanticCacheSize = 256

// DefaultReconcileIntervalSec is the dead-letter reconcile cadence used
// when reconcile_interval_sec is omitted from config. 0 disables the loop.
const DefaultReconcileIntervalSec = 3600

// SemanticDeadRowSoftCap triggers a warning log when dead-row count exceeds
// this; signals operator attention without aborting watcher.
const SemanticDeadRowSoftCap = 1000

var DefaultProjectMarkers = []string{
	".git",
	"go.mod",
	"package.json",
	"Cargo.toml",
	"pyproject.toml",
	"setup.py",
	"pom.xml",
	"build.gradle",
	"composer.json",
}

func DefaultConfigPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".config", "omnigraph", "watcher.yaml")
}

func Load(path string) (*WatcherConfig, error) {
	if path == "" {
		path = DefaultConfigPath()
	}

	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, fmt.Errorf("config not found at %s: run 'watcher init' first", path)
		}
		return nil, err
	}

	var cfg WatcherConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}

	if cfg.Hub.BatchSize == 0 {
		cfg.Hub.BatchSize = 50
	}
	if cfg.Hub.BatchSec == 0 {
		cfg.Hub.BatchSec = 5
	}
	if cfg.Hub.DebounceMs == 0 {
		cfg.Hub.DebounceMs = 3000
	}
	if cfg.Hub.MaxEventsPerSec == 0 {
		cfg.Hub.MaxEventsPerSec = 100
	}
	if cfg.Semantic.WorkerCount == 0 {
		cfg.Semantic.WorkerCount = 1
	}
	if cfg.Semantic.QueueCapacity == 0 {
		cfg.Semantic.QueueCapacity = 100
	}
	if cfg.Semantic.TimeoutMs == 0 {
		cfg.Semantic.TimeoutMs = 3000
	}
	if cfg.Semantic.RetryDelayMs == 0 {
		cfg.Semantic.RetryDelayMs = 500
	}
	if cfg.Semantic.MaxRetries == 0 {
		cfg.Semantic.MaxRetries = 3
	}
	if !hasYAMLPath(data, "semantic", "cache_size") {
		cfg.Semantic.CacheSize = DefaultSemanticCacheSize
	}
	if !hasYAMLPath(data, "semantic", "reconcile_interval_sec") {
		cfg.Semantic.ReconcileIntervalSec = DefaultReconcileIntervalSec
	}
	if cfg.AutoDetect == false && cfg.ProjectName == "" {
		cfg.AutoDetect = true // default to auto-detect
	}
	if len(cfg.Markers) == 0 {
		cfg.Markers = DefaultProjectMarkers
	}

	return &cfg, nil
}

func hasYAMLPath(data []byte, path ...string) bool {
	var node yaml.Node
	if err := yaml.Unmarshal(data, &node); err != nil || len(node.Content) == 0 {
		return false
	}
	current := node.Content[0]
	for _, segment := range path {
		if current.Kind != yaml.MappingNode {
			return false
		}
		found := false
		for i := 0; i+1 < len(current.Content); i += 2 {
			if current.Content[i].Value == segment {
				current = current.Content[i+1]
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	return true
}

func (c *WatcherConfig) Save(path string) error {
	if path == "" {
		path = DefaultConfigPath()
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	data, err := yaml.Marshal(c)
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0600)
}
