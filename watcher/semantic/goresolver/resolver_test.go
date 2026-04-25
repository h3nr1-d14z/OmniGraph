package goresolver

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/omnigraph/watcher/models"
)

func TestResolverResolvesImportsAndCalls(t *testing.T) {
	root := tempModule(t)
	file := filepath.Join(root, "main.go")
	writeFile(t, file, `package demo

import "fmt"

func helper() {}

func main() {
	fmt.Println("hello")
	helper()
}
`)

	relations, err := (Resolver{}).Resolve(context.Background(), Request{Root: root, FilePath: file})
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}

	assertRelation(t, relations, "IMPORTS_RESOLVED", "", "fmt", "fmt")
	assertRelation(t, relations, "CALLS_RESOLVED", "main", "fmt.Println", "fmt.Println")
	assertRelation(t, relations, "CALLS_RESOLVED", "main", "demo.helper", "example.com/demo.helper")
}

func TestResolverResolvesAliasedImport(t *testing.T) {
	root := tempModule(t)
	file := filepath.Join(root, "main.go")
	writeFile(t, file, `package demo

import out "fmt"

func main() {
	out.Println("hello")
}
`)

	relations, err := (Resolver{}).Resolve(context.Background(), Request{Root: root, FilePath: file})
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}

	assertRelation(t, relations, "IMPORTS_RESOLVED", "", "out", "fmt")
	assertRelation(t, relations, "CALLS_RESOLVED", "main", "fmt.Println", "fmt.Println")
}

func TestResolverDisambiguatesReceiverMethods(t *testing.T) {
	root := tempModule(t)
	file := filepath.Join(root, "main.go")
	writeFile(t, file, `package demo

type Alpha struct{}
type Beta struct{}

func (Alpha) helper() {}
func (Beta) helper() {}
func (Alpha) Run() { Alpha{}.helper() }
func (Beta) Run() { Beta{}.helper() }

func main() {
	Alpha{}.Run()
	Beta{}.Run()
}
`)

	relations, err := (Resolver{}).Resolve(context.Background(), Request{Root: root, FilePath: file})
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}

	assertRelation(t, relations, "CALLS_RESOLVED", "main", "demo.Alpha.Run", "example.com/demo.Alpha.Run")
	assertRelation(t, relations, "CALLS_RESOLVED", "main", "demo.Beta.Run", "example.com/demo.Beta.Run")
	assertRelation(t, relations, "CALLS_RESOLVED", "Alpha.Run", "demo.Alpha.helper", "example.com/demo.Alpha.helper")
	assertRelation(t, relations, "CALLS_RESOLVED", "Beta.Run", "demo.Beta.helper", "example.com/demo.Beta.helper")
}

func TestResolverUsesOverlayContent(t *testing.T) {
	root := tempModule(t)
	file := filepath.Join(root, "main.go")
	writeFile(t, file, `package demo

func main() {}
`)
	overlay := []byte(`package demo

import "fmt"

func main() {
	fmt.Println("overlay")
}
`)

	relations, err := (Resolver{}).Resolve(context.Background(), Request{Root: root, FilePath: file, Content: overlay})
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}

	assertRelation(t, relations, "IMPORTS_RESOLVED", "", "fmt", "fmt")
	assertRelation(t, relations, "CALLS_RESOLVED", "main", "fmt.Println", "fmt.Println")
}

func TestResolverReturnsNoRelationsForBrokenOverlay(t *testing.T) {
	root := tempModule(t)
	file := filepath.Join(root, "main.go")
	writeFile(t, file, `package demo

func main() {}
`)
	overlay := []byte(`package demo

import "fmt"

func main() {
	fmt.Println("missing close"
`)

	relations, err := (Resolver{}).Resolve(context.Background(), Request{Root: root, FilePath: file, Content: overlay})
	if err != nil {
		t.Fatalf("resolve broken overlay: %v", err)
	}
	if len(relations) != 0 {
		t.Fatalf("expected no relations for broken overlay, got %#v", relations)
	}
}

func TestResolverReturnsNoRelationsForMissingImport(t *testing.T) {
	root := tempModule(t)
	file := filepath.Join(root, "main.go")
	writeFile(t, file, `package demo

import "example.com/missing"

func main() {
	missing.Call()
}
`)

	relations, err := (Resolver{}).Resolve(context.Background(), Request{Root: root, FilePath: file})
	if err != nil {
		t.Fatalf("resolve missing import: %v", err)
	}
	if len(relations) != 0 {
		t.Fatalf("expected no relations for missing import, got %#v", relations)
	}
}

func TestResolverIgnoresNonGoFiles(t *testing.T) {
	relations, err := (Resolver{}).Resolve(context.Background(), Request{Root: t.TempDir(), FilePath: "README.md"})
	if err != nil {
		t.Fatalf("resolve non-go: %v", err)
	}
	if len(relations) != 0 {
		t.Fatalf("expected no relations, got %#v", relations)
	}
}

func tempModule(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "go.mod"), "module example.com/demo\n\ngo 1.22\n")
	return root
}

func writeFile(t *testing.T, path string, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func assertRelation(t *testing.T, relations []models.Relation, relationType, source, target, targetRef string) {
	t.Helper()
	for _, rel := range relations {
		if rel.Type == relationType && rel.Source == source && rel.Target == target && rel.TargetRef == targetRef {
			if rel.Layer != "semantic" || rel.Status != "resolved" || rel.Language != "go" || rel.Confidence != "semantic" {
				t.Fatalf("bad semantic metadata: %#v", rel)
			}
			return
		}
	}
	t.Fatalf("missing %s %s -> %s (%s) in %#v", relationType, source, target, targetRef, relations)
}
