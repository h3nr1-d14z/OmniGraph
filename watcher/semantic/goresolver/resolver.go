package goresolver

import (
	"context"
	"go/ast"
	"go/token"
	"go/types"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/omnigraph/watcher/models"
	"golang.org/x/tools/go/packages"
)

type Request struct {
	Root     string
	FilePath string
	Content  []byte
}

type Resolver struct{}

func (Resolver) Resolve(ctx context.Context, req Request) ([]models.Relation, error) {
	if filepath.Ext(req.FilePath) != ".go" {
		return nil, nil
	}
	absFile, err := filepath.Abs(req.FilePath)
	if err != nil {
		return nil, err
	}
	root, err := filepath.Abs(req.Root)
	if err != nil {
		return nil, err
	}

	cfg := &packages.Config{
		Context: ctx,
		Dir:     root,
		Mode: packages.NeedName |
			packages.NeedFiles |
			packages.NeedCompiledGoFiles |
			packages.NeedImports |
			packages.NeedTypes |
			packages.NeedTypesInfo |
			packages.NeedSyntax,
		Tests: false,
	}
	if len(req.Content) > 0 {
		cfg.Overlay = map[string][]byte{absFile: req.Content}
	}

	pkgs, err := packages.Load(cfg, "file="+absFile)
	if err != nil {
		return nil, err
	}
	pkg, file, index := packageForFile(pkgs, absFile)
	if pkg == nil || file == nil || len(pkg.Errors) > 0 {
		return nil, nil
	}

	relations := make([]models.Relation, 0)
	relations = append(relations, resolvedImports(pkg, file, index)...)
	relations = append(relations, resolvedCalls(pkg, file, index)...)
	return relations, nil
}

func packageForFile(pkgs []*packages.Package, filePath string) (*packages.Package, *ast.File, int) {
	for _, pkg := range pkgs {
		for i, syntax := range pkg.Syntax {
			if i >= len(pkg.CompiledGoFiles) {
				continue
			}
			compiled, err := filepath.Abs(pkg.CompiledGoFiles[i])
			if err == nil && compiled == filePath {
				return pkg, syntax, i
			}
		}
	}
	return nil, nil, -1
}

func resolvedImports(pkg *packages.Package, file *ast.File, fileIndex int) []models.Relation {
	relations := make([]models.Relation, 0, len(file.Imports))
	for _, spec := range file.Imports {
		path, err := strconv.Unquote(spec.Path.Value)
		if err != nil || path == "" {
			continue
		}
		imported := pkg.Imports[path]
		target := importName(spec, imported, path)
		relations = append(relations, models.Relation{
			Type:       "IMPORTS_RESOLVED",
			Target:     target,
			TargetType: "module",
			Line:       lineOf(pkg, fileIndex, spec.Pos()),
			Confidence: "semantic",
			Layer:      "semantic",
			Status:     "resolved",
			TargetRef:  path,
			Package:    path,
			Language:   "go",
		})
	}
	return relations
}

func importName(spec *ast.ImportSpec, imported *packages.Package, path string) string {
	if spec.Name != nil && spec.Name.Name != "." && spec.Name.Name != "_" {
		return spec.Name.Name
	}
	if imported != nil && imported.Name != "" {
		return imported.Name
	}
	parts := strings.Split(path, "/")
	return parts[len(parts)-1]
}

func resolvedCalls(pkg *packages.Package, file *ast.File, fileIndex int) []models.Relation {
	var relations []models.Relation
	ast.Inspect(file, func(node ast.Node) bool {
		call, ok := node.(*ast.CallExpr)
		if !ok {
			return true
		}
		source := enclosingFunction(file, call.Pos())
		if source == "" {
			return true
		}
		obj := calledObject(pkg.TypesInfo, call.Fun)
		if obj == nil {
			return true
		}
		targetRef, packagePath := objectRef(obj)
		if targetRef == "" {
			return true
		}
		relations = append(relations, models.Relation{
			Type:       "CALLS_RESOLVED",
			Source:     source,
			Target:     displayName(obj, targetRef),
			TargetType: "symbol",
			Line:       lineOf(pkg, fileIndex, call.Pos()),
			Confidence: "semantic",
			Layer:      "semantic",
			Status:     "resolved",
			SymbolID:   "go:" + targetRef,
			TargetRef:  targetRef,
			Package:    packagePath,
			Language:   "go",
		})
		return true
	})
	return relations
}

func calledObject(info *types.Info, expr ast.Expr) types.Object {
	switch fn := expr.(type) {
	case *ast.Ident:
		return info.Uses[fn]
	case *ast.SelectorExpr:
		if sel := info.Selections[fn]; sel != nil {
			return sel.Obj()
		}
		return info.Uses[fn.Sel]
	}
	return nil
}

func objectRef(obj types.Object) (string, string) {
	pkg := obj.Pkg()
	if pkg == nil {
		return obj.Name(), ""
	}
	if fn, ok := obj.(*types.Func); ok {
		if sig, ok := fn.Type().(*types.Signature); ok && sig.Recv() != nil {
			receiver := receiverName(sig.Recv().Type())
			if receiver != "" {
				return pkg.Path() + "." + receiver + "." + obj.Name(), pkg.Path()
			}
		}
	}
	return pkg.Path() + "." + obj.Name(), pkg.Path()
}

func receiverName(typ types.Type) string {
	if ptr, ok := typ.(*types.Pointer); ok {
		typ = ptr.Elem()
	}
	if named, ok := typ.(*types.Named); ok {
		return named.Obj().Name()
	}
	return ""
}

func displayName(obj types.Object, targetRef string) string {
	pkg := obj.Pkg()
	if pkg == nil || pkg.Name() == "" {
		return obj.Name()
	}
	if fn, ok := obj.(*types.Func); ok {
		if sig, ok := fn.Type().(*types.Signature); ok && sig.Recv() != nil {
			receiver := receiverName(sig.Recv().Type())
			if receiver != "" {
				return pkg.Name() + "." + receiver + "." + obj.Name()
			}
		}
	}
	return pkg.Name() + "." + obj.Name()
}

func enclosingFunction(file *ast.File, pos token.Pos) string {
	for _, decl := range file.Decls {
		switch fn := decl.(type) {
		case *ast.FuncDecl:
			if fn.Pos() <= pos && pos <= fn.End() {
				if fn.Recv != nil {
					if receiver := astReceiverName(fn.Recv); receiver != "" {
						return receiver + "." + fn.Name.Name
					}
				}
				return fn.Name.Name
			}
		}
	}
	return ""
}

func astReceiverName(recv *ast.FieldList) string {
	if recv == nil || len(recv.List) == 0 {
		return ""
	}
	return astTypeName(recv.List[0].Type)
}

func astTypeName(expr ast.Expr) string {
	switch typ := expr.(type) {
	case *ast.Ident:
		return typ.Name
	case *ast.StarExpr:
		return astTypeName(typ.X)
	}
	return ""
}

func lineOf(pkg *packages.Package, fileIndex int, pos token.Pos) int {
	if pkg.Fset == nil || pos == token.NoPos {
		return 0
	}
	return pkg.Fset.Position(pos).Line
}
