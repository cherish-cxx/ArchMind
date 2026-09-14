package com.example.archmind.model.ast;

import lombok.Data;

import java.util.ArrayList;
import java.util.List;

/**
 * 全项目解析结果的总容器。
 * A 阶段产出 files（声明 + 悬空调用），B/C 阶段在其上建索引并消解出 edges。
 */
@Data
public class ParsedProject {

    private List<ParsedFile> files = new ArrayList<>();

    /** C 阶段消解产出的关系边，parse() 返回时已填好 */
    private List<ParsedEdge> edges = new ArrayList<>();
}
