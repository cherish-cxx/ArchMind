package com.example.archmind.service.ast;

import com.example.archmind.model.ast.ParsedProject;
import com.example.archmind.model.ast.SourceFileView;

import java.util.List;

/**
 * AST 解析器：把 Java 源码文本翻译成结构化声明 + 关系边。
 * 内部依次跑 A（抽声明与悬空调用）→ B（建索引）→ C（消解关系）。
 * 纯函数、零 IO——不碰磁盘、不碰数据库、不碰 Spring。
 * 喂字符串就能跑，方便单测。
 */
public interface AstParserService {

    /**
     * 解析一批 Java 源码文件，产出全项目的声明与已消解的关系边。
     * 单文件解析失败仅告警跳过，不拖垮整批。
     *
     * @param files 待解析文件列表（每项含 fileId、相对路径、源码文本）
     * @return 全项目解析结果（声明 + 关系边，edges 已填好）
     */
    ParsedProject parse(List<SourceFileView> files);
}
