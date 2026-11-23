package com.example.archmind.model.ast;

import java.util.List;

/**
 * 一个注解声明：注解名 + 注解里所有字符串字面量（按源码顺序，去重）。
 * 名字取源码写法（如 "RequestMapping"），消解留给后续阶段。
 */
public record ParsedAnnotation(String name, List<String> values) {
}
