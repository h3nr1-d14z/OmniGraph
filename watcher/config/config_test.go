package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadPreservesExplicitSemanticCacheSizeZero(t *testing.T) {
	path := filepath.Join(t.TempDir(), "watcher.yaml")
	content := []byte(`machine_id: test-machine
watch_root: /tmp
semantic:
  cache_size: 0
`)
	if err := os.WriteFile(path, content, 0600); err != nil {
		t.Fatalf("write config: %v", err)
	}

	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("load config: %v", err)
	}
	if cfg.Semantic.CacheSize != 0 {
		t.Fatalf("semantic cache size = %d, want 0", cfg.Semantic.CacheSize)
	}
}

func TestLoadDefaultsOmittedSemanticCacheSize(t *testing.T) {
	path := filepath.Join(t.TempDir(), "watcher.yaml")
	content := []byte(`machine_id: test-machine
watch_root: /tmp
`)
	if err := os.WriteFile(path, content, 0600); err != nil {
		t.Fatalf("write config: %v", err)
	}

	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("load config: %v", err)
	}
	if cfg.Semantic.CacheSize != 256 {
		t.Fatalf("semantic cache size = %d, want 256", cfg.Semantic.CacheSize)
	}
}

func TestLoadDefaultsOmittedAutoReplayKnobs(t *testing.T) {
	path := filepath.Join(t.TempDir(), "watcher.yaml")
	content := []byte(`machine_id: test-machine
watch_root: /tmp
`)
	if err := os.WriteFile(path, content, 0600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("load config: %v", err)
	}
	if cfg.Semantic.AutoReplayMinAgeSec != DefaultAutoReplayMinAgeSec {
		t.Errorf("AutoReplayMinAgeSec = %d, want %d", cfg.Semantic.AutoReplayMinAgeSec, DefaultAutoReplayMinAgeSec)
	}
	if cfg.Semantic.AutoReplayMaxCount != DefaultAutoReplayMaxCount {
		t.Errorf("AutoReplayMaxCount = %d, want %d", cfg.Semantic.AutoReplayMaxCount, DefaultAutoReplayMaxCount)
	}
	if cfg.Semantic.AutoReplayBatchSize != DefaultAutoReplayBatchSize {
		t.Errorf("AutoReplayBatchSize = %d, want %d", cfg.Semantic.AutoReplayBatchSize, DefaultAutoReplayBatchSize)
	}
}

func TestLoadPreservesExplicitAutoReplayZero(t *testing.T) {
	// Explicit `auto_replay_min_age_sec: 0` is a valid setting (immediate
	// replay for tests / aggressive recovery). hasYAMLPath() must
	// distinguish this from the omitted-field case so the default does
	// NOT clobber the operator's intent.
	path := filepath.Join(t.TempDir(), "watcher.yaml")
	content := []byte(`machine_id: test-machine
watch_root: /tmp
semantic:
  auto_replay_min_age_sec: 0
  auto_replay_max_count: 0
  auto_replay_batch_size: 0
`)
	if err := os.WriteFile(path, content, 0600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("load config: %v", err)
	}
	if cfg.Semantic.AutoReplayMinAgeSec != 0 {
		t.Errorf("AutoReplayMinAgeSec = %d, want 0 (preserved)", cfg.Semantic.AutoReplayMinAgeSec)
	}
	if cfg.Semantic.AutoReplayMaxCount != 0 {
		t.Errorf("AutoReplayMaxCount = %d, want 0 (preserved)", cfg.Semantic.AutoReplayMaxCount)
	}
	if cfg.Semantic.AutoReplayBatchSize != 0 {
		t.Errorf("AutoReplayBatchSize = %d, want 0 (preserved)", cfg.Semantic.AutoReplayBatchSize)
	}
}
