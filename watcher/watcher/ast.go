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
	entities, _, err := ExtractGraph(path, content)
	return entities, err
}

func ExtractGraph(path string, content []byte) ([]models.Entity, []models.Relation, error) {
	ext := strings.ToLower(filepath.Ext(path))
	lang := languageMap[ext]
	if lang == nil {
		return nil, nil, nil
	}

	parser := sitter.NewParser()
	parser.SetLanguage(lang)
	tree := parser.Parse(nil, content)
	root := tree.RootNode()

	var entities []models.Entity
	walkTree(root, content, ext, &entities)
	relations := extractRelations(root, content, ext, entities)
	return entities, relations, nil
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

func appendGoMethodEntity(node *sitter.Node, content []byte, out *[]models.Entity) {
	name := node.ChildByFieldName("name")
	if name == nil {
		return
	}
	methodName := goMethodName(node, content)
	if methodName == "" {
		methodName = string(name.Content(content))
	}
	*out = append(*out, models.Entity{
		Name:      methodName,
		Type:      "function",
		Line:      int(name.StartPoint().Row) + 1,
		StartLine: int(node.StartPoint().Row) + 1,
		EndLine:   int(node.EndPoint().Row) + 1,
	})
}

func extractGo(node *sitter.Node, content []byte, out *[]models.Entity) {
	switch node.Type() {
	case "function_declaration":
		appendEntity(node, content, out, "function")
	case "method_declaration":
		appendGoMethodEntity(node, content, out)
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

func extractRelations(root *sitter.Node, content []byte, ext string, entities []models.Entity) []models.Relation {
	relations := make([]models.Relation, 0, len(entities))
	for _, ent := range entities {
		relations = append(relations, models.Relation{
			Type:       "CONTAINS",
			Target:     ent.Name,
			TargetType: ent.Type,
			Line:       ent.Line,
			Confidence: "syntax",
		})
	}

	if ext == ".go" {
		extractGoRelations(root, content, &relations)
	}
	return relations
}

func extractGoRelations(node *sitter.Node, content []byte, out *[]models.Relation) {
	if node == nil {
		return
	}

	switch node.Type() {
	case "import_spec":
		if target := goImportTarget(node, content); target != "" {
			*out = append(*out, models.Relation{
				Type:       "IMPORTS",
				Target:     target,
				TargetType: "module",
				Line:       int(node.StartPoint().Row) + 1,
				Confidence: "syntax",
			})
		}
	case "function_declaration", "method_declaration":
		source := nodeName(node, content)
		if node.Type() == "method_declaration" {
			source = goMethodName(node, content)
		}
		if source != "" {
			extractGoCallRelations(node, content, source, out)
		}
	}

	for i := 0; i < int(node.ChildCount()); i++ {
		extractGoRelations(node.Child(i), content, out)
	}
}

func goImportTarget(node *sitter.Node, content []byte) string {
	for i := 0; i < int(node.ChildCount()); i++ {
		child := node.Child(i)
		if child.Type() == "interpreted_string_literal" || child.Type() == "raw_string_literal" {
			return strings.Trim(string(child.Content(content)), "`\"")
		}
	}
	return ""
}

func extractGoCallRelations(node *sitter.Node, content []byte, source string, out *[]models.Relation) {
	if node == nil {
		return
	}
	if node.Type() == "call_expression" {
		if target := goCallTarget(node, content); target != "" {
			*out = append(*out, models.Relation{
				Type:       "CALLS_SYNTAX",
				Source:     source,
				Target:     target,
				TargetType: "symbol",
				Line:       int(node.StartPoint().Row) + 1,
				Confidence: "syntax",
			})
		}
	}
	for i := 0; i < int(node.ChildCount()); i++ {
		extractGoCallRelations(node.Child(i), content, source, out)
	}
}

func goCallTarget(node *sitter.Node, content []byte) string {
	function := node.ChildByFieldName("function")
	if function == nil {
		return ""
	}
	return selectorName(function, content)
}

func selectorName(node *sitter.Node, content []byte) string {
	switch node.Type() {
	case "identifier", "field_identifier", "qualified_type":
		return string(node.Content(content))
	case "selector_expression":
		operand := selectorName(node.ChildByFieldName("operand"), content)
		field := selectorName(node.ChildByFieldName("field"), content)
		if operand != "" && field != "" {
			return operand + "." + field
		}
		if field != "" {
			return field
		}
	}
	return strings.TrimSpace(string(node.Content(content)))
}

func nodeName(node *sitter.Node, content []byte) string {
	name := node.ChildByFieldName("name")
	if name == nil {
		return ""
	}
	return string(name.Content(content))
}

func goMethodName(node *sitter.Node, content []byte) string {
	name := node.ChildByFieldName("name")
	receiver := node.ChildByFieldName("receiver")
	if name == nil || receiver == nil {
		return ""
	}
	receiverType := goReceiverType(receiver, content)
	if receiverType == "" {
		return string(name.Content(content))
	}
	return receiverType + "." + string(name.Content(content))
}

func goReceiverType(node *sitter.Node, content []byte) string {
	if node == nil {
		return ""
	}
	switch node.Type() {
	case "type_identifier", "identifier":
		return string(node.Content(content))
	case "pointer_type":
		return goReceiverType(node.Child(1), content)
	}
	for i := 0; i < int(node.ChildCount()); i++ {
		if receiverType := goReceiverType(node.Child(i), content); receiverType != "" {
			return receiverType
		}
	}
	return ""
}

// ExtractFileSymbols is a convenience wrapper that reads the file then extracts.
func ExtractFileSymbols(path string) ([]models.Entity, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	return ExtractSymbols(path, content)
}
