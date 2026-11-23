package com.example.archmind.service.ast;

import com.example.archmind.model.ast.ParsedAnnotation;
import com.example.archmind.model.ast.ParsedClass;
import com.example.archmind.model.ast.ParsedEndpoint;
import com.example.archmind.model.ast.ParsedFile;
import com.example.archmind.model.ast.ParsedMethod;
import com.example.archmind.model.ast.ParsedProject;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 从 ParsedProject 算出入口列表：HTTP 路由与定时 / MQ / 事件 / 启动任务。
 * 纯内存计算、无 Spring、无 IO，每次使用新建一个实例即可。
 *
 * <p>规则：
 * <ul>
 *   <li>类级 {@code RequestMapping} 的 values 作为前缀；无值视为空前缀。</li>
 *   <li>方法级 {@code Get/Post/Put/Delete/PatchMapping} 决定动词；方法级 {@code RequestMapping} 为 {@code ANY}。</li>
 *   <li>路径 = 前缀 + 方法路径；{@code //} 归一为 {@code /}，末尾去 {@code /}（根路径 {@code /} 保留）。</li>
 *   <li>多路径展开成多个入口，uid = 动词 + 空格 + 路径。</li>
 *   <li>方法级映射但没写路径：path = "/" + 方法名，inferred = true。</li>
 *   <li>非 HTTP：Scheduled / RabbitListener|KafkaListener / EventListener / CommandLineRunner|ApplicationRunner。</li>
 *   <li>同一方法同时有 HTTP 与非 HTTP 注解时只取 HTTP。</li>
 * </ul>
 */
public class EndpointResolver {

    private static final String REQUEST_MAPPING = "RequestMapping";

    private static final Map<String, String> HTTP_VERBS = Map.of(
            "GetMapping", "GET",
            "PostMapping", "POST",
            "PutMapping", "PUT",
            "DeleteMapping", "DELETE",
            "PatchMapping", "PATCH");

    /** 非 HTTP 注解 → (kind, uid 前缀)，按优先级排列 */
    private record NonHttp(String kind, String uidPrefix) {
    }

    private record HttpMapping(String verb, List<String> paths) {
    }

    public List<ParsedEndpoint> resolve(ParsedProject project) {
        List<ParsedEndpoint> result = new ArrayList<>();
        if (project == null || project.getFiles() == null) {
            return result;
        }
        for (ParsedFile file : project.getFiles()) {
            if (file == null || file.getClasses() == null) {
                continue;
            }
            for (ParsedClass cls : file.getClasses()) {
                resolveClass(cls, result);
            }
        }
        return result;
    }

    private void resolveClass(ParsedClass cls, List<ParsedEndpoint> out) {
        List<String> prefixes = classPrefixes(cls);
        boolean runner = isRunner(cls);
        for (ParsedMethod method : cls.getMethods()) {
            if (method == null || method.isConstructor()) {
                continue;
            }
            String handlerUid = handlerUid(method);

            HttpMapping http = httpMapping(method);
            if (http != null) {
                emitHttp(http, prefixes, method, handlerUid, out);
                continue; // HTTP 优先，忽略同一方法上的调度注解
            }

            NonHttp nonHttp = nonHttpKind(method);
            if (nonHttp != null) {
                out.add(new ParsedEndpoint(nonHttp.uidPrefix() + " " + handlerUid,
                        nonHttp.kind(), null, null, handlerUid, false));
                continue;
            }

            if (runner && "run".equals(method.getName())) {
                out.add(new ParsedEndpoint("MANUAL " + handlerUid,
                        "manual", null, null, handlerUid, false));
            }
        }
    }

    // ==================== HTTP ====================

    private void emitHttp(HttpMapping mapping, List<String> prefixes, ParsedMethod method,
                          String handlerUid, List<ParsedEndpoint> out) {
        List<String> methodPaths;
        boolean inferred;
        if (mapping.paths().isEmpty()) {
            methodPaths = List.of("/" + method.getName());
            inferred = true;
        } else {
            methodPaths = mapping.paths();
            inferred = false;
        }
        for (String prefix : prefixes) {
            for (String methodPath : methodPaths) {
                String path = normalize(join(prefix, methodPath));
                out.add(new ParsedEndpoint(mapping.verb() + " " + path,
                        "http", mapping.verb(), path, handlerUid, inferred));
            }
        }
    }

    /** 类级 RequestMapping 的 values 作为前缀；无注解或无值视为单个空前缀 */
    private List<String> classPrefixes(ParsedClass cls) {
        if (cls.getAnnotations() != null) {
            for (ParsedAnnotation annotation : cls.getAnnotations()) {
                if (isNamed(annotation, REQUEST_MAPPING)) {
                    List<String> values = nonBlank(annotation.values());
                    return values.isEmpty() ? List.of("") : values;
                }
            }
        }
        return List.of("");
    }

    /** 方法级 HTTP 映射；没有返回 null。首次命中即停。 */
    private HttpMapping httpMapping(ParsedMethod method) {
        if (method.getAnnotations() == null) {
            return null;
        }
        for (ParsedAnnotation annotation : method.getAnnotations()) {
            if (isNamed(annotation, REQUEST_MAPPING)) {
                return new HttpMapping("ANY", nonBlank(annotation.values()));
            }
            String verb = verbOf(annotation);
            if (verb != null) {
                return new HttpMapping(verb, nonBlank(annotation.values()));
            }
        }
        return null;
    }

    private String verbOf(ParsedAnnotation annotation) {
        for (Map.Entry<String, String> entry : HTTP_VERBS.entrySet()) {
            if (isNamed(annotation, entry.getKey())) {
                return entry.getValue();
            }
        }
        return null;
    }

    // ==================== 非 HTTP ====================

    private NonHttp nonHttpKind(ParsedMethod method) {
        if (method.getAnnotations() == null) {
            return null;
        }
        if (hasAnnotation(method, "Scheduled")) {
            return new NonHttp("scheduled", "SCHEDULED");
        }
        if (hasAnnotation(method, "RabbitListener") || hasAnnotation(method, "KafkaListener")) {
            return new NonHttp("mq", "MQ");
        }
        if (hasAnnotation(method, "EventListener")) {
            return new NonHttp("event", "EVENT");
        }
        return null;
    }

    private boolean hasAnnotation(ParsedMethod method, String simpleName) {
        for (ParsedAnnotation annotation : method.getAnnotations()) {
            if (isNamed(annotation, simpleName)) {
                return true;
            }
        }
        return false;
    }

    private boolean isRunner(ParsedClass cls) {
        return implementsType(cls, "CommandLineRunner") || implementsType(cls, "ApplicationRunner");
    }

    private boolean implementsType(ParsedClass cls, String simpleName) {
        if (cls.getImplementsTypes() == null) {
            return false;
        }
        for (String raw : cls.getImplementsTypes()) {
            if (simpleNameOf(raw).equals(simpleName)) {
                return true;
            }
        }
        return false;
    }

    // ==================== 工具 ====================

    private String handlerUid(ParsedMethod method) {
        return method.getOwnerQualifiedName() + "#" + method.getSignature();
    }

    /** 注解名取源码简单名，兼容全限定写法 */
    private boolean isNamed(ParsedAnnotation annotation, String simpleName) {
        if (annotation == null || annotation.name() == null) {
            return false;
        }
        return simpleNameOf(annotation.name()).equals(simpleName);
    }

    private String simpleNameOf(String name) {
        if (name == null) {
            return "";
        }
        int dot = name.lastIndexOf('.');
        return dot >= 0 ? name.substring(dot + 1) : name;
    }

    private List<String> nonBlank(List<String> values) {
        if (values == null) {
            return List.of();
        }
        return values.stream()
                .filter(v -> v != null && !v.isBlank())
                .map(String::trim)
                .toList();
    }

    /** 拼前缀与方法路径，保证交界处恰有一个 "/" */
    private String join(String prefix, String path) {
        String a = prefix == null ? "" : prefix.trim();
        String b = path == null ? "" : path.trim();
        if (a.isEmpty()) {
            return b;
        }
        if (b.isEmpty()) {
            return a;
        }
        boolean aSlash = a.endsWith("/");
        boolean bSlash = b.startsWith("/");
        if (aSlash && bSlash) {
            return a + b.substring(1);
        }
        if (!aSlash && !bSlash) {
            return a + "/" + b;
        }
        return a + b;
    }

    /** 补前导 "/"，把连续的 "/" 归一成一个，去末尾 "/"（根路径保留） */
    private String normalize(String path) {
        String p = path == null ? "" : path.trim();
        if (p.isEmpty()) {
            return "/";
        }
        if (!p.startsWith("/")) {
            p = "/" + p;
        }
        p = p.replaceAll("/{2,}", "/");
        while (p.length() > 1 && p.endsWith("/")) {
            p = p.substring(0, p.length() - 1);
        }
        return p;
    }
}
