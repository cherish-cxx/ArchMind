package com.example.archmind.service.impl;

import com.example.archmind.model.ast.*;
import com.example.archmind.service.ast.AstParserService;
import com.example.archmind.service.ast.RelationResolver;
import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.ConstructorDeclaration;
import com.github.javaparser.ast.body.EnumDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.expr.VariableDeclarationExpr;
import com.github.javaparser.ast.AccessSpecifier;
import com.github.javaparser.ast.nodeTypes.NodeWithRange;
import com.github.javaparser.ast.nodeTypes.modifiers.NodeWithAccessModifiers;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * AST 解析流水线编排：A 逐文件抽声明与悬空调用 → B 建全项目索引 → C 消解关系产边。
 * 纯函数、零 IO，可单测。
 */
@Service
@Slf4j
public class AstParserServiceImpl implements AstParserService {

    private final JavaParser parser;

    public AstParserServiceImpl() {
        ParserConfiguration cfg = new ParserConfiguration();
        // 3.25.10 没有 JAVA_21 常量，BLEEDING_EDGE 尽量接受新语法
        cfg.setLanguageLevel(ParserConfiguration.LanguageLevel.BLEEDING_EDGE);
        this.parser = new JavaParser(cfg);
    }

    // ==================== 主循环 ====================

    @Override
    public ParsedProject parse(List<SourceFileView> files) {
        ParsedProject project = new ParsedProject();
//        每一轮拿到一个文件对象，里面包含relativePath等等的文件内容
        for (SourceFileView file : files) {
            try {
                ParseResult<CompilationUnit> result = parser.parse(file.content());
//              解析没有语法错误，并且拿到AST语法树
                if (result.isSuccessful() && result.getResult().isPresent()) {
//                    调用extractFile把原始文件信息+AST打包成ParsedFile对象
                    ParsedFile pf = extractFile(file, result.getResult().get());
                    project.getFiles().add(pf);
                } else {
                    log.warn("AST 解析失败，跳过: {} — 问题数: {}",
                            file.relativePath(), result.getProblems().size());
                }
            } catch (Exception e) {
                log.warn("AST 解析异常，跳过: {}", file.relativePath(), e);
            }
        }

        // B：建全项目符号索引（类/方法字典）
        ProjectIndex index = ProjectIndex.build(project);
        // C：关系解析——拿字典给悬空调用与短名定名，产出边
        project.setEdges(new RelationResolver(index).resolve(project));
        return project;
    }

    // ==================== 抽文件 ====================

    private ParsedFile extractFile(SourceFileView file, CompilationUnit cu) {
        ParsedFile pf = new ParsedFile();
        pf.setFileId(file.fileId());
        pf.setRelativePath(file.relativePath());
        pf.setPackageName(cu.getPackageDeclaration()
                .map(p -> p.getNameAsString())
                .orElse(""));
        pf.setImports(cu.getImports().stream()
                .map(imp -> imp.getNameAsString())
                .toList());

        for (TypeDeclaration<?> type : cu.getTypes()) {
            extractType(pf, file, type, null);
        }
        return pf;
    }

    /**
     * 抽一个类型声明（类/接口/枚举），嵌套类打平进 pf.classes。
     *
     * @param enclosing 外层类的 qualifiedName，null 表示顶层
     */
    private void extractType(ParsedFile pf, SourceFileView file,
                             TypeDeclaration<?> type, String enclosing) {
        if (type instanceof ClassOrInterfaceDeclaration cid) {
            ParsedClass pc = extractClass(pf, file, cid, enclosing);
            pf.getClasses().add(pc);
            // 嵌套类打平
            cid.getMembers().stream()
                    .filter(m -> m instanceof ClassOrInterfaceDeclaration)
                    .map(m -> (ClassOrInterfaceDeclaration) m)
                    .forEach(nested -> extractType(pf, file, nested, pc.getQualifiedName()));
            cid.getMembers().stream()
                    .filter(m -> m instanceof EnumDeclaration)
                    .map(m -> (EnumDeclaration) m)
                    .forEach(nested -> extractType(pf, file, nested, pc.getQualifiedName()));
        } else if (type instanceof EnumDeclaration ed) {
            ParsedClass pc = extractEnum(pf, file, ed, enclosing);
            pf.getClasses().add(pc);
            // 枚举里的嵌套类
            ed.getMembers().stream()
                    .filter(m -> m instanceof ClassOrInterfaceDeclaration)
                    .map(m -> (ClassOrInterfaceDeclaration) m)
                    .forEach(nested -> extractType(pf, file, nested, pc.getQualifiedName()));
        }
        // 其他类型（注解 @interface 等）P1 跳过
    }

    // ==================== 抽类 ====================

    private ParsedClass extractClass(ParsedFile pf, SourceFileView file,
                                     ClassOrInterfaceDeclaration decl, String enclosing) {
        ParsedClass pc = new ParsedClass();
        String simple = decl.getNameAsString();
        pc.setQualifiedName(buildQualifiedName(pf.getPackageName(), simple, enclosing));
        pc.setSimpleName(simple);
        pc.setPackageName(pf.getPackageName());
        pc.setKind(decl.isInterface() ? ClassKind.INTERFACE : ClassKind.CLASS);
        fillCommonClassFields(pc, file, decl);
        pc.setAbstract(decl.isAbstract());
        pc.setExtendsTypes(decl.getExtendedTypes().stream().map(t -> t.getNameAsString()).toList());
        pc.setImplementsTypes(decl.getImplementedTypes().stream().map(t -> t.getNameAsString()).toList());
        extractFields(pc, decl);
        extractMethods(pc, decl);
        return pc;
    }

    private ParsedClass extractEnum(ParsedFile pf, SourceFileView file,
                                    EnumDeclaration decl, String enclosing) {
        ParsedClass pc = new ParsedClass();
        String simple = decl.getNameAsString();
        pc.setQualifiedName(buildQualifiedName(pf.getPackageName(), simple, enclosing));
        pc.setSimpleName(simple);
        pc.setPackageName(pf.getPackageName());
        pc.setKind(ClassKind.ENUM);
        fillCommonClassFields(pc, file, decl);
        pc.setAbstract(false);
        pc.setExtendsTypes(decl.getImplementedTypes().stream().map(t -> t.getNameAsString()).toList());
        // 枚举的 implements 存到 extendsTypes（枚举隐式 extends Enum，这里只取显式 implements）
        extractFields(pc, decl);
        extractEnumMethods(pc, decl);
        return pc;
    }

    private void fillCommonClassFields(ParsedClass pc, SourceFileView file,
                                       TypeDeclaration<?> decl) {
        pc.setFileId(file.fileId());
        pc.setFilePath(file.relativePath());
        int[] range = lineRange(decl);
        pc.setStartLine(range[0]);
        pc.setEndLine(range[1]);
        pc.setVisibility(accessOf(decl));
    }

    // ==================== 抽字段 ====================

    private void extractFields(ParsedClass pc, TypeDeclaration<?> decl) {
        for (FieldDeclaration fd : decl.getFields()) {
            String type = fd.getElementType().asString();
            String vis = accessOf(fd);
            boolean isStatic = fd.isStatic();
            int line = lineOf(fd);
            for (var v : fd.getVariables()) {
                ParsedField pf = new ParsedField(
                        v.getNameAsString(), type, vis, isStatic, line);
                pc.getFields().add(pf);
            }
        }
    }

    // ==================== 抽方法 ====================

    private void extractMethods(ParsedClass pc, ClassOrInterfaceDeclaration decl) {
        for (MethodDeclaration md : decl.getMethods()) {
            pc.getMethods().add(extractMethod(pc, md));
        }
        for (ConstructorDeclaration cd : decl.getConstructors()) {
            pc.getMethods().add(extractConstructor(pc, cd));
        }
    }

    private void extractEnumMethods(ParsedClass pc, EnumDeclaration decl) {
        for (MethodDeclaration md : decl.getMethods()) {
            pc.getMethods().add(extractMethod(pc, md));
        }
        for (ConstructorDeclaration cd : decl.getConstructors()) {
            pc.getMethods().add(extractConstructor(pc, cd));
        }
    }

    private ParsedMethod extractMethod(ParsedClass owner, MethodDeclaration md) {
        ParsedMethod pm = new ParsedMethod();
        pm.setOwnerQualifiedName(owner.getQualifiedName());
        pm.setName(md.getNameAsString());
        pm.setQualifiedName(owner.getQualifiedName() + "#" + md.getNameAsString());
        pm.setConstructor(false);
        pm.setReturnType(md.getType().asString());
        pm.setStatic(md.isStatic());
        pm.setAbstract(md.isAbstract());

        // 参数
        for (var p : md.getParameters()) {
            pm.getParams().add(new ParsedParam(p.getNameAsString(), p.getType().asString()));
        }
        pm.setSignature(buildSignature(md.getNameAsString(), pm.getParams()));

        int[] range = lineRange(md);
        pm.setStartLine(range[0]);
        pm.setEndLine(range[1]);
        pm.setVisibility(accessOf(md));

        // 作用域表 + 悬空调用
        buildLocalTypesAndCalls(pm, owner, md);
        return pm;
    }

    private ParsedMethod extractConstructor(ParsedClass owner, ConstructorDeclaration cd) {
        ParsedMethod pm = new ParsedMethod();
        pm.setOwnerQualifiedName(owner.getQualifiedName());
        pm.setName(cd.getNameAsString());
        pm.setQualifiedName(owner.getQualifiedName() + "#" + cd.getNameAsString());
        pm.setConstructor(true);
        pm.setReturnType(null);
        pm.setStatic(false);
        pm.setAbstract(false);

        for (var p : cd.getParameters()) {
            pm.getParams().add(new ParsedParam(p.getNameAsString(), p.getType().asString()));
        }
        pm.setSignature(buildSignature(cd.getNameAsString(), pm.getParams()));

        int[] range = lineRange(cd);
        pm.setStartLine(range[0]);
        pm.setEndLine(range[1]);
        pm.setVisibility(accessOf(cd));

        buildLocalTypesAndCallsForConstructor(pm, owner, cd);
        return pm;
    }

    // ==================== 作用域表 + 悬空调用 ====================

    private void buildLocalTypesAndCalls(ParsedMethod pm, ParsedClass owner, MethodDeclaration md) {
        Map<String, String> locals = pm.getLocalTypes();

        // ① 参数（优先级最高）
        for (ParsedParam p : pm.getParams()) {
            locals.put(p.name(), p.type());
        }

        // ② 局部变量
        try {
            for (VariableDeclarationExpr vde : md.findAll(VariableDeclarationExpr.class)) {
                String type = vde.getElementType().asString();
                for (var v : vde.getVariables()) {
                    locals.putIfAbsent(v.getNameAsString(), type);
                }
            }
        } catch (Exception e) {
            log.debug("抽取局部变量失败: {}#{}", owner.getQualifiedName(), pm.getName());
        }

        // ③ 本类字段（putIfAbsent：局部变量/参数同名时遮蔽字段）
        for (ParsedField f : owner.getFields()) {
            locals.putIfAbsent(f.name(), f.type());
        }

        // ④ 悬空调用
        Set<String> seen = new HashSet<>();
        try {
            for (MethodCallExpr mce : md.findAll(MethodCallExpr.class)) {
                String receiver = mce.getScope().map(Object::toString).orElse(null);
                ParsedCall call = new ParsedCall(
                        receiver, mce.getNameAsString(),
                        mce.getArguments().size(), lineOf(mce));
                if (seen.add(callKey(call))) {
                    pm.getCalls().add(call);
                }
            }
        } catch (Exception e) {
            log.debug("抽取方法调用失败: {}#{}", owner.getQualifiedName(), pm.getName());
        }

        // ⑤ new Foo() 当作调用
        try {
            for (ObjectCreationExpr oce : md.findAll(ObjectCreationExpr.class)) {
                ParsedCall call = new ParsedCall(
                        oce.getType().getNameAsString(), "<init>",
                        oce.getArguments().size(), lineOf(oce));
                if (seen.add(callKey(call))) {
                    pm.getCalls().add(call);
                }
            }
        } catch (Exception e) {
            log.debug("抽取对象创建失败: {}#{}", owner.getQualifiedName(), pm.getName());
        }
    }

    private void buildLocalTypesAndCallsForConstructor(ParsedMethod pm, ParsedClass owner,
                                                       ConstructorDeclaration cd) {
        Map<String, String> locals = pm.getLocalTypes();

        for (ParsedParam p : pm.getParams()) {
            locals.put(p.name(), p.type());
        }

        try {
            for (VariableDeclarationExpr vde : cd.findAll(VariableDeclarationExpr.class)) {
                String type = vde.getElementType().asString();
                for (var v : vde.getVariables()) {
                    locals.putIfAbsent(v.getNameAsString(), type);
                }
            }
        } catch (Exception e) {
            log.debug("抽取局部变量失败(构造器): {}#{}", owner.getQualifiedName(), pm.getName());
        }

        for (ParsedField f : owner.getFields()) {
            locals.putIfAbsent(f.name(), f.type());
        }

        Set<String> seen = new HashSet<>();
        try {
            for (MethodCallExpr mce : cd.findAll(MethodCallExpr.class)) {
                String receiver = mce.getScope().map(Object::toString).orElse(null);
                ParsedCall call = new ParsedCall(
                        receiver, mce.getNameAsString(),
                        mce.getArguments().size(), lineOf(mce));
                if (seen.add(callKey(call))) {
                    pm.getCalls().add(call);
                }
            }
        } catch (Exception e) {
            log.debug("抽取方法调用失败(构造器): {}#{}", owner.getQualifiedName(), pm.getName());
        }

        try {
            for (ObjectCreationExpr oce : cd.findAll(ObjectCreationExpr.class)) {
                ParsedCall call = new ParsedCall(
                        oce.getType().getNameAsString(), "<init>",
                        oce.getArguments().size(), lineOf(oce));
                if (seen.add(callKey(call))) {
                    pm.getCalls().add(call);
                }
            }
        } catch (Exception e) {
            log.debug("抽取对象创建失败(构造器): {}#{}", owner.getQualifiedName(), pm.getName());
        }
    }

    // ==================== 工具方法 ====================

    private String buildQualifiedName(String pkg, String simpleName, String enclosing) {
        String base = pkg.isEmpty() ? simpleName : pkg + "." + simpleName;
        return enclosing == null ? base : enclosing + "." + simpleName;
    }

    private String buildSignature(String name, List<ParsedParam> params) {
        String paramTypes = params.stream()
                .map(ParsedParam::type)
                .reduce((a, b) -> a + "," + b)
                .orElse("");
        return name + "(" + paramTypes + ")";
    }

    private String callKey(ParsedCall call) {
        return call.receiver() + "|" + call.methodName() + "|" + call.argCount();
    }

    private int lineOf(NodeWithRange<?> node) {
        return node.getRange().map(r -> r.begin.line).orElse(-1);
    }

    private int[] lineRange(NodeWithRange<?> node) {
        return node.getRange()
                .map(r -> new int[]{r.begin.line, r.end.line})
                .orElse(new int[]{-1, -1});
    }

    private String accessOf(NodeWithAccessModifiers<?> node) {
        return node.getAccessSpecifier().name().toLowerCase();
    }

    /** TypeDeclaration 的访问级别：有修饰符接口就走，否则兜底 package_private */
    private String accessOf(TypeDeclaration<?> decl) {
        if (decl instanceof NodeWithAccessModifiers<?> nwa) {
            return nwa.getAccessSpecifier().name().toLowerCase();
        }
        return "package_private";
    }
}
