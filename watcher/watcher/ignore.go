package watcher

import (
	"os"
	"path/filepath"
	"strings"

	gitignore "github.com/sabhiram/go-gitignore"
)

// hardcoded exclusions that are always ignored.
var hardcodedExcludes = []string{
	"node_modules", "vendor", ".git", ".svn", ".hg",
	".idea", ".vscode", ".vs",
	"venv", ".venv", "env", ".env",
	"__pycache__", ".pytest_cache", ".mypy_cache",
	"build", "dist", "out", "target", "bin", "obj",
	".next", ".nuxt", ".svelte-kit",
	"*.log", "*.tmp", "*.swp", "*.swo", "*.DS_Store",
	"coverage", ".coverage", "htmlcov",
}

// IgnoreFilter determines whether a path should be skipped.
type IgnoreFilter struct {
	git    gitignore.IgnoreParser
	docker gitignore.IgnoreParser
	extra  []string
	root   string
}

// NewIgnoreFilter creates a filter for the given root directory.
func NewIgnoreFilter(root string, useGit, useDocker bool, extra []string) (*IgnoreFilter, error) {
	f := &IgnoreFilter{root: root, extra: extra}

	if useGit {
		path := filepath.Join(root, ".gitignore")
		if data, err := os.ReadFile(path); err == nil {
			f.git = gitignore.CompileIgnoreLines(strings.Split(string(data), "\n")...)
		}
	}

	if useDocker {
		path := filepath.Join(root, ".dockerignore")
		if data, err := os.ReadFile(path); err == nil {
			f.docker = gitignore.CompileIgnoreLines(strings.Split(string(data), "\n")...)
		}
	}

	return f, nil
}

// Match returns true if the path should be ignored.
func (f *IgnoreFilter) Match(path string) bool {
	rel, err := filepath.Rel(f.root, path)
	if err != nil {
		return true
	}
	rel = filepath.ToSlash(rel)

	// Hardcoded exclusions
	for _, ex := range hardcodedExcludes {
		if matched, _ := filepath.Match(ex, filepath.Base(rel)); matched {
			return true
		}
		if strings.Contains(rel, "/"+ex+"/") || strings.HasPrefix(rel, ex+"/") {
			return true
		}
	}

	// Extra user patterns
	for _, ex := range f.extra {
		if matched, _ := filepath.Match(ex, filepath.Base(rel)); matched {
			return true
		}
	}

	if f.git != nil && f.git.MatchesPath(rel) {
		return true
	}
	if f.docker != nil && f.docker.MatchesPath(rel) {
		return true
	}

	return false
}
