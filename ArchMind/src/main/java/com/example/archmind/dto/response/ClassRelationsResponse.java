package com.example.archmind.dto.response;

import lombok.Data;

import java.util.List;

/**
 * 「点类看关联代码」的返回体：中心类 + 上游（谁指向我）+ 下游（我指向谁）。
 * 上游/下游各按关系类型再细分，前端分块渲染「谁调我 / 我调谁」。
 */
@Data
public class ClassRelationsResponse {

    private ClassNodeDTO center;

    private Upstream upstream;

    private Downstream downstream;

    @Data
    public static class Upstream {
        /** 谁的方法调用了我的方法 */
        private List<RelatedClassDTO> callers;
        /** 谁依赖我（字段/签名类型指向我） */
        private List<RelatedClassDTO> dependents;
        /** 谁继承/实现了我 */
        private List<RelatedClassDTO> subclasses;
    }

    @Data
    public static class Downstream {
        /** 我调用了谁 */
        private List<RelatedClassDTO> callees;
        /** 我依赖谁 */
        private List<RelatedClassDTO> dependencies;
        /** 我继承/实现了谁 */
        private List<RelatedClassDTO> parents;
    }
}
