package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/omnigraph/watcher/models"
)

func TestSemanticCommandFailsForInvalidGoPackage(t *testing.T) {
	root := t.TempDir()
	file := filepath.Join(root, "main.go")
	writeMainTestFile(t, file, "package demo\n\nfunc main() {\n")

	cmd := exec.Command("go", "run", ".", "semantic", "-root", root, "-file", file)
	cmd.Dir = "."
	out, err := cmd.CombinedOutput()
	if err == nil {
		t.Fatalf("expected semantic command failure, got success: %s", out)
	}
	if !strings.Contains(string(out), "semantic resolve error") {
		t.Fatalf("expected semantic error, got: %s", out)
	}
}

func TestSemanticCommandOutputsResolvedRelations(t *testing.T) {
	root := t.TempDir()
	writeMainTestFile(t, filepath.Join(root, "go.mod"), "module example.com/semantic\n\ngo 1.22\n")
	file := filepath.Join(root, "main.go")
	writeMainTestFile(t, file, `package semantic

import "fmt"

func main() {
	fmt.Println("ok")
}
`)

	cmd := exec.Command("go", "run", ".", "semantic", "-root", root, "-file", file)
	cmd.Dir = "."
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("semantic command failed: %v\n%s", err, out)
	}

	var relations []models.Relation
	if err := json.Unmarshal(out, &relations); err != nil {
		t.Fatalf("decode semantic output: %v\n%s", err, out)
	}
	assertMainRelation(t, relations, "IMPORTS_RESOLVED", "", "fmt", "fmt")
	assertMainRelation(t, relations, "CALLS_RESOLVED", "main", "fmt.Println", "fmt.Println")
}

func writeMainTestFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func assertMainRelation(t *testing.T, relations []models.Relation, relationType, source, target, targetRef string) {
	t.Helper()
	for _, rel := range relations {
		if rel.Type == relationType && rel.Source == source && rel.Target == target && rel.TargetRef == targetRef {
			return
		}
	}
	t.Fatalf("missing %s %s -> %s (%s) in %#v", relationType, source, target, targetRef, relations)
}
