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

	Ignore struct {
		GitIgnore    bool     `yaml:"gitignore"`
		DockerIgnore bool     `yaml:"dockerignore"`
		Extra        []string `yaml:"extra"`
	} `yaml:"ignore"`
}

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
	if cfg.AutoDetect == false && cfg.ProjectName == "" {
		cfg.AutoDetect = true // default to auto-detect
	}
	if len(cfg.Markers) == 0 {
		cfg.Markers = DefaultProjectMarkers
	}

	return &cfg, nil
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
