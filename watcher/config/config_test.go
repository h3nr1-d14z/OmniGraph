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
