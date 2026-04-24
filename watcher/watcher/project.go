package watcher

import (
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// ProjectResolver maps file paths to project names based on marker files.
type ProjectResolver struct {
	root     string
	markers  []string
	projects map[string]string // dir path -> project name
	mu       sync.RWMutex
}

// NewProjectResolver creates a resolver for the given root.
func NewProjectResolver(root string, markers []string) *ProjectResolver {
	return &ProjectResolver{
		root:     root,
		markers:  markers,
		projects: make(map[string]string),
	}
}

// Discover scans the root directory to find all projects.
func (pr *ProjectResolver) Discover() error {
	pr.mu.Lock()
	defer pr.mu.Unlock()

	pr.projects = make(map[string]string)

	return filepath.Walk(pr.root, func(path string, info os.FileInfo, err error) error {
		if err != nil || !info.IsDir() {
			return nil
		}
		// Skip ignored dirs
		for _, skip := range hardcodedExcludes {
			if strings.Contains(path, "/"+skip+"/") || filepath.Base(path) == skip {
				return filepath.SkipDir
			}
		}

		// Check for project markers
		for _, marker := range pr.markers {
			markerPath := filepath.Join(path, marker)
			if _, err := os.Stat(markerPath); err == nil {
				pr.projects[path] = filepath.Base(path)
				// Don't recurse deeper — subdirs belong to this project
				return filepath.SkipDir
			}
		}
		return nil
	})
}

// Resolve returns the project name for a given file path.
func (pr *ProjectResolver) Resolve(filePath string) string {
	pr.mu.RLock()
	defer pr.mu.RUnlock()

	// Find the longest matching project root
	var bestProject string
	var bestLen int

	for dir, name := range pr.projects {
		rel, err := filepath.Rel(dir, filePath)
		if err != nil || strings.HasPrefix(rel, "..") {
			continue
		}
		if len(dir) > bestLen {
			bestLen = len(dir)
			bestProject = name
		}
	}

	if bestProject != "" {
		return bestProject
	}

	// Fallback: return the immediate parent directory name
	return filepath.Base(filepath.Dir(filePath))
}

// List returns all discovered project names.
func (pr *ProjectResolver) List() []string {
	pr.mu.RLock()
	defer pr.mu.RUnlock()

	seen := make(map[string]struct{})
	var out []string
	for _, name := range pr.projects {
		if _, ok := seen[name]; !ok {
			seen[name] = struct{}{}
			out = append(out, name)
		}
	}
	return out
}
