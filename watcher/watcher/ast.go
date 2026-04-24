package watcher

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	sitter "github.com/smacker/go-tree-sitter"
	"github.com/smacker/go-tree-sitter/golang"
	"github.com/smacker/go-tree-sitter/javascript"
	"github.com/smacker/go-tree-sitter/python"
	"github.com/smacker/go-tree-sitter/typescript/tsx"
	"github.com/smacker/go-tree-sitter/typescript/typescript"

	"github.com/omnigraph/watcher/models"
)

// languageMap maps file extensions to Tree-sitter parsers.
var languageMap = map[string]*sitter.Language{
	".go":  golang.GetLanguage(),
	".js":  javascript.GetLanguage(),
	".jsx": javascript.GetLanguage(),
	".ts":  typescript.GetLanguage(),
	".tsx": tsx.GetLanguage(),
	".py":  python.GetLanguage(),
	".mjs": javascript.GetLanguage(),
}

// ExtractSymbols parses a file and extracts code entities.
func ExtractSymbols(path string, content []byte) ([]models.Entity, error) {
	ext := strings.ToLower(filepath.Ext(path))
	lang := languageMap[ext]
	if lang == nil {
		// Fallback: no AST extraction for unsupported languages
		return nil, nil
	}

	parser := sitter.NewParser()
	parser.SetLanguage(lang)
	tree := parser.Parse(nil, content)
	root := tree.RootNode()

	var entities []models.Entity
	walkTree(root, content, ext, &entities)
	return entities, nil
}

func walkTree(node *sitter.Node, content []byte, ext string, out *[]models.Entity) {
	if node == nil {
		return
	}

	switch ext {
	case ".go":
		extractGo(node, content, out)
	case ".js", ".jsx", ".mjs":
		extractJS(node, content, out)
	case ".ts", ".tsx":
		extractTS(node, content, out)
	case ".py":
		extractPython(node, content, out)
	}

	for i := 0; i < int(node.ChildCount()); i++ {
		walkTree(node.Child(i), content, ext, out)
	}
}

func appendEntity(node *sitter.Node, content []byte, out *[]models.Entity, entityType string) {
	name := node.ChildByFieldName("name")
	if name == nil {
		return
	}
	*out = append(*out, models.Entity{
		Name:      string(name.Content(content)),
		Type:      entityType,
		Line:      int(name.StartPoint().Row) + 1,
		StartLine: int(node.StartPoint().Row) + 1,
		EndLine:   int(node.EndPoint().Row) + 1,
	})
}

func extractGo(node *sitter.Node, content []byte, out *[]models.Entity) {
	switch node.Type() {
	case "function_declaration", "method_declaration":
		appendEntity(node, content, out, "function")
	case "type_spec":
		appendEntity(node, content, out, "type")
	}
}

func extractJS(node *sitter.Node, content []byte, out *[]models.Entity) {
	switch node.Type() {
	case "function_declaration":
		appendEntity(node, content, out, "function")
	case "class_declaration":
		appendEntity(node, content, out, "class")
	case "method_definition":
		appendEntity(node, content, out, "method")
	}
}

func extractTS(node *sitter.Node, content []byte, out *[]models.Entity) {
	// TS shares most nodes with JS
	extractJS(node, content, out)
	// Additional TS-specific nodes
	switch node.Type() {
	case "interface_declaration":
		appendEntity(node, content, out, "interface")
	}
}

func extractPython(node *sitter.Node, content []byte, out *[]models.Entity) {
	switch node.Type() {
	case "function_definition":
		appendEntity(node, content, out, "function")
	case "class_definition":
		appendEntity(node, content, out, "class")
	}
}

// ExtractFileSymbols is a convenience wrapper that reads the file then extracts.
func ExtractFileSymbols(path string) ([]models.Entity, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	return ExtractSymbols(path, content)
}
